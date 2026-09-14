"""人工验收：Ollama 端到端流式（检索→融合→判定→LLM chunk 到达）。

前置（有 Ollama 的机器）：
    ollama serve
    ollama pull qwen2.5:7b
    python scripts/e2e_ollama.py [--model qwen2.5:7b] [--question "发货周期多久"]

chunk 带到达时间戳打印，证明逐块流式（非缓冲后一次吐出）。
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))

from database.connection import connect
from database.schema import run_migrations
from generation.provider import OllamaProvider
from generation.router import answer_stream
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from retrieval.embedder import Embedder
from models.registry import BGE_SMALL_ZH, default_model_dir


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--question", default="发货周期多久")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="e2e-ollama-"))
    conn = connect(str(tmp / "e2e.db"))
    run_migrations(conn)
    store = create_store(conn, "e2e")
    seed = json.loads((ROOT / "sidecar/tests/eval/seed_demo.json").read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    compile_store(conn, store["id"], qa, fields, vocab_miss=miss)

    embedder = Embedder(default_model_dir(BGE_SMALL_ZH))
    llm = OllamaProvider(model=args.model)
    print(f"question: {args.question}", flush=True)
    t0 = time.perf_counter()
    async for event in answer_stream(conn, store["id"], args.question, llm,
                                     embedder.embed):
        et = time.perf_counter() - t0
        if event["type"] == "chunk":
            print(f"[+{et:.2f}s chunk] {event['text']}", end="", flush=True)
        else:
            print(f"\n[+{et:.2f}s {event['type']}] "
                  f"{str(event.get('result') or event.get('action') or '')[:200]}",
                  flush=True)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
