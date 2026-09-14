"""WS 音频协议单测（R9–R14）：鉴权/帧格式/分段/下行 schema/背压/内容无关。"""

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

import routers.audio as audio_mod
from app import app
from asr.provider import ASRError, StubASRProvider
from core.auth import init_token

TEST_TOKEN = "test-token-" + "x" * 32
SECRET_TEXT = "绝密转写内容甲乙丙"

init_token(TEST_TOKEN)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _stub_provider():
    audio_mod.set_asr_provider(StubASRProvider(text=SECRET_TEXT))
    audio_mod._LAST_COUNTERS = None
    yield
    audio_mod.set_asr_provider(StubASRProvider(text=SECRET_TEXT))
    audio_mod._LAST_COUNTERS = None


def _headers(token=TEST_TOKEN):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _audio(seq, ts, fill=0.0):
    return struct.pack("<BHI", 0x00, seq, ts) + struct.pack("<480f", *([fill] * 480))


def _event(seq, ts, obj):
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


def _start(seq, ts, path="loopback"):
    return _event(seq, ts, {"event": "segment_start", "ts_ms": ts, "path": path})


def _end(seq, ts, path="loopback", duration=None):
    obj = {"event": "segment_end", "ts_ms": ts, "path": path}
    if duration is not None:
        obj["duration_ms"] = duration
    return _event(seq, ts, obj)


def test_auth_missing_close_1008():
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/audio/stream", headers={}):
            pass
    assert exc.value.code == 1008


def test_auth_wrong_close_1008():
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/audio/stream", headers=_headers("wrong")):
            pass
    assert exc.value.code == 1008


def test_loopback_segment_happy_path():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 1000))
        assert ws.receive_json() == {"type": "asr_start", "segment_id": "seg_1",
                                     "path": "loopback", "ts_ms": 1000}
        ws.send_bytes(_audio(1, 1030, 0.1))
        ws.send_bytes(_audio(2, 1060, 0.2))
        ws.send_bytes(_end(3, 1120, duration=120))
        final = ws.receive_json()
        assert final["type"] == "asr_final"
        assert final["segment_id"] == "seg_1"
        assert final["text"] == SECRET_TEXT
        assert final["path"] == "loopback"
        assert final["ts_ms"] == 1000
        assert final["duration_ms"] == 120


def test_provider_call_gets_exact_segment_bytes():
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        a1 = _audio(1, 30, 0.5)
        a2 = _audio(2, 60, -0.5)
        ws.send_bytes(a1)
        ws.send_bytes(a2)
        ws.send_bytes(_end(3, 90))
        ws.receive_json()
    assert len(stub.calls) == 1
    size, rate = stub.calls[0]
    assert (size, rate) == (2 * 1920, 16000)


def test_mic_path_and_conflict_enforced():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0, "mic"))
        assert ws.receive_json()["path"] == "mic"
        # 同一连接上冲突路径的 start 被忽略：seq 照常推进但无新段。
        ws.send_bytes(_start(1, 10, "loopback"))
        ws.send_bytes(_audio(2, 40, 0.1))
        ws.send_bytes(_end(3, 70, "mic"))
        final = ws.receive_json()
        assert (final["segment_id"], final["path"]) == ("seg_1", "mic")


def test_malformed_frames_connection_survives():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(b"\x00\x01")  # 过短
        ws.send_bytes(struct.pack("<BHI", 0x02, 0, 0) + b"junk")  # 未知 type
        ws.send_bytes(struct.pack("<BHI", 0x00, 0, 0) + b"short")  # 音频长度错
        ws.send_text("plain text message")  # 非二进制帧
        ws.send_bytes(_start(0, 0))
        assert ws.receive_json()["type"] == "asr_start"
        ws.send_bytes(_audio(1, 30))
        ws.send_bytes(_end(2, 60))
        assert ws.receive_json()["type"] == "asr_final"


