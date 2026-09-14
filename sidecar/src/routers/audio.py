"""WS /audio/stream：双路音频上行 + ASR 转写下行（R9–R14 落地）。

落地裁定（PRD 未细化处，见 docs/api-contract.md WS 节）：
- 一连接承载一路：path 由本连接首个 segment_start 确立并强制，
  冲突路径的 segment 事件忽略 + 计数（R10 线格式不变，音频帧无 path 位）。
- seq 每连接单调递增，音频帧与事件帧共享同一序号空间（Rust 每路一个计数器，
  所有上行帧统一编号）；跳号记丢帧并按收到的重同步。事件帧头部的 seq/ts 仅用于
  连续性校验，业务语义以 JSON 体内的 ts_ms/path 为准。
- segment_id 由 sidecar 按连接内 start 顺序分配（`seg_{n}`）；end 须匹配已打开段。
- 同 path 重复 start：旧段按 interrupted 丢弃（计数 + asr_error）。
- 有界（R13）：每连接待转写 ≤8（超则本段丢弃 + dropped_overload + 计数）；
  单段缓冲 ≤32MB（超则自动封段转写部分）；接收循环永不 await 转写；
  下行发送串行锁 + 5s 超时，超时/断开即清理连接。
- 内容无关：本模块零打印；转写文本只进下行 JSON（R14）。

鉴权（R9）：Authorization Header，缺失/错误 → close code=1008（accept 之前）。
"""

import asyncio
import secrets
import struct

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from asr.provider import ASRError
from core.auth import get_token

router = APIRouter()

FRAME_HEADER = struct.Struct("<B H I")  # type u8 | seq u16le | ts_ms u32le
AUDIO_FRAME = 0x00
EVENT_FRAME = 0x01
AUDIO_SAMPLES = 480
AUDIO_PAYLOAD_BYTES = AUDIO_SAMPLES * 4
AUDIO_FRAME_BYTES = FRAME_HEADER.size + AUDIO_PAYLOAD_BYTES  # 1927
VALID_PATHS = ("loopback", "mic")
MAX_PENDING = 8
MAX_SEGMENT_BYTES = 32 * 1024 * 1024
SEND_TIMEOUT_S = 5.0

_PROVIDER = None


def set_asr_provider(provider) -> None:
    """注入 ASR provider（测试/1b-2 接线用）。"""
    global _PROVIDER
    _PROVIDER = provider


def _new_counters() -> dict:
    return {
        "malformed": 0,
        "unsolicited_audio": 0,
        "seq_gap": 0,
        "violations": 0,
        "interrupted": 0,
        "dropped_overload": 0,
        "vad_heartbeat": 0,
    }


class _ConnDead(Exception):
    pass


class _Conn:
    __slots__ = ("ws", "alive", "path", "seg_counter", "open_seg",
                 "expected_seq", "in_flight", "counters", "send_lock")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.alive = True
        self.path = None
        self.seg_counter = 0
        self.open_seg = None
        self.expected_seq = 0
        self.in_flight = 0
        self.counters = _new_counters()
        self.send_lock = asyncio.Lock()


