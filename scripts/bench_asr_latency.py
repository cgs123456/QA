"""task15 ASR 时延实测：VAD end（segment_end 发出）→ asr_final（收到）。

为什么是"发 segment_end → 收 asr_final"：这正是端点状态机判出段结束到前端拿到文本的
用户可感区间，也是 PRD 要记进 `docs/benchmark.md` 的口径。VAD 判定本身不在此计时
（它在 Rust 侧，30ms/帧，属于 1b-4 的范畴）。

口径与限制（重要，勿误读）：
- 走**真实 uvicorn + 真实 WS 传输**，不是 TestClient——进程内客户端不反映真实链路
  （见 `real-transport-verification` 技能的实测记录）。
- 音频为**合成信号**（谐波堆 + 音节包络），不是真人语音。它足以驱动真实的算力路径，
  故**时延数字有效**；但**转写文本无意义**，不得据此判断识别质量。
  识别质量（中文样本 → 文本正确，人耳比对）需真实录音，属挂起的人工验收项。
- 冷加载（首次权重加载）单独计时，不计入 e2e——它在 sidecar 启动预热路径上，
  与"每段转写"是两笔账。

用法：
    python scripts/bench_asr_latency.py                     # 默认 3s/15s，各 2 次
    python scripts/bench_asr_latency.py --durations 3,15 --repeat 3
    python scripts/bench_asr_latency.py --provider stub     # 无权重时的冒烟自检
    python scripts/bench_asr_latency.py --out ../docs/benchmark.md
"""

import argparse
import asyncio
import json
import math
import statistics
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "sidecar" / "src"

SAMPLE_RATE = 16000
FRAME_SAMPLES = 480
FRAME_MS = 30


# ---------------------------------------------------------------- 合成音频

def speech_like(seconds: float, seed: int = 0x5EED) -> list:
    """谐波堆 + 音节包络的合成"类语音"。仅供驱动算力路径，不是真人语音。"""
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
    """→ [(wire_bytes, ts_ms)]，按 30ms/480 样本切。"""
    pcm = speech_like(seconds)
    total = len(pcm) // FRAME_SAMPLES
    frames = []
    for idx in range(total):
        chunk = pcm[idx * FRAME_SAMPLES:(idx + 1) * FRAME_SAMPLES]
        ts = idx * FRAME_MS
        frames.append((struct.pack("<BHI", 0x00, 0, ts) + struct.pack(f"<{FRAME_SAMPLES}f", *chunk), ts))
    return frames


def event(seq: int, ts: int, obj: dict) -> bytes:
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


# ---------------------------------------------------------------- 服务端

def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """真实 uvicorn，跑在后台线程。"""

    def __init__(self, provider, token: str):
        self.token = token
        self.port = free_port()
        sys.path.insert(0, str(SIDECAR_SRC))
        import app as app_module
        import routers.audio as audio_mod
        from core.auth import init_token

        init_token(token)
        app_module.HEALTH_NONCE = "bench"
        audio_mod.set_asr_provider(provider)

        import uvicorn

        self._config = uvicorn.Config(app_module.app, host="127.0.0.1",
                                      port=self.port, log_level="error")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=0.5) as r:
                    if r.status == 200:
                        return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("sidecar 未就绪（/health 未在 20s 内返回 200）")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)


# ---------------------------------------------------------------- 计时

