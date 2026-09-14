"""两级检索单测：字段直查（s=1.0）+ FTS 双路（门限/归一化/rejected 过滤）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from retrieval.field_lookup import field_lookup, normalize_query
from retrieval.fts5_search import fts5_search, normalize_score

SAMPLE_MD = """# 公司信息

## 公司成立时间

公司成立于2015年，总部位于北京。

## 发货周期

标准发货周期为三天内发出。

## 退货期限是几天？

七天内无理由退货，运费由买家承担。

## 客服电话是多少？

客服电话是400-123-4567。
"""


def _seed(db):
    store = create_store(db, "检索库")
    parsed = parse_markdown(SAMPLE_MD, "sample.md")
    qa, _ = validate_qa_items(
        parsed["qa_items"]
        + [
            {
                "standard_question": "内部作废词XYZABC",
                "official_answer": "作废答案",
                "usage_status": "rejected",
            }
        ]
    )
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return store["id"]


def test_normalize_query():
    assert normalize_query("  发货周期 ") == "发货周期"
    assert normalize_query("FaHuo") == "fahuo"


def test_normalize_score():
    assert normalize_score(-0.5) == 0.5  # 门限处 s=0.5
    assert abs(normalize_score(-2.0) - 0.8) < 1e-9
    assert normalize_score(0.0) == 0.0


def test_field_lookup_exact_and_alias(db):
    sid = _seed(db)
    hits = field_lookup(db, sid, "发货周期")
    assert len(hits) == 1
    assert hits[0]["field_name"] == "发货周期"
    assert hits[0]["field_value"] == "标准发货周期为三天内发出。"
    assert hits[0]["s"] == 1.0
    assert hits[0]["source"] == "field"

    alias_hits = field_lookup(db, sid, "多久发货")
    assert [h["field_name"] for h in alias_hits] == ["发货周期"]

    assert field_lookup(db, sid, "不存在的字段XYZ") == []
    assert field_lookup(db, sid, "   ") == []


def test_fts_chinese_and_pinyin_hit(db):
    sid = _seed(db)
    hits = fts5_search(db, sid, "退货期限是几天")
    routes = {h["route"] for h in hits}
    assert "jieba" in routes
    top = [h for h in hits if h["route"] == "jieba"][0]
    assert top["standard_question"] == "退货期限是几天？"
    assert top["s"] >= 0.5

    py_hits = fts5_search(db, sid, "tuihuo")
    assert any(
        h["route"] == "simple" and h["standard_question"] == "退货期限是几天？"
        for h in py_hits
    )


def test_rejected_excluded(db):
    sid = _seed(db)
    assert fts5_search(db, sid, "内部作废词XYZABC") == []
    assert field_lookup(db, sid, "内部作废词XYZABC") == []


def test_top_k_respected(db):
    sid = _seed(db)
    hits = fts5_search(db, sid, "是", top_k=1)
    per_route: dict = {}
    for h in hits:
        per_route[h["route"]] = per_route.get(h["route"], 0) + 1
    assert all(v <= 1 for v in per_route.values())
