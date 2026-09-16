"""eval 骨架单测：loader 校验 + runner 在合成语料上跑通。

语料规模（2026-09-15 扩库）：seed_demo.json 由 8 QA 扩到 **108 QA + 29 字段**
（6 类目），题目由 59 补到 **100 题（87 scored + 13 应 Fail-Closed）**。
文件名 seed_demo 是历史遗留（首版只有 8 条 demo），扩库后它就是这个仓库的
唯一合成语料 —— 没有另建第二份语料文件，避免出现两个真相源。
"""

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
    assert len(items) == 100
    assert all(isinstance(it, EvalItem) for it in items)
    # null 题（应 Fail-Closed）占比必须留在 10~15/100 —— 这是评测集的口径约束，
    # 不是分数线：null 太少则红线没被压测，太多则命中率失去统计意义。
    n_null = sum(1 for it in items if it.expected_qa_id is None)
    assert 10 <= n_null <= 15
    assert sum(1 for it in items if "real" in it.tags) == 80

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"question": "", "expected_qa_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_eval_items(bad)


def test_frozen_set_has_no_duplicate_questions(tmp_path):
    """重复题 = 同一道题在指标里被计权两次。

    Top-3 / 拒答率都按"题数"口径统计，把某道题抄两遍就能放大或稀释某一类错误，
    评测集一旦冻结就不该再受这种操作影响。loader 已硬拒，这里再钉住
    当前冻结集本身是干净的，并覆盖"仅空白字符差异"也算重复。
    """
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    qs = [it.question for it in items]
    assert len(qs) == len(set(qs)), "冻结评测集出现重复题"

    dup = tmp_path / "dup.jsonl"
    dup.write_text(
        '{"question": "保修期多久", "expected_qa_id": "eval-001"}\n'
        '{"question": " 保修期多久 ", "expected_qa_id": "eval-001"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_eval_items(dup)


def test_every_expected_id_resolves_to_the_corpus():
    """题目引用的 expected_qa_id 必须真在语料里。

    扩库/出题是两份文件、两个动作：一旦题目引用了语料里不存在的 id，
    该题**永远不可能命中**，而 runner 不会报错 —— Top-3 会静默掉分，
    看起来像"检索变差了"。这条断言把这种静默失败变成红色。
    """
    seed = json.loads((EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8"))
    corpus_ids = {q["id"] for q in seed["qa_pairs"]}
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    dangling = sorted(
        {it.expected_qa_id for it in items if it.expected_qa_id is not None} - corpus_ids
    )
    assert dangling == [], f"题目引用了语料里不存在的 qa_id：{dangling}"


def test_runner_on_demo_seed(db):
    sid = _seed_demo(db)
    items = load_eval_items(EVAL_DIR / "questions_100.jsonl")
    summary = evaluate(db, sid, items)
    assert summary["n"] == 100
    assert summary["scored"] == 87
    assert summary["null_items"] == 13
    # 命中率 verdict 在 docs/eval-baseline.md（如实记录，不在单测里定分数线，
    # 防止“为过单测而调题/调阈值”的自我交易）；单测只锁结构与有效性。
    assert 0.0 <= summary["top3_rate"] <= 1.0
    assert 0.0 <= summary["top5_rate"] <= 1.0
    assert summary["top5_rate"] >= summary["top3_rate"]
    assert set(summary["avg_ms"]) == {"prep", "field", "fts"}
    missed = [
        (d["question"], d["ranked_top3"])
        for d in summary["details"]
        if d.get("scored") and not d["top3_hit"]
    ]
    print(f"\n[eval] top3={summary['top3_rate']:.3f} top5={summary['top5_rate']:.3f} "
          f"missed={len(missed)}")
    for q, ranked in missed:
        print(f"  MISS {q} -> {ranked}")
