"""体积回归：onedir 产物 vs docs/benchmark.md 基准，超标即非零退出（CI 用）。

用法：python scripts/check_size.py [--dist DIR] [--tolerance 1.15]
基线从 benchmark.md 的 BUNDLE_BYTES=<n> 行读取（Windows onedir 口径；
macOS/Linux 基线待首次打包后补记，届时按平台行扩展）。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_baseline() -> int:
    text = (ROOT / "docs" / "benchmark.md").read_text(encoding="utf-8")
    m = re.search(r"BUNDLE_BYTES=(\d+)", text)
    if not m:
        raise SystemExit("benchmark.md 缺 BUNDLE_BYTES 基准行")
    return int(m.group(1))


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist-sidecar" / "interviewcopilot-sidecar"))
    ap.add_argument("--tolerance", type=float, default=1.15)
    args = ap.parse_args()

    dist = Path(args.dist)
    if not dist.is_dir():
        raise SystemExit(f"产物目录不存在，先打包：{dist}")
    baseline = read_baseline()
    actual = dir_size(dist)
    limit = int(baseline * args.tolerance)
    print(f"baseline={baseline} actual={actual} limit={limit} "
          f"({actual / 1048576:.1f} MiB)")
    if actual > limit:
        raise SystemExit(f"体积回归失败：{actual} > {limit}（超 {args.tolerance}x 基准）")
    print("SIZE_REGRESSION_PASS")


if __name__ == "__main__":
    main()
