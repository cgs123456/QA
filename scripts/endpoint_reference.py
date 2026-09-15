"""C1a 参考端点的一次性调用入口（给 Rust 注入 E2E 用）。

用途：把**生产链路真实产出的逐帧 voiced 决策**交给参考实现
（`sidecar/src/audio_eval/endpoint.py::detect_segments`），拿回参考段列表，
让 Rust 侧比对"线上 segment_start/segment_end 的 ts_ms"与"参考边界"。

为什么要有这个脚本，而不是在 Rust 里重写一遍参考逻辑：
重写一遍就变成"我拿我的实现当标准"，跨语言复核的意义立刻归零。
参考实现只有一份，就在这里被调用。

为什么"同源"：喂进去的决策来自 Rust worker 的真实 VAD（
`CaptureService::with_decision_tap` 观察点），不是测试自己重跑一遍 VAD 猜出来的。
所以比的是**同一串决策**下的两个端点实现。

R14：本脚本只处理 0/1 决策与帧号，不接触音频与转写文本；stdout 只输出一行 JSON。

用法：
    python scripts/endpoint_reference.py --decisions-file <path>
    python scripts/endpoint_reference.py --decisions 000111000...
    echo -n 000111000... | python scripts/endpoint_reference.py

输出：
    {"frames": N, "segments": [[onset, offset], ...], "ms": [[start_ms, end_ms], ...]}

`ms` 用 `endpoint.py::to_ms`，与 Rust `Segment::start_ms/end_ms` 同口径
（end 为闭区间右端之后一帧的边界，所以 duration = end_ms - start_ms = 帧数 × 30ms）。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parse(text: str) -> list:
    """'0'/'1' 字符串（允许空白/换行）→ list[bool]。其它字符直接报错。"""
    decisions = []
    for ch in text:
        if ch in "01":
            decisions.append(ch == "1")
        elif ch.isspace():
            continue
        else:
            raise SystemExit(f"decisions 里出现非 0/1 字符: {ch!r}")
    return decisions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions-file", type=Path, default=None)
    ap.add_argument("--decisions", default=None)
    args = ap.parse_args()

    if args.decisions_file is not None:
        text = args.decisions_file.read_text(encoding="utf-8")
    elif args.decisions is not None:
        text = args.decisions
    else:
        text = sys.stdin.read()

    sys.path.insert(0, str(ROOT / "sidecar" / "src"))
    from audio_eval.endpoint import detect_segments, to_ms

    decisions = _parse(text)
    segs = detect_segments(decisions)
    print(
        json.dumps(
            {
                "frames": len(decisions),
                "voiced_frames": sum(1 for d in decisions if d),
                "segments": [[int(s), int(e)] for s, e in segs],
                "ms": [[int(a), int(b)] for a, b in (to_ms(s) for s in segs)],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
