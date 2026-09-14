"""evaluate_full 结构单测（stub embedding，无模型依赖）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite_vec

from knowledge.compiler import compile_store
from knowledge.stores import create_store
from tests.eval.loader import EvalItem
from tests.eval.runner import evaluate_full


def _unit(i, dim=512):
    v = [0.0] * dim
    v[i] = 1.0
    return sqlite_vec.serialize_float32(v)


def test_evaluate_full_structure(db):
    store = create_store(db, "fulltest")
    qa = [
        {"id": "f1", "standard_question": "红色警报问题",
         "official_answer": "红色答", "usage_status": "fixed", "category": "测试实体"},
        {"id": "f2", "standard_question": "蓝色警报问题",
         "official_answer": "蓝色答", "usage_status": "fixed", "category": "测试实体"},
    ]
    compile_store(db, store["id"], qa, [],
                  embeddings={"f1": _unit(0), "f2": _unit(1)})

    stub = lambda texts: [_unit(0)] * len(texts)  # noqa: E731
    items = [
        EvalItem(question="红色警报", expected_qa_id="f1"),
        EvalItem(question="今天天气怎么样？", expected_qa_id=None),
    ]
    s = evaluate_full(db, store["id"], items, stub)

    assert s["n"] == 2 and s["scored"] == 1 and s["null_items"] == 1
    for key in ("top3_rate", "top5_rate", "direct_rate", "fail_closed_rate",
                "null_fail_closed_rate"):
        assert 0.0 <= s[key] <= 1.0
    assert set(s["avg_ms"]) == {"field", "fts", "vec", "embed", "fuse"}
    assert all(v >= 0.0 for v in s["avg_ms"].values())
    assert set(s["actions"]) <= {"direct", "maybe_single", "maybe_multi", "fail_closed"}
    # stub 向量恒为 e1 → f1 的 vec 路确定性命中。
    assert any(d.get("n_vec", 0) >= 1 for d in s["details"])
    assert sum(s["actions"].values()) == 2
