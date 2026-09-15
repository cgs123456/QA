"""人工验收：多 LLM 端到端流式（F6.1 全量，参数化 provider，出首字延迟）。

用法（可达才跑；不可达记延期台账，不伪造数字）::

    python scripts/e2e_llm.py --provider ollama [--model qwen2.5:7b]
    python scripts/e2e_llm.py --provider openai --api-key $OPENAI_API_KEY
    python scripts/e2e_llm.py --provider claude --api-key $ANTHROPIC_API_KEY
    python scripts/e2e_llm.py --provider gemini --api-key $GEMINI_API_KEY
    python scripts/e2e_llm.py --provider groq --api-key $GROQ_API_KEY
    python scripts/e2e_llm.py --provider custom --model http://127.0.0.1:8080/v1

key 解析顺序：--api-key > 同名环境变量 > 内存密钥库（本次进程内无推送，
故前两者缺失即判不可用，exit 2）。环境变量名：

- openai: OPENAI_API_KEY
- claude: ANTHROPIC_API_KEY（兼容 CLAUDE_API_KEY）
- gemini: GEMINI_API_KEY（兼容 GOOGLE_API_KEY）
- groq: GROQ_API_KEY
- custom: CUSTOM_API_KEY（可选，缺省用 custom-no-key 占位）

输出：chunk 带到达时间戳（证明逐块流式）；结尾打印机器行
`FIRST_TOKEN_S=<秒> TOTAL_S=<秒> ACTION=<direct|llm|...>`，
可达的 provider 把该行落盘 docs/benchmark.md（见 F6.1 延期台账节）。

embed：优先本地 bge（与 e2e_ollama 同口径）；缺模型则零向量降级
（answer_stream 内记 warnings，不阻断 LLM 首字计时——首字延迟只量
检索判定 + LLM 首 chunk，不含 embedding 冷加载）。
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))

from database.connection import connect
from database.schema import run_migrations
from generation.provider import get_provider, list_llm_providers
from generation.router import answer_stream
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items

ENV_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "custom": ("CUSTOM_API_KEY",),
    "ollama": (),
}


def _resolve_key(provider: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for env in ENV_KEYS.get(provider, ()):
        val = os.environ.get(env, "").strip()
        if val:
            return val
    return None


def _embed_fn():
    """本地 embedder，缺模型降级零向量（首字计时不受影响）。"""
    try:
        from models.registry import BGE_SMALL_ZH, default_model_dir
        from retrieval.embedder import Embedder

        model_dir = default_model_dir(BGE_SMALL_ZH)
        if not (model_dir / "model.onnx").is_file():
            raise RuntimeError("本地 embedding 模型缺失")
        emb = Embedder(model_dir)
        return emb.embed, "local-bge"
    except Exception as e:
        print(f"[warn] embed 降级为零向量（{type(e).__name__}: {e}），"
              "仅影响 vec 路，不影响 LLM 首字计时", flush=True)
        return lambda texts: [[0.0] * 512 for _ in texts], "zeros-fallback"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama", choices=list_llm_providers())
    ap.add_argument("--model", default=None, help="覆盖默认模型（custom 槽传 base_url）")
    ap.add_argument("--api-key", default=None, help="显式 key（缺省走环境变量）")
    ap.add_argument("--question", default="发货周期多久")
    args = ap.parse_args()

    provider_name = args.provider.strip().lower()
    api_key = _resolve_key(provider_name, args.api_key)
    if provider_name in ("openai", "claude", "gemini", "groq") and not api_key:
        print(f"[unreachable] provider={provider_name} 缺 API Key "
              f"（--api-key / {ENV_KEYS[provider_name]} 均为空），"
              "按 F6.1 要求记延期台账，不跑真机", flush=True)
        raise SystemExit(2)
    try:
        llm = get_provider(provider_name, model=args.model, api_key=api_key)
    except ValueError as e:
        print(f"[unreachable] {e}", flush=True)
        raise SystemExit(2)

    tmp = Path(tempfile.mkdtemp(prefix="e2e-llm-"))
    conn = connect(str(tmp / "e2e.db"))
    run_migrations(conn)
    store = create_store(conn, "e2e")
    seed = json.loads((ROOT / "sidecar/tests/eval/seed_demo.json").read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    compile_store(conn, store["id"], qa, fields, vocab_miss=miss)

    embed_fn, embed_src = _embed_fn()
    print(f"provider: {provider_name} model: {getattr(llm, 'model', args.model)} "
          f"embed: {embed_src}", flush=True)
    print(f"question: {args.question}", flush=True)
    t0 = time.perf_counter()
    first_token_s = None
    action = None
    try:
        async for event in answer_stream(conn, store["id"], args.question, llm, embed_fn):
            et = time.perf_counter() - t0
            kind = event["type"]
            if kind == "decision":
                action = event.get("action")
                print(f"\n[+{et:.2f}s decision] action={action} "
                      f"top1={event.get('top1_score')} warnings={event.get('warnings')}",
                      flush=True)
            elif kind == "sources":
                print(f"[+{et:.2f}s sources] n={len(event.get('sources', []))}", flush=True)
            elif kind == "chunk":
                if first_token_s is None:
                    first_token_s = et
                    print(f"\n[+{et:.2f}s FIRST_TOKEN]", flush=True)
                print(f"[+{et:.2f}s chunk] {event['text']}", end="", flush=True)
            else:
                result = event.get("result", {})
                action = result.get("type", action)
                print(f"\n[+{et:.2f}s done] type={result.get('type')} "
                      f"llm_calls={result.get('llm_calls')} "
                      f"error={result.get('error')} "
                      f"text={str(result.get('text', ''))[:200]}", flush=True)
    finally:
        conn.close()
    total_s = time.perf_counter() - t0
    if first_token_s is None:
        print(f"FIRST_TOKEN_S=null TOTAL_S={total_s:.2f} ACTION={action} "
              "(无 chunk：direct/fail_closed/error 路径属正常)", flush=True)
    else:
        print(f"\nFIRST_TOKEN_S={first_token_s:.2f} TOTAL_S={total_s:.2f} ACTION={action}",
              flush=True)


if __name__ == "__main__":
    asyncio.run(main())
