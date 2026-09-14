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
    assert len(items) == 20
    assert all(isinstance(it, EvalItem) for it in items)
    assert sum(1 for it in items if it.expected_qa_id is None) == 3

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"question": "", "expected_qa_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_eval_items(bad)


def test_runner_on_demo_seed(db):
    sid = _seed_demo(db)
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    summary = evaluate(db, sid, items)
    assert summary["n"] == 20
    assert summary["scored"] == 17
    assert summary["null_items"] == 3
    assert summary["top3_rate"] == 1.0, [
        (d["question"], d["ranked_top3"])
        for d in summary["details"]
        if d.get("scored") and not d["top3_hit"]
    ]
    assert summary["top5_rate"] == 1.0
    assert set(summary["avg_ms"]) == {"field", "fts"}
