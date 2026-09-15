"""真机验收：cloud embedding 重建耗时与成本实测（F5.3/F6.5）。

用法（有 key 才跑；无 key 记延期台账，不伪造数字）::

    python scripts/bench_embedding.py [--api-key $OPENAI_API_KEY] [--n 20]

key 解析顺序：--api-key > OPENAI_API_KEY 环境变量；缺失即判不可用，exit 2。
输出机器行：`EMBED_N=.. EMBED_S=.. TOKENS=.. DIM=3072`（耗时含限流退避等待；
TOKENS 为服务端 usage.prompt_tokens 求和，只记数不记文）。
有 key 的机器把该行落盘 docs/benchmark.md 即关闭台账。
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))

from retrieval.embedding import OpenAIEmbeddingProvider  # noqa: E402

SAMPLES = [
    "发货周期是多久？", "七天内无理由退货吗", "客服电话是多少",
    "公司成立于哪一年", "支持微信支付吗", "发票怎么开",
    "优惠券可以叠加吗", "收货地址填错了怎么办", "退货运费谁承担",
    "人工客服工作时间", "电子发票怎么开", "一单能用几张券",
    "公司总部在哪里", "质保期是多长", "分期付款支持吗",
    "发货前能改地址吗", "退款多久到账", "有没有纸质发票",
    "优惠券有效期多久", "周末发货吗",
]


async def _run(api_key: str, n: int) -> None:
    texts = (SAMPLES * ((n // len(SAMPLES)) + 1))[:n]
    provider = OpenAIEmbeddingProvider(api_key=api_key)
    t0 = time.perf_counter()
    vecs = await provider.embed(texts)
    dt = time.perf_counter() - t0
    assert len(vecs) == n and all(len(v) == 3072 for v in vecs)
    print(f"EMBED_N={n} EMBED_S={dt:.2f} TOKENS={provider.tokens_used} DIM=3072")
    print(f"PER_TEXT_MS={dt / n * 1000:.1f} TOKENS_PER_TEXT={provider.tokens_used / n:.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    api_key = (args.api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        print("EMBEDDING_DEFERRED=1 reason=no-key "
              "(--api-key / OPENAI_API_KEY 均为空，按 F5.3 要求记延期台账，不跑真机)")
        raise SystemExit(2)
    asyncio.run(_run(api_key, args.n))


if __name__ == "__main__":
    main()