async def one_run(port: int, token: str, seconds: float, timeout_s: float) -> float:
    """发 segment_start → N 帧 → segment_end，返回 (segment_end → asr_final) 毫秒。"""
    import websockets

    uri = f"ws://127.0.0.1:{port}/audio/stream"
    headers = {"Authorization": f"Bearer {token}"}
    frames = frames_for(seconds)
    try:
        cm = websockets.connect(uri, additional_headers=headers)
    except TypeError:  # websockets < 14
        cm = websockets.connect(uri, extra_headers=headers)

    async with cm as ws:
        seq = 0
        ts = 0
        await ws.send(event(seq, ts, {"event": "segment_start", "path": "loopback", "ts_ms": ts}))
        seq += 1
        await ws.recv()  # asr_start（段开启）
        for wire, fts in frames:
            await ws.send(struct.pack("<BHI", 0x00, seq & 0xFFFF, fts) + wire[7:])
            seq += 1
        end_ts = frames[-1][1] + FRAME_MS
        t0 = time.perf_counter()
        await ws.send(event(seq, end_ts, {"event": "segment_end", "path": "loopback", "ts_ms": end_ts}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            obj = json.loads(msg)
            if obj.get("type") in ("asr_final", "asr_error"):
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                return elapsed_ms, obj
    raise RuntimeError("unreachable")


def measure(port: int, token: str, durations, repeat: int, timeout_s: float) -> list:
    rows = []
    for seconds in durations:
        samples, last = [], None
        for _ in range(repeat):
            ms, obj = asyncio.run(one_run(port, token, seconds, timeout_s))
            samples.append(ms)
            last = obj
        rows.append({
            "seconds": seconds,
            "frames": int(seconds * 1000 / FRAME_MS),
            "n": len(samples),
            "median_ms": statistics.median(samples),
            "min_ms": min(samples),
            "max_ms": max(samples),
            "type": last.get("type"),
            "provider": last.get("provider"),
            "duration_ms": last.get("duration_ms"),
            "error": last.get("error"),
        })
    return rows


# ---------------------------------------------------------------- 输出

def render(rows, load_ms, provider_name, host_note) -> str:
    lines = []
    lines.append(f"**provider**: `{provider_name}`  ")
    lines.append(f"**冷加载（首次权重加载，不计入 e2e）**: {load_ms:.0f} ms  ")
    lines.append(f"**口径**: segment_end 发出 → asr_final 收到，真实 uvicorn + 真实 WS  ")
    lines.append(f"**音频**: 合成信号（谐波堆 + 音节包络）——时延有效，**文本无意义**  ")
    if host_note:
        lines.append(f"**环境**: {host_note}  ")
    lines.append("")
    lines.append("| 段时长 | 帧数 | 样本 | 中位 | 最小 | 最大 | 下行 | 报回 duration_ms |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        outcome = r["type"] if r["type"] == "asr_final" else f"{r['type']}({r['error']})"
        lines.append(
            f"| {r['seconds']:.1f}s | {r['frames']} | {r['n']} | "
            f"{r['median_ms']:.0f} ms | {r['min_ms']:.0f} ms | {r['max_ms']:.0f} ms | "
            f"{outcome} / {r['provider']} | {r['duration_ms']} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--durations", default="3,15", help="逗号分隔的段时长（秒）")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=180.0, help="单段等待上限（秒）")
    ap.add_argument("--provider", choices=["local", "stub"], default="local")
    ap.add_argument("--out", default=None, help="追加写入的 markdown 文件")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    durations = [float(x) for x in args.durations.split(",") if x.strip()]
    token = "bench-token-" + "z" * 32

    if args.provider == "stub":
        sys.path.insert(0, str(SIDECAR_SRC))
        from asr.provider import StubASRProvider

        provider = StubASRProvider(text="stub", name="stub")
        load_ms = 0.0
        provider_name = "stub"
    else:
        sys.path.insert(0, str(SIDECAR_SRC))
        from asr.faster_whisper_provider import FasterWhisperProvider

        provider = FasterWhisperProvider()
        if not provider.is_ready():
            print("权重未就位：先跑 `POST /model/download` 或 ensure_model_files()。",
                  file=sys.stderr)
            return 2
        t0 = time.perf_counter()
        provider.warmup()
        load_ms = (time.perf_counter() - t0) * 1000.0
        provider_name = f"{provider.name}/{provider.model}/{provider.compute_type}"

    import platform

    host_note = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"

    with Server(provider, token) as srv:
        rows = measure(srv.port, token, durations, args.repeat, args.timeout)

    body = render(rows, load_ms, provider_name, host_note)
    print(body)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = (ROOT / out).resolve()
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n" + (args.note or "") + "\n")
            f.write(body + "\n")
        print(f"\n[已追加到 {out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