def test_seq_gap_resyncs():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.3))
        ws.send_bytes(_audio(9, 60, 0.4))  # 跳号：丢帧计数 + 重同步
        ws.send_bytes(_audio(10, 90, 0.5))
        ws.send_bytes(_end(11, 120))
        assert ws.receive_json()["type"] == "asr_final"


def test_unsolicited_audio_dropped():
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_audio(0, 0, 0.9))  # 无打开段 → 丢弃
        ws.send_bytes(_start(1, 10))
        ws.receive_json()
        ws.send_bytes(_audio(2, 40, 0.1))
        ws.send_bytes(_end(3, 70))
        ws.receive_json()
    assert len(stub.calls) == 1
    assert stub.calls[0][0] == 1920


def test_interrupted_segment_errors():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        assert ws.receive_json()["segment_id"] == "seg_1"
        ws.send_bytes(_start(1, 50))  # 同 path 重复 start：旧段 interrupted
        err = ws.receive_json()
        assert err == {"type": "asr_error", "segment_id": "seg_1",
                       "error": "interrupted", "path": "loopback", "ts_ms": 0}
        assert ws.receive_json()["segment_id"] == "seg_2"
        ws.send_bytes(_end(2, 100))
        assert ws.receive_json()["segment_id"] == "seg_2"


def test_provider_error_event():
    audio_mod.set_asr_provider(
        StubASRProvider(fail_with=ASRError("provider_timeout", "t/o")))
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_end(1, 60))
        err = ws.receive_json()
        assert err["type"] == "asr_error"
        assert err["error"] == "provider_timeout"
        assert err["segment_id"] == "seg_1"


def test_empty_segment_final():
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 500))
        ws.receive_json()
        ws.send_bytes(_end(1, 500))
        final = ws.receive_json()
        assert final["type"] == "asr_final"
        assert final["duration_ms"] == 0


def test_overload_drops_with_error(monkeypatch):
    import asyncio as _asyncio
    import threading as _threading

    gate = _threading.Event()  # 跨线程安全；转写任务内经 executor 等待，不占事件循环

    class Gated(StubASRProvider):
        name = "gated"

        async def transcribe(self, pcm_f32, sample_rate=16000, path=None):
            self.calls.append((len(pcm_f32), sample_rate))
            self.paths.append(path)
            loop = _asyncio.get_running_loop()
            await loop.run_in_executor(None, gate.wait)
            return "g"

    monkeypatch.setattr(audio_mod, "MAX_PENDING", 2)
    audio_mod.set_asr_provider(Gated())
    try:
        with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
            for i in range(3):
                base = i * 10
                ws.send_bytes(_start(base, base * 10))
                ws.receive_json()  # asr_start
                ws.send_bytes(_end(base + 1, base * 10 + 30))
            # 前 2 段占满 in_flight（卡在 gate），第 3 段被丢弃（顺序确定）。
            third = ws.receive_json()
            assert third["error"] == "dropped_overload"
            assert third["segment_id"] == "seg_3"
            gate.set()  # 放行被卡任务（须在 with 内，否则关闭握手等它们）
            for _ in range(2):
                assert ws.receive_json()["type"] == "asr_final"
    finally:
        gate.set()


def test_r14_no_transcript_in_logs(capsys):
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.7))
        ws.send_bytes(_end(2, 60))
        ws.receive_json()
    out, err = capsys.readouterr()
    assert SECRET_TEXT not in out
    assert SECRET_TEXT not in err


# --- task-14 DoD ---------------------------------------------------------


