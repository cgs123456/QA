"""task20 门槛2：双路同时活跃 10 分钟——无丢段、无交叉污染、内存无泄漏增长。

范围诚实声明：Rust 采集侧（设备/重建/双路 ts 偏差）本机无 cargo 跑不了，
见 audio-regression.md §5–§6。本脚本测 **sidecar 侧**双路并发正确性与稳定性：
- 两条真实 WS 连接（path=loopback / mic），按真实 30ms 节拍各跑 600s，
  每路每 6s 一个段（5s 音频 + 1s 间隙）→ 每路 100 段；
- provider 用 HashProvider（全局注入）：转写返回 `path#sha256(pcm)[:16]`，
  既隔离“管线正确性”（本门槛目标），又不受推理算力波动污染计时；
  （推理容量是另一笔账：门槛1 已单列 faster-whisper/paraformer 实测。）
- 无丢段 = ends_sent == finals_received 且 segment_id 连续；
- 无交叉污染 = 每个 final 的 path 标签与 hash 必须匹配本路发送字节；
- 无泄漏增长 = 本进程 RSS：600s 内增长 <50MB（限额见 REPORT）。

用法：
    python scripts/e2e_m2_gate2_soak.py [--minutes 10] [--seg-s 5] [--gap-s 1]
输出：stdout 摘要 + .workbuddy-ai/gate2_result.json。
退出码：0=PASS，1=FAIL（任一硬条件不满足），2=环境错误。
"""

import argparse
import asyncio
import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "sidecar" / "src"
SCRIPTS_DIR = ROOT / "scripts"
OUT_JSON = ROOT / ".workbuddy-ai" / "gate2_result.json"

sys.path.insert(0, str(SIDECAR_SRC))
sys.path.insert(0, str(SCRIPTS_DIR))

FRAME_MS = 30
FRAME_SAMPLES = 480
RSS_GROWTH_BUDGET_MB = 50.0


class HashProvider:
    """确定性内容回显：text = f"{path}#{sha256(pcm).hexdigest()[:16]}"。

    finals 与发送字节逐段核对——任何串路/错路/错段都会导致 hash 失配。
    """

    name = "hash-soak"

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        return f"{path}#{hashlib.sha256(pcm_f32).hexdigest()[:16]}"


def make_frame(fill: float) -> bytes:
    return struct.pack(f"<{FRAME_SAMPLES}f", *([fill] * FRAME_SAMPLES))


def event(seq: int, ts: int, obj: dict) -> bytes:
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


async def drive_path(port: int, token: str, path: str, fill: float,
                     minutes: float, seg_s: float, gap_s: float,
                     result: dict) -> None:
    """一路的 10 分钟驱动：按绝对时刻表发帧（防 sleep 累积漂移），逐段核对。"""
    import websockets

    frames_per_seg = int(seg_s * 1000 / FRAME_MS)
    gap_frames = int(gap_s * 1000 / FRAME_MS)
    total_s = minutes * 60.0
    uri = f"ws://127.0.0.1:{port}/audio/stream"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        cm = websockets.connect(uri, additional_headers=headers)
    except TypeError:
        cm = websockets.connect(uri, extra_headers=headers)

    st = result[path]
    async with cm as ws:
        seq = 0
        seg_idx = 0
        t_start = time.perf_counter()
        deadline = t_start + total_s
        frame_no = 0  # 本路音频帧序号（ts 派生用，与 seq 区分：seq 含事件帧）

        async def paced_send(payload: bytes, at: float):
            delay = at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await ws.send(payload)

        while True:
            seg_idx += 1
            seg_t0 = t_start + (seg_idx - 1) * (seg_s + gap_s)
            if seg_t0 >= deadline:
                break
            ts_start = frame_no * FRAME_MS
            await paced_send(event(seq, ts_start,
                                   {"event": "segment_start", "path": path,
                                    "ts_ms": ts_start}), seg_t0)
            seq += 1
            got_start = json.loads(await ws.recv())
            assert got_start["type"] == "asr_start", got_start
            seg_id = got_start["segment_id"]
            pcm = bytearray()
            for i in range(frames_per_seg):
                f = make_frame(fill)
                pcm += f
                ts = (frame_no + 1) * FRAME_MS
                await paced_send(struct.pack("<BHI", 0x00, seq & 0xFFFF, ts) + f,
                                 seg_t0 + (i + 1) * FRAME_MS / 1000.0)
                seq += 1
            frame_no += frames_per_seg
            ts_end = frame_no * FRAME_MS
            t_end_sent = time.perf_counter()
            await ws.send(event(seq, ts_end,
                                {"event": "segment_end", "path": path,
                                 "ts_ms": ts_end}))
            seq += 1
            expected = f"{path}#{hashlib.sha256(bytes(pcm)).hexdigest()[:16]}"
            st["ends_sent"] += 1
            # 等本段 final（40s 上限；超时即丢段，记 fail 不死等）。
            try:
                while True:
                    obj = json.loads(await asyncio.wait_for(ws.recv(), timeout=40.0))
                    if obj.get("type") == "asr_final":
                        break
                    st["unexpected"].append(obj.get("type"))
            except asyncio.TimeoutError:
                st["timeouts"] += 1
                continue
            st["finals_received"] += 1
            if obj.get("segment_id") != seg_id:
                st["seg_id_mismatch"] += 1
            if obj.get("path") != path:
                st["path_mismatch"] += 1
            if obj.get("text") != expected:
                st["hash_mismatch"] += 1
                if len(st["mismatch_samples"]) < 5:
                    st["mismatch_samples"].append(
                        {"seg": seg_id, "got": obj.get("text"), "want": expected})
            st["e2e_ms"].append(round((time.perf_counter() - t_end_sent) * 1000.0, 1))
            # 间隙：静默 gap，不发帧只等时刻表（对端看就是停顿）。
            gap_until = seg_t0 + seg_s + gap_s
            delay = gap_until - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
    st["segments_done"] = seg_idx - 1


