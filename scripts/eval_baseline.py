"""D6 前基线：当前默认阈值下四路 + 融合 + 判定 → docs/eval-baseline.md。

- 阈值一律默认值（TH_DIRECT=0.75/TH_MAYBE=0.45/GAP=0.15，各路门限与权重），
  不改；调整按 PRD 标定顺序（s_i→w_i→阈值）并记录。
- 种子库：tests/eval/seed_demo.json（8 QA + 4 字段，QA 均带 category 以
  还原 Markdown 导入形态、激活字段联动）；embedding 批量推理在事务外，
  同一短事务写 qa+vec（R6 示范）。
- 需本地模型（sidecar/models/bge-small-zh-v1.5/），缺失即 loudly 失败。
"""

import datetime
import json
import sys
import tempfile
from pathlib import Path

import sqlite_vec

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))
sys.path.insert(0, str(ROOT / "sidecar" / "tests" / "eval"))

from database.connection import connect
from database.schema import run_migrations
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from loader import load_eval_items
from models.registry import BGE_SMALL_ZH, MODEL_REGISTRY, default_model_dir
from retrieval.embedder import Embedder
from retrieval.hybrid_rank import GAP, TH_DIRECT, TH_MAYBE
from runner import evaluate_full


def main() -> None:
    model_dir = default_model_dir(BGE_SMALL_ZH)
    missing = [f for f in ("model.onnx", "model.onnx_data", "tokenizer.json")
               if not (model_dir / f).is_file()]
    if missing:
        raise SystemExit(f"本地模型缺失：{missing}（先跑模型下载）")

    t0_all = __import__("time").perf_counter()
    embedder = Embedder(model_dir)
    tmp = Path(tempfile.mkdtemp(prefix="baseline-"))
    conn = connect(str(tmp / "base.db"))
    run_migrations(conn)
    store = create_store(conn, "baseline-demo")

    seed = json.loads((ROOT / "sidecar/tests/eval/seed_demo.json").read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa_items, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    field_items, miss = extract(fields_raw, load_vocab())

    texts = [q["standard_question"] for q in qa_items]
    vecs = embedder.embed(texts)  # 事务外批量推理（R6）
    for q in qa_items:
        q.setdefault("id", q["standard_question"])
    embeddings = {
        q["id"]: sqlite_vec.serialize_float32(v.tolist())
        for q, v in zip(qa_items, vecs)
    }
    # compile_store 按 question 匹配既有行、显式 id 透传，embeddings 对齐。
    stats = compile_store(conn, store["id"], qa_items, field_items,
                          vocab_miss=miss, embeddings=embeddings)

    items = load_eval_items(ROOT / "sidecar/tests/eval/questions_100.jsonl")
    summary = evaluate_full(conn, store["id"], items, embedder.embed)
    conn.close()

    date = datetime.date.today().isoformat()
    lines = [
        f"# 评测基线（四路 + 融合 + 判定，{date}）",
        "",
        "> D6 前基线：阈值一律默认值，未改。调整按 PRD 标定顺序（s_i→w_i→阈值）并记录。",
        "",
        "## 语料与模型",
        "",
        f"- 种子：tests/eval/seed_demo.json（8 QA + 4 字段；QA 均带 category=公司信息，还原 Markdown 形态）",
        f"- 题目：tests/eval/questions_100.jsonl（20 题：17 scored + 3 应 Fail-Closed），冻结前为示例",
        f"- embedding：{MODEL_REGISTRY[BGE_SMALL_ZH]['display']}（base {MODEL_REGISTRY[BGE_SMALL_ZH]['base_model']}，dim 512，L2 归一化，事务外批量）",
        f"- 入库统计：qa_inserted={stats['qa_inserted']} fields_upserted={stats['fields_upserted']} vec_written={stats['vec_written']}",
        "",
        "## 阈值（默认）",
        "",
        "| 常量 | 值 |",
        "|---|---|",
        f"| TH_DIRECT | {TH_DIRECT} |",
        f"| TH_MAYBE | {TH_MAYBE} |",
        f"| GAP | {GAP} |",
        "| bm25 门限 | -0.5（→s=0.5，T_fts=0.5） |",
        "| vec 门限 | d<0.6（→s=0.7） |",
        "| w | field:3, jieba:1, simple:1, vec:1（Σ=6.0） |",
        "",
        "## 基线数字",
        "",
        f"- Top-3：{summary['top3_rate']:.3f}；Top-5：{summary['top5_rate']:.3f}（{summary['scored']} scored）",
        f"- 直接返回率：{summary['direct_rate']:.3f}；Fail-Closed 率：{summary['fail_closed_rate']:.3f}",
        f"- null 题 Fail-Closed 率：{summary['null_fail_closed_rate']:.3f}（{summary['null_items']} 题）",
        f"- 动作分布：{summary['actions']}",
        "- 各路平均延迟（ms）："
        + "；".join(f"{k}={v:.2f}" for k, v in summary["avg_ms"].items()),
        "",
        "## 逐题",
        "",
        "| 题目 | 期望 | top1 | 动作 | top3 |",
        "|---|---|---|---|---|",
    ]
    for d in summary["details"]:
        top1 = d.get("top1")
        top1s = f"{top1['type']}:{str(top1['key'])[:12]}@{top1['score']:.3f}" if top1 else "-"
        lines.append(
            f"| {d['question']} | {d.get('expected_qa_id') or 'null(应拒答)'} | {top1s}"
            f" | {d.get('action', '-')} | {d.get('top3_hit', '-')} |"
        )
    lines += [
        "",
        f"（tmp 库已弃：{tmp}；全量耗时 {__import__('time').perf_counter() - t0_all:.1f}s）",
        "",
    ]
    out = ROOT / "docs" / "eval-baseline.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:24]))
    print(f"WROTE {out}")


if __name__ == "__main__":
    main()