def test_hundred_frames_buffer_matches_duration_within_one_frame():
    """DoD：100 帧后 segment_end → 缓冲恰好 3.0s 的 float32，且与 duration_ms 一致（±1 帧）。"""
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    frames = 100
    ts0 = 1000
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, ts0))
        ws.receive_json()  # asr_start
        for i in range(frames):
            ws.send_bytes(_audio(1 + i, ts0 + (i + 1) * audio_mod.FRAME_MS, 0.25))
        ws.send_bytes(_end(1 + frames, ts0 + frames * audio_mod.FRAME_MS))
        final = ws.receive_json()
    assert final["type"] == "asr_final"

    assert len(stub.calls) == 1
    size, rate = stub.calls[0]
    assert rate == 16000
    # 缓冲恰好是 100 帧 float32：字节数精确，不是"约等于"。
    assert size == frames * audio_mod.AUDIO_PAYLOAD_BYTES == 192_000

    # 由缓冲字节数反推时长，必须与下行 duration_ms 在 1 帧内一致。
    buf_ms = size * audio_mod.FRAME_MS // audio_mod.AUDIO_PAYLOAD_BYTES
    assert buf_ms == 3000
    assert final["duration_ms"] == 3000
    assert abs(final["duration_ms"] - buf_ms) <= audio_mod.FRAME_MS

    assert audio_mod.last_counters()["dropped_oldest"] == 0


def test_segment_buffer_bounded_drops_oldest(monkeypatch):
    """R13：单段缓冲有界，满则丢最旧 + 计数 + 每段只告警一次。"""
    cap = 5
    monkeypatch.setattr(audio_mod, "MAX_SEGMENT_FRAMES", cap)
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    sent = cap + 3
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()  # asr_start
        for i in range(sent):
            ws.send_bytes(_audio(1 + i, (i + 1) * audio_mod.FRAME_MS, 0.1))
        # 首次丢帧即告警（只有一次，后续丢帧只计数）。
        warn = ws.receive_json()
        assert warn == {"type": "asr_error", "segment_id": "seg_1",
                        "error": "segment_truncated", "path": "loopback", "ts_ms": 0}
        ws.send_bytes(_end(1 + sent, (sent + 1) * audio_mod.FRAME_MS))
        final = ws.receive_json()
    assert final["type"] == "asr_final"

    # 保留最近 cap 帧（丢的是最旧），段仍然闭合。
    assert len(stub.calls) == 1
    size, _ = stub.calls[0]
    assert size == cap * audio_mod.AUDIO_PAYLOAD_BYTES
    assert audio_mod.last_counters()["dropped_oldest"] == sent - cap == 3


def test_seq_gap_increments_counter_and_resyncs():
    """DoD：跳号 → 计数 +1，且连接继续可用（按收到的重同步）。"""
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.3))
        ws.send_bytes(_audio(9, 60, 0.4))   # 跳号 2..8
        ws.send_bytes(_audio(10, 90, 0.5))
        ws.send_bytes(_end(11, 120))
        assert ws.receive_json()["type"] == "asr_final"
    counters = audio_mod.last_counters()
    assert counters["seq_gap"] == 1
    assert counters["malformed"] == 0
    assert counters["violations"] == 0
    # 三帧都进了缓冲：跳号只告警，不丢数据。
    assert stub.calls[0][0] == 3 * audio_mod.AUDIO_PAYLOAD_BYTES


def test_vad_heartbeat_never_enters_audio_buffer():
    """DoD：vad_state 心跳帧不得进入音频缓冲。"""
    stub = StubASRProvider(text="x")
    audio_mod.set_asr_provider(stub)
    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        for i in range(5):
            ws.send_bytes(_event(1 + i, (i + 1) * 1000,
                                 {"event": "vad_state", "ts_ms": (i + 1) * 1000,
                                  "path": "loopback", "state": "silence"}))
        ws.send_bytes(_audio(6, 6000, 0.2))
        ws.send_bytes(_end(7, 6030))
        assert ws.receive_json()["type"] == "asr_final"
    assert stub.calls[0][0] == audio_mod.AUDIO_PAYLOAD_BYTES  # 只有 1 帧音频
    counters = audio_mod.last_counters()
    assert counters["vad_heartbeat"] == 5
    assert counters["violations"] == 0
