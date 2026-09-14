"""Rust↔sidecar 音频 WS 全链路 e2e 用的最小真实 ASGI 服务（task-14）。

用途：给 `src-tauri/tests/audio_ws_e2e.rs` 提供一个**真实 uvicorn** 对端，
以便验证「Rust 客户端 + 真实 FastAPI 路由」而不是只验证各自的单测替身。

与 `main.py` 的差异（有意为之，避免 e2e 依赖打包 vendor/DB）：
- 跳过 storage 初始化（不碰 sqlite 扩展与迁移）；
- 端口与 token 由调用方给定（而非随机 + stdout 握手）；
- 注入 `StubASRProvider`（不下载模型也能走到 asr_final）。

用法：
    python scripts/serve_audio_e2e.py --port 8123 --token <token> [--nonce <nonce>]
                                     [--max-segment-frames N]

`--max-segment-frames` 用于把 R13 的单段缓冲上限调小，使"丢最旧 + 告警"能在
几十帧内触发（默认 2000 帧 = 60s，测试里推不满，且客户端出站队列只有 128 帧）。
就绪判据：GET /health 返回 {"status":"ok","nonce":<nonce>}。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--nonce", default="audio-e2e")
    ap.add_argument("--max-segment-frames", type=int, default=None)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "sidecar" / "src"))

    import app as app_module  # noqa: E402
    import routers.audio as audio_mod  # noqa: E402
    from asr.provider import StubASRProvider  # noqa: E402
    from core.auth import init_token  # noqa: E402

    init_token(args.token)
    app_module.HEALTH_NONCE = args.nonce
    audio_mod.set_asr_provider(StubASRProvider(text="e2e-transcript"))
    if args.max_segment_frames is not None:
        audio_mod.MAX_SEGMENT_FRAMES = args.max_segment_frames

    import uvicorn  # noqa: E402

    uvicorn.run(
        app_module.app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
