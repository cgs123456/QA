"""eval 骨架单测：loader 校验 + runner 在 demo 种子上跑通."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

EVAL_DIR = Path(__file__).resolve().parent

import pytest

from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from tests.eval.loader import EvalItem, load_eval_items
from tests.eval.runner import evaluate


def _seed_demo(db):
    store = create_store(db, "eval-demo")
    seed = json.loads((EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return store["id"]


def test_loader_schema_and_errors(tmp_path):
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    assert len(items) == 59
    assert all(isinstance(it, EvalItem) for it in items)
    assert sum(1 for it in items if it.expected_qa_id is None) == 10
    assert sum(1 for it in items if "real" in it.tags) == 39

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"question": "", "expected_qa_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_eval_items(bad)


def test_runner_on_demo_seed(db):
    sid = _seed_demo(db)
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    summary = evaluate(db, sid, items)
    assert summary["n"] == 59
    assert summary["scored"] == 49
    assert summary["null_items"] == 10
    # 命中率 verdict 在 docs/eval-baseline.md（如实记录，不在单测里定分数线，
    # 防止“为过单测而调题/调阈值”的自我交易）；单测只锁结构与有效性。
    assert 0.0 <= summary["top3_rate"] <= 1.0
    assert 0.0 <= summary["top5_rate"] <= 1.0
    assert summary["top5_rate"] >= summary["top3_rate"]
    assert set(summary["avg_ms"]) == {"field", "fts"}
    missed = [
        (d["question"], d["ranked_top3"])
        for d in summary["details"]
        if d.get("scored") and not d["top3_hit"]
    ]
    print(f"\n[eval] top3={summary['top3_rate']:.3f} top5={summary['top5_rate']:.3f} "
          f"missed={len(missed)}")
    for q, ranked in missed:
        print(f"  MISS {q} -> {ranked}")
