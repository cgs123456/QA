"""task20 门槛1：CPU 整段路径分解耗时（固定答案 ≤4s / 生成答案 ≤6s，分路径记录）。

链路（说完 → 答案卡）= ASR(segment_end→asr_final) + 问答(ask→done)。
本机约束下的诚实分解（见 docs/acceptance-m2.md 门槛1节）：
- ASR：真实 faster-whisper base/int8/CPU + 真实 uvicorn + 真实 WS，
  音频为合成信号——时延有效、文本无意义（task15 口径，文本只记长度不记内容）。
- 问答直接路径：本机缺 vendor 二进制（libsimple.dll 取不到：GitHub 不通，
  P0 纪律停等投喂），QA 链跑不起来；引用 M1 同代码路径实测 ask→done 2.4ms
 （acceptance-m1 #9），合成总账 = ASR 实测 + 该常量 + 传输开销（已含在 ASR 段内）。
- 生成路径：Ollama 11434 拒连（M1 #10 同状），LLM 段不可测，记阻塞。

用法：
    python scripts/e2e_m2_gate1.py [--seconds 3] [--repeat 3] [--timeout 120]
输出：stdout 表格 + .workbuddy-ai/gate1_result.json（验收报告引用）。
"""

import argparse
import asyncio
import json
import os
import platform
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "sidecar" / "src"
SCRIPTS_DIR = ROOT / "scripts"
OUT_JSON = ROOT / ".workbuddy-ai" / "gate1_result.json"

sys.path.insert(0, str(SIDECAR_SRC))
sys.path.insert(0, str(SCRIPTS_DIR))

SAMPLE_RATE = 16000
FRAME_SAMPLES = 480
FRAME_MS = 30


def speech_like(seconds: float, seed: int = 0x5EED) -> list:
    """与 bench_asr_latency 同源的合成类语音（时延有效、文本无意义）。"""
    import math

    n = int(seconds * SAMPLE_RATE)
    state = seed | 1
    out = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 0.6 + 0.4 * math.sin(2 * math.pi * 3.5 * t)
        f0 = 120.0 + 20.0 * math.sin(2 * math.pi * 1.7 * t)
        s = 0.0
        for h in range(1, 13):
            f = f0 * h
            w = 1.0 if 300.0 <= f <= 900.0 else (0.5 if 1100.0 <= f <= 2800.0 else 0.08)
            s += w * math.sin(2 * math.pi * f * t)
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        noise = ((state >> 33) / (1 << 31)) - 1.0
        out.append(max(-1.0, min(1.0, 0.25 * env * s / 6.0 + 0.01 * noise)))
    return out


def frames_for(seconds: float):
    pcm = speech_like(seconds)
    total = len(pcm) // FRAME_SAMPLES
    frames = []
    for idx in range(total):
        chunk = pcm[idx * FRAME_SAMPLES:(idx + 1) * FRAME_SAMPLES]
        frames.append((struct.pack("<BHI", 0x00, 0, idx * FRAME_MS)
                       + struct.pack(f"<{FRAME_SAMPLES}f", *chunk)))
    return frames


def event(seq: int, ts: int, obj: dict) -> bytes:
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


async def one_asr_run(port: int, token: str, seconds: float, timeout_s: float):
    """一次真实转写：segment_start → N 帧 → segment_end，返回 (ms, final)。"""
    import websockets

    uri = f"ws://127.0.0.1:{port}/audio/stream"
    headers = {"Authorization": f"Bearer {token}"}
    frames = frames_for(seconds)
    try:
        cm = websockets.connect(uri, additional_headers=headers)
    except TypeError:
        cm = websockets.connect(uri, extra_headers=headers)
    async with cm as ws:
        seq, ts = 0, 0
        await ws.send(event(seq, ts, {"event": "segment_start",
                                      "path": "loopback", "ts_ms": ts}))
        seq += 1
        await ws.recv()
        for wire in frames:
            await ws.send(struct.pack("<BHI", 0x00, seq & 0xFFFF, 0) + wire[7:])
            seq += 1
        end_ts = int(seconds * 1000)
        t0 = time.perf_counter()
        await ws.send(event(seq, end_ts, {"event": "segment_end",
                                          "path": "loopback", "ts_ms": end_ts}))
        while True:
            obj = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
            if obj.get("type") in ("asr_final", "asr_error"):
                return (time.perf_counter() - t0) * 1000.0, obj
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--provider", choices=["faster-whisper", "paraformer", "sensevoice"],
                    default="faster-whisper",
                    help="整段路径不限定 provider（门槛只限 CPU 整段）；默认 provider 优先")
    args = ap.parse_args()

    import statistics

    import bench_asr_latency as bench

    token = "gate1-" + "z" * 32
    if args.provider == "faster-whisper":
        from asr.faster_whisper_provider import FasterWhisperProvider

        provider = FasterWhisperProvider()
        label = "faster-whisper/base/int8/cpu"
    elif args.provider == "paraformer":
        from asr.paraformer_provider import ParaformerProvider

        provider = ParaformerProvider()
        label = "paraformer/int8/cpu"
    else:
        from asr.sensevoice_provider import SenseVoiceProvider

        provider = SenseVoiceProvider()
        label = "sensevoice/int8/cpu"
    if not provider.is_ready():
        print("权重未就位", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    provider.warmup()
    cold_ms = (time.perf_counter() - t0) * 1000.0

    samples = []
    texts = []
    with bench.Server(provider, token) as srv:
        for _ in range(args.repeat):
            ms, obj = asyncio.run(one_asr_run(srv.port, token, args.seconds,
                                              args.timeout))
            assert obj.get("type") == "asr_final", obj
            samples.append(ms)
            texts.append(len(obj.get("text") or ""))

    import platform as _pf

    result = {
        "provider": label,
        "segment_s": args.seconds,
        "n": len(samples),
        "cold_load_ms": round(cold_ms, 1),
        "asr_median_ms": round(statistics.median(samples), 1),
        "asr_min_ms": round(min(samples), 1),
        "asr_max_ms": round(max(samples), 1),
        "hyp_text_len": texts,  # 合成音频，转写文本只记长度（R14 + 无意义声明）
        "direct_qa_ms_m1": 2.4,  # acceptance-m1 #9，同代码路径；本机 QA 链缺 vendor 跑不动
        "machine": (f"{_pf.system()} {_pf.machine()}, "
                    f"{os.environ.get('PROCESSOR_IDENTIFIER', '?')}, "
                    f"Python {_pf.python_version()}"),
    }
    # 合成总账：ASR 实测 + 直接路径常量（传输开销已含在 ASR 段内；VAD 判定在 Rust 侧，不在本区间）。
    result["fixed_total_ms"] = round(result["asr_median_ms"] + result["direct_qa_ms_m1"], 1)
    result["fixed_budget_ms"] = 4000
    result["fixed_pass"] = result["fixed_total_ms"] <= 4000

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    for k, v in result.items():
        print(f"{k}={v}")
    print(f"GATE1_FIXED_{'PASS' if result['fixed_pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
