"""知识编译单测：解析 → 校验 → 入库三表行数内容正确。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items

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

SAMPLE_JSON = """{
  "售后政策": {"退货期限": "七天内可退", "运费承担": "买家承担首重"},
  "qa_pairs": [
    {"standard_question": "发货周期是多久？", "official_answer": "三天内发出", "usage_status": "fixed"},
    {"question": "支持哪些支付方式？", "answer": "支持微信和支付宝"}
  ]
}"""


def _compile_markdown(db, store_id):
    parsed = parse_markdown(SAMPLE_MD, "sample.md")
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    return compile_store(db, store_id, qa, fields, vocab_miss=miss)


def test_markdown_parser_rules():
    parsed = parse_markdown(SAMPLE_MD, "sample.md")
    assert [q["standard_question"] for q in parsed["qa_items"]] == [
        "退货期限是几天？",
        "客服电话是多少？",
    ]
    assert parsed["qa_items"][0]["official_answer"].startswith("七天内无理由退货")
    assert [(f["entity"], f["field_name"]) for f in parsed["field_items"]] == [
        ("公司信息", "公司成立时间"),
        ("公司信息", "发货周期"),
    ]
    assert parsed["field_items"][1]["field_value"] == "标准发货周期为三天内发出。"


def test_markdown_qa_blocks():
    parsed = parse_markdown(
        "# E\n## 联系\nQ：电话是多少？\nA：123。\n", "x.md"
    )
    assert len(parsed["qa_items"]) == 1
    assert parsed["qa_items"][0]["standard_question"] == "电话是多少？"
    assert parsed["qa_items"][0]["official_answer"] == "123。"


def test_json_parser_rules():
    import json

    parsed = parse_json(json.loads(SAMPLE_JSON))
    assert parsed["skipped"] == 0
    assert [(f["entity"], f["field_name"], f["field_value"]) for f in parsed["field_items"]] == [
        ("售后政策", "退货期限", "七天内可退"),
        ("售后政策", "运费承担", "买家承担首重"),
    ]
    assert [q["standard_question"] for q in parsed["qa_items"]] == [
        "发货周期是多久？",
        "支持哪些支付方式？",
    ]
    assert parsed["qa_items"][1]["official_answer"] == "支持微信和支付宝"


def test_markdown_import_three_tables(db):
    store = create_store(db, "md库")
    stats = _compile_markdown(db, store["id"])
    assert stats["qa_inserted"] == 2
    assert stats["fields_upserted"] == 2
    assert stats["vocab_miss"] == 0
    assert db.execute("SELECT COUNT(*) FROM qa_pairs").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM fields").fetchone()[0] == 2
    # 发货周期组 4 别名 + 公司成立时间组 3 别名 = 7。
    assert db.execute("SELECT COUNT(*) FROM field_aliases").fetchone()[0] == 7
    row = db.execute(
        "SELECT field_value FROM fields WHERE field_name='发货周期'"
    ).fetchone()
    assert row[0] == "标准发货周期为三天内发出。"
    assert db.execute(
        "SELECT COUNT(*) FROM field_aliases WHERE alias='多久发货'"
    ).fetchone()[0] == 1


def test_json_import_counts_and_vocab_miss(db):
    import json

    store = create_store(db, "json库")
    parsed = parse_json(json.loads(SAMPLE_JSON))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    assert miss == 1  # “运费承担”不在词表
    stats = compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    assert stats["qa_inserted"] == 2
    assert stats["fields_upserted"] == 2
    assert db.execute("SELECT COUNT(*) FROM field_aliases").fetchone()[0] == 3
    row = db.execute(
        "SELECT field_name FROM fields WHERE field_value='七天内可退'"
    ).fetchone()
    assert row[0] == "退货期限"  # 别名归一化
    assert db.execute(
        "SELECT COUNT(*) FROM session_events WHERE event_type='field_vocab_miss'"
    ).fetchone()[0] == 1


def test_conflict_overwrite_logs_event(db):
    store = create_store(db, "覆盖库")
    _compile_markdown(db, store["id"])
    db.execute(
        "UPDATE fields SET field_value='旧值' WHERE field_name='发货周期'"
    )
    db.commit()
    stats = _compile_markdown(db, store["id"])
    assert stats["qa_skipped"] == 2  # 相同问答跳过
    assert stats["field_overwritten"] == 2
    row = db.execute(
        "SELECT field_value FROM fields WHERE field_name='发货周期'"
    ).fetchone()
    assert row[0] == "标准发货周期为三天内发出。"
    events = db.execute(
        "SELECT metadata FROM session_events WHERE event_type='field_overwritten'"
    ).fetchall()
    assert events  # content-free：只记计数
    assert '"count": 2' in events[-1][0]