async def rss_sampler(pid: int, stop: asyncio.Event, out: list) -> None:
    import psutil

    proc = psutil.Process(pid)
    while not stop.is_set():
        out.append({"t": round(time.perf_counter(), 1),
                    "rss_mb": round(proc.memory_info().rss / (1 << 20), 1)})
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass
    out.append({"t": round(time.perf_counter(), 1),
                "rss_mb": round(proc.memory_info().rss / (1 << 20), 1)})


async def amain(args) -> dict:
    import bench_asr_latency as bench

    token = "gate2-" + "z" * 32
    result = {
        path: {"ends_sent": 0, "finals_received": 0, "timeouts": 0,
               "seg_id_mismatch": 0, "path_mismatch": 0, "hash_mismatch": 0,
               "unexpected": [], "mismatch_samples": [], "e2e_ms": [],
               "segments_done": 0}
        for path in ("loopback", "mic")
    }
    fills = {"loopback": 0.1, "mic": 0.7}
    rss_rows: list = []
    stop = asyncio.Event()
    with bench.Server(HashProvider(), token) as srv:
        base_rss = __import__("psutil").Process(os.getpid()).memory_info().rss / (1 << 20)
        sampler = asyncio.create_task(rss_sampler(os.getpid(), stop, rss_rows))
        try:
            await asyncio.gather(
                drive_path(srv.port, token, "loopback", fills["loopback"],
                           args.minutes, args.seg_s, args.gap_s, result),
                drive_path(srv.port, token, "mic", fills["mic"],
                           args.minutes, args.seg_s, args.gap_s, result),
            )
        finally:
            stop.set()
            await sampler
    result["_rss"] = {"base_mb": round(base_rss, 1), "samples": rss_rows}
    return result


def verdict(result: dict) -> tuple:
    fails = []
    for path in ("loopback", "mic"):
        st = result[path]
        if st["finals_received"] != st["ends_sent"]:
            fails.append(f"{path} 丢段：ends={st['ends_sent']} finals={st['finals_received']}")
        if st["timeouts"]:
            fails.append(f"{path} 超时 {st['timeouts']} 段")
        if st["seg_id_mismatch"] or st["path_mismatch"] or st["hash_mismatch"]:
            fails.append(f"{path} 污染：seg_id={st['seg_id_mismatch']} "
                         f"path={st['path_mismatch']} hash={st['hash_mismatch']}")
        if st["unexpected"]:
            fails.append(f"{path} 意外下行：{st['unexpected'][:5]}")
    rss = [r["rss_mb"] for r in result["_rss"]["samples"]]
    growth = (max(rss) - rss[0]) if rss else 0.0
    result["_rss"]["growth_mb"] = round(growth, 1)
    if growth >= RSS_GROWTH_BUDGET_MB:
        fails.append(f"内存增长 {growth:.1f}MB ≥ {RSS_GROWTH_BUDGET_MB}MB")
    return fails, growth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--seg-s", type=float, default=5.0)
    ap.add_argument("--gap-s", type=float, default=1.0)
    args = ap.parse_args()

    t0 = time.perf_counter()
    try:
        result = asyncio.run(amain(args))
    except Exception as e:  # 环境错误（非 verdict FAIL）：端口/依赖等
        print(f"ENV_ERROR: {type(e).__name__}: {e}")
        return 2
    wall = time.perf_counter() - t0
    fails, growth = verdict(result)
    result["_wall_s"] = round(wall, 1)
    result["_verdict"] = "PASS" if not fails else "FAIL"
    result["_fails"] = fails
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    for path in ("loopback", "mic"):
        st = result[path]
        e2e = st["e2e_ms"]
        med = sorted(e2e)[len(e2e) // 2] if e2e else -1
        print(f"{path}: segs={st['segments_done']} ends={st['ends_sent']} "
              f"finals={st['finals_received']} timeouts={st['timeouts']} "
              f"mismatch(seg/path/hash)={st['seg_id_mismatch']}/{st['path_mismatch']}/"
              f"{st['hash_mismatch']} e2e_med={med}ms")
    print(f"rss_base={result['_rss']['base_mb']}MB growth={growth:.1f}MB "
          f"budget={RSS_GROWTH_BUDGET_MB}MB wall={wall:.0f}s")
    print(f"GATE2_{result['_verdict']}")
    for f in fails:
        print(f"  FAIL: {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