async def _send(conn: _Conn, payload: dict) -> None:
    try:
        async with conn.send_lock:
            await asyncio.wait_for(conn.ws.send_json(payload), SEND_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        conn.alive = False
        raise _ConnDead() from e


def _check_auth(websocket: WebSocket) -> bool:
    expected = get_token()
    if not expected:
        return False
    auth = websocket.headers.get("authorization", "")
    return secrets.compare_digest(auth, f"Bearer {expected}")


async def _ship(conn: _Conn, seg: dict) -> None:
    provider = _PROVIDER
    if provider is None:
        await _send(conn, {"type": "asr_error", "segment_id": seg["id"],
                           "error": "no_provider", "path": seg["path"],
                           "ts_ms": seg["ts_start"]})
        return
    if conn.in_flight >= MAX_PENDING:
        conn.counters["dropped_overload"] += 1
        await _send(conn, {"type": "asr_error", "segment_id": seg["id"],
                           "error": "dropped_overload", "path": seg["path"],
                           "ts_ms": seg["ts_start"]})
        return
    conn.in_flight += 1
    asyncio.create_task(_transcribe(conn, provider, seg))


async def _transcribe(conn: _Conn, provider, seg: dict) -> None:
    try:
        text = await provider.transcribe(bytes(seg["buf"]))
        await _send(conn, {"type": "asr_final", "segment_id": seg["id"],
                           "text": text, "path": seg["path"],
                           "ts_ms": seg["ts_start"],
                           "duration_ms": max(0, seg["ts_end"] - seg["ts_start"])})
    except ASRError as e:
        await _send(conn, {"type": "asr_error", "segment_id": seg["id"],
                           "error": e.kind, "path": seg["path"],
                           "ts_ms": seg["ts_start"]})
    except (_ConnDead, asyncio.CancelledError):
        raise
    except Exception:
        import traceback as _traceback

        _traceback.print_exc()  # 进 stderr（Rust 落盘可查）；客户端只见种类。
        await _send(conn, {"type": "asr_error", "segment_id": seg["id"],
                           "error": "provider_error", "path": seg["path"],
                           "ts_ms": seg["ts_start"]})
    finally:
        conn.in_flight -= 1


async def _auto_finalize(conn: _Conn) -> None:
    seg = conn.open_seg
    conn.open_seg = None
    if seg is None:
        return
    seg["ts_end"] = seg["ts_start"]
    await _ship(conn, seg)


async def _on_audio(conn: _Conn, ts_ms: int, payload: bytes) -> None:
    if len(payload) != AUDIO_PAYLOAD_BYTES:
        conn.counters["malformed"] += 1
        return
    seg = conn.open_seg
    if seg is None or conn.path is None:
        conn.counters["unsolicited_audio"] += 1
        return
    seg["buf"].extend(payload)
    if len(seg["buf"]) >= MAX_SEGMENT_BYTES:
        await _auto_finalize(conn)


async def _on_event(conn: _Conn, obj: dict) -> None:
    if not isinstance(obj, dict):
        conn.counters["malformed"] += 1
        return
    kind = obj.get("event")
    path = obj.get("path")
    ts_ms = obj.get("ts_ms")
    if kind not in ("segment_start", "segment_end", "vad_state"):
        conn.counters["malformed"] += 1
        return
    if path not in VALID_PATHS or not isinstance(ts_ms, int) or isinstance(ts_ms, bool):
        conn.counters["violations"] += 1
        return
    if kind == "vad_state":
        conn.counters["vad_heartbeat"] += 1
        return
    if conn.path is None:
        conn.path = path
    elif conn.path != path:
        conn.counters["violations"] += 1
        return
    if kind == "segment_start":
        if conn.open_seg is not None:
            conn.counters["interrupted"] += 1
            old = conn.open_seg
            conn.open_seg = None
            await _send(conn, {"type": "asr_error", "segment_id": old["id"],
                               "error": "interrupted", "path": old["path"],
                               "ts_ms": old["ts_start"]})
        conn.seg_counter += 1
        seg_id = f"seg_{conn.seg_counter}"
        conn.open_seg = {"id": seg_id, "path": path, "ts_start": ts_ms,
                         "ts_end": ts_ms, "buf": bytearray()}
        await _send(conn, {"type": "asr_start", "segment_id": seg_id,
                           "path": path, "ts_ms": ts_ms})
        return
    # segment_end
    seg = conn.open_seg
    if seg is None or seg["path"] != path:
        conn.counters["violations"] += 1
        return
    conn.open_seg = None
    seg["ts_end"] = ts_ms if isinstance(ts_ms, int) else seg["ts_start"]
    await _ship(conn, seg)


async def _on_frame(conn: _Conn, data: bytes) -> None:
    import json as _json

    if len(data) < FRAME_HEADER.size:
        conn.counters["malformed"] += 1
        return
    ftype, seq, ts_ms = FRAME_HEADER.unpack_from(data)
    obj = None
    if ftype == AUDIO_FRAME:
        if len(data) != AUDIO_FRAME_BYTES:
            conn.counters["malformed"] += 1
            return
    elif ftype == EVENT_FRAME:
        try:
            obj = _json.loads(data[FRAME_HEADER.size:].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            conn.counters["malformed"] += 1
            return
    else:
        conn.counters["malformed"] += 1
        return
    # 序号空间全帧共享：合法帧（长度/类型/JSON 均有效）才推进期望序号。
    if seq != conn.expected_seq:
        conn.counters["seq_gap"] += 1
    conn.expected_seq = seq + 1
    if ftype == AUDIO_FRAME:
        await _on_audio(conn, ts_ms, data[FRAME_HEADER.size:])
    else:
        await _on_event(conn, obj)


@router.websocket("/audio/stream")
async def audio_stream(websocket: WebSocket):
    if not _check_auth(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    conn = _Conn(websocket)
    try:
        while conn.alive:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            data = msg.get("bytes")
            if data is None:
                conn.counters["malformed"] += 1
                continue
            try:
                await _on_frame(conn, bytes(data))
            except _ConnDead:
                break
    finally:
        conn.alive = False