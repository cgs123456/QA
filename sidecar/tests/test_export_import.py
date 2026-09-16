"""导出/恢复单测（P9 F4.5/F4.6/F11.6）。

- JSON：export→import（新库）→export 逐字节等价（store 信封 id 除外，
  属 DB 作用域身份）；同库覆盖恢复后重导出完全等价（含 store 块）。
- MD：规范内容 export→import→export 稳定；多段 value 截断、问答 H2 碰撞、
  别名重建均为显式锁定的已文档行为。
- 列清单锁定：导出列 == 线上 schema 列 − 合法排除；迁移加列不改清单即变红。
- CLI：子进程真跑（参数校验/错误码/字节一致）；打包脚本作用域不断言体积
  （重打 expensive），只锁定“主包构建脚本无 cli 引用”。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_SRC = ROOT / "sidecar" / "src"

sys.path.insert(0, str(SIDECAR_SRC))

from database.connection import connect
from database.schema import run_migrations
from knowledge import exporter
from knowledge.compiler import compile_store
from knowledge.exporter import (
    export_store_snapshot,
    render_markdown,
    restore_store,
    snapshot_to_json,
    write_json_snapshot,
)
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.stores import StoreNotFound, create_store
from knowledge.validator import validate_field_items, validate_qa_items

SEED_MD = """# 公司信息

## 公司成立时间

公司成立于2015年，总部位于北京。

## 发货周期

标准发货周期为三天内发出。

## 问答

本组存放问答条目。

## 退货期限是几天？

七天内无理由退货，运费由买家承担。

## 客服电话是多少？

客服电话是400-123-4567。
"""

# 非 ? 结尾问题走 Q/A 块；rejected/None-category/显式别名覆盖全字段。
SEED_QA = [
    {"standard_question": "发票怎么开", "official_answer": "备注抬头开电子票",
     "category": "公司信息", "usage_status": "fixed", "followup_logic": "见财务制度"},
    {"standard_question": "内部作废问题", "official_answer": "作废答案",
     "category": None, "usage_status": "rejected"},
]
SEED_FIELDS = [
    {"entity": "公司信息", "field_name": "客服电话", "field_value": "400-123-4567",
     "aliases": ["400电话", "客服热线"]},
]


def _seed(db, name="种子库"):
    store = create_store(db, name)
    parsed = parse_markdown(SEED_MD, "seed.md")
    qa, _ = validate_qa_items(parsed["qa_items"] + SEED_QA)
    fr, _ = validate_field_items(parsed["field_items"] + SEED_FIELDS)
    fields, miss = extract(fr, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return store["id"]


def _norm(snapshot):
    snap = json.loads(json.dumps(snapshot))
    snap["store"]["id"] = "STORE"
    return snap


def test_json_roundtrip_byte_equal(db, tmp_path):
    sid = _seed(db)
    snap1 = export_store_snapshot(db, sid)
    fresh = connect(str(tmp_path / "fresh.db"))
    run_migrations(fresh)
    try:
        # 同名恢复进新库：除 store.id（DB 作用域身份）外逐字节等价。
        restore_store(fresh, json.loads(json.dumps(snap1)), store_name="种子库")
        sid2 = fresh.execute("SELECT id FROM stores WHERE name=?", ("种子库",)).fetchone()[0]
        snap2 = export_store_snapshot(fresh, sid2)
        assert _norm(snap2) == _norm(snap1)  # 内容行 id 全保留
    finally:
        fresh.close()


def test_json_roundtrip_exact_bytes_with_same_name(db, tmp_path):
    """同名恢复进新库：除 store.id 外逐字节等价的字节级断言。"""
    sid = _seed(db)
    j1 = snapshot_to_json(export_store_snapshot(db, sid))
    fresh = connect(str(tmp_path / "fresh.db"))
    run_migrations(fresh)
    try:
        restore_store(fresh, json.loads(j1), store_name="种子库")
        sid2 = fresh.execute("SELECT id FROM stores WHERE name=?", ("种子库",)).fetchone()[0]
        d2 = export_store_snapshot(fresh, sid2)
        d1 = json.loads(j1)
        d1["store"]["id"] = d2["store"]["id"] = "X"
        assert json.dumps(d1, ensure_ascii=False, sort_keys=True) == \
            json.dumps(d2, ensure_ascii=False, sort_keys=True)
        # store 信封名一致时，只差 id。
        assert d1["store"]["name"] == d2["store"]["name"] == "种子库"
    finally:
        fresh.close()


def test_same_db_restore_regenerates_colliding_ids(db):
    """同库恢复到新 store：id 被占则换新（既有 compiler 策略），内容逐行一致。"""
    sid = _seed(db)
    snap1 = export_store_snapshot(db, sid)
    stats = restore_store(db, snap1, store_name="同库副本")
    assert stats["qa_inserted"] == 4 and stats["fields_upserted"] == 4
    sid2 = db.execute("SELECT id FROM stores WHERE name='同库副本'").fetchone()[0]
    snap2 = export_store_snapshot(db, sid2)
    assert [q["standard_question"] for q in snap2["qa_pairs"]] == \
        [q["standard_question"] for q in snap1["qa_pairs"]]
    assert [(f["entity"], f["field_name"], f["field_value"]) for f in snap2["fields"]] == \
        [(f["entity"], f["field_name"], f["field_value"]) for f in snap1["fields"]]
    assert {q["id"] for q in snap2["qa_pairs"]}.isdisjoint(q["id"] for q in snap1["qa_pairs"])


def test_export_deterministic(db):
    sid = _seed(db)
    assert snapshot_to_json(export_store_snapshot(db, sid)) == \
        snapshot_to_json(export_store_snapshot(db, sid))


def test_write_json_snapshot_matches_oneshot(db, tmp_path):
    sid = _seed(db)
    import io

    buf = io.StringIO()
    n = write_json_snapshot(db, sid, buf, batch=1)
    assert n == 8  # 4 QA + 4 field（含“问答”名字段）
    assert buf.getvalue() == snapshot_to_json(export_store_snapshot(db, sid))
    # 空库边界：空数组渲染与 json.dumps 一致。
    empty = create_store(db, "空库")
    buf2 = io.StringIO()
    assert write_json_snapshot(db, empty["id"], buf2) == 0
    assert buf2.getvalue() == snapshot_to_json(export_store_snapshot(db, empty["id"]))


def test_md_roundtrip_stable(db):
    sid = _seed(db)
    md1 = render_markdown(export_store_snapshot(db, sid))
    parsed = parse_markdown(md1, "r.md")
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    assert len(qa) == 4 and len(fields) == 4  # MD：2问答/3字段 + 附加 2QA/1field
    sid2 = create_store(db, "MD库")["id"]
    compile_store(db, sid2, qa, fields, vocab_miss=miss)
    assert render_markdown(export_store_snapshot(db, sid2)) == md1


def test_md_multipara_truncation_locked(db):
    """多段 value 的 MD 重导入只回首段：已文档化的有损边界，锁死防悄悄变。

    注：多段 value 只能直写 DB（parser 本来就只取首段，进不了库）。
    """
    sid = create_store(db, "多段库")["id"]
    qa, _ = validate_qa_items([])
    fr, _ = validate_field_items(
        [{"entity": "E", "field_name": "政策", "field_value": "第一段。\n\n第二段。"}]
    )
    fields, miss = extract(fr, load_vocab())
    compile_store(db, sid, qa, fields, vocab_miss=miss)
    md1 = render_markdown(export_store_snapshot(db, sid))
    assert "第二段" in md1  # 导出是全的
    parsed2 = parse_markdown(md1, "m.md")
    assert parsed2["field_items"][0]["field_value"] == "第一段。"


def test_missing_fields_tolerated(db):
    """部分列缺失容错：可选键（category/followup_logic/aliases/usage_status/
    template_id）缺席即取默认/空；非法 usage 记 invalid 不炸整批。
    注：快照用规范键（别名映射是 parser 层的职责，restore 直接走 validator）。"""
    sid = create_store(db, "容错库")
    snap = {
        "format": "interview-copilot-store",
        "version": 1,
        "store": {"name": "容错库"},
        "qa_pairs": [
            {"standard_question": "能退吗", "official_answer": "七天可退"},
            {"standard_question": "包邮吗", "official_answer": "满99包邮",
             "usage_status": "bogus"},
        ],
        "fields": [{"entity": "E", "field_name": "电话"}],
    }
    stats = restore_store(db, snap, store_name="容错库")
    assert stats["qa_inserted"] == 1  # 非法 usage_status 那条被 validator 记 invalid 丢弃
    assert stats["fields_upserted"] == 0  # 缺 field_value 同理
    row = db.execute(
        "SELECT usage_status, category FROM qa_pairs WHERE standard_question='能退吗'"
    ).fetchone()
    assert row[0] == "fixed" and row[1] is None


def test_overwrite_semantics(db):
    sid = _seed(db)
    snap = export_store_snapshot(db, sid)
    with __import__("pytest").raises(ValueError, match="非空"):
        restore_store(db, snap, store_id=sid)
    stats = restore_store(db, snap, store_id=sid, overwrite=True)
    assert stats["qa_inserted"] == 2 + 2 and stats["fields_upserted"] == 2 + 2
    # 覆盖后重导出完全等价（含 store 块：同库同 id）。
    assert snapshot_to_json(export_store_snapshot(db, sid)) == snapshot_to_json(snap)


def test_unknown_store_and_bad_snapshot(db):
    import pytest

    sid = _seed(db)
    snap = export_store_snapshot(db, sid)
    with pytest.raises(StoreNotFound):
        export_store_snapshot(db, "nope")
    with pytest.raises(StoreNotFound):
        restore_store(db, snap, store_id="nope")
    for bad in ({"format": "nope", "version": 1, "store": {}, "qa_pairs": [], "fields": []},
                {"format": "interview-copilot-store", "version": 99,
                 "store": {}, "qa_pairs": [], "fields": []},
                {"format": "interview-copilot-store", "version": 1},
                ["not", "a", "dict"]):
        with pytest.raises(ValueError):
            restore_store(db, bad, store_name="坏库")


def _live_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_export_covers_all_columns(db):
    """列清单锁定：清单 == 线上列 − 合法排除；迁移加列必须同步更新 exporter。"""
    from knowledge.exporter import (
        FIELD_EXPORT_COLUMNS,
        QA_EXPORT_COLUMNS,
        STORE_EXPORT_COLUMNS,
        _SCHEMA_EXCLUSIONS,
    )

    assert set(QA_EXPORT_COLUMNS) == set(_live_columns(db, "qa_pairs")) - _SCHEMA_EXCLUSIONS["qa_pairs"]
    assert set(FIELD_EXPORT_COLUMNS) == set(_live_columns(db, "fields")) - _SCHEMA_EXCLUSIONS["fields"]
    assert set(STORE_EXPORT_COLUMNS) == set(_live_columns(db, "stores")) - _SCHEMA_EXCLUSIONS["stores"]
    # 导出条目键集合与清单一致（加键忘 SELECT 即红）。
    sid = _seed(db)
    snap = export_store_snapshot(db, sid)
    assert set(snap["qa_pairs"][0]) == set(QA_EXPORT_COLUMNS)
    assert set(snap["fields"][0]) == set(FIELD_EXPORT_COLUMNS)
    assert set(snap["store"]) == set(STORE_EXPORT_COLUMNS)


def _file_db(tmp_path, name="cli.db"):
    """自有文件库（CLI 子进程用 --db 直指它；fixture db 路径拿不到）。"""
    from database.schema import run_migrations as _migrate

    conn = connect(str(tmp_path / name))
    _migrate(conn)
    return conn


def _run_cli(*args):
    # encoding 必须显式：父进程侧 text 解码走 locale（本机 GBK），
    # 不写则子进程 utf-8 中文输出直接炸 reader 线程、streams 变 None。
    # （子进程侧由 PYTHONIOENCODING=utf-8 保证 utf-8 输出。）
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "cli", *args], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=300)


def test_cli_export_stdout_matches_library(tmp_path):
    conn = _file_db(tmp_path)
    try:
        sid = _seed(conn)
        want = snapshot_to_json(export_store_snapshot(conn, sid))
    finally:
        conn.close()
    proc = _run_cli("export", "--store-id", sid, "--format", "json",
                    "--db", str(tmp_path / "cli.db"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == want


def test_cli_export_import_file_roundtrip(tmp_path):
    conn = _file_db(tmp_path)
    try:
        sid = _seed(conn)
    finally:
        conn.close()
    db_arg = str(tmp_path / "cli.db")
    out = tmp_path / "bak.json"
    assert _run_cli("export", "--store-name", "种子库", "--format", "json",
                    "--out", str(out), "--db", db_arg).returncode == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["qa_pairs"]) == 4
    proc = _run_cli("import", "--file", str(out), "--store-name", "CLI库",
                    "--db", db_arg)
    assert proc.returncode == 0, proc.stderr
    conn = connect(db_arg)
    try:
        got = conn.execute("SELECT COUNT(*) FROM qa_pairs WHERE store_id="
                           "(SELECT id FROM stores WHERE name='CLI库')").fetchone()[0]
        assert got == 4
    finally:
        conn.close()


def test_cli_arg_validation(tmp_path):
    db_arg = str(tmp_path / "cli.db")
    conn = _file_db(tmp_path)
    conn.close()
    # 未知格式 → argparse exit 2。
    bad = _run_cli("export", "--store-id", "x", "--format", "yaml", "--db", db_arg)
    assert bad.returncode == 2
    # 不存在的库 → exit 1 + stderr 说明。
    missing = _run_cli("export", "--store-id", "nope", "--db", db_arg)
    assert missing.returncode == 1
    assert "不存在" in missing.stderr
    # 缺文件 → exit 1。
    nofile = _run_cli("import", "--file", str(tmp_path / "ghost.json"),
                      "--store-name", "X", "--db", db_arg)
    assert nofile.returncode == 1
    # 无 --store-id/--store-name → exit 1（export 要求定位库）。
    neither = _run_cli("export", "--db", db_arg)
    assert neither.returncode == 1


def test_build_cli_script_scoped():
    """体积纪律：CLI 单独分发——主包构建脚本无 cli 引用；
    CLI 脚本自带独立产物目录与入口，不复用主包 dist。"""
    build_main = (ROOT / "sidecar" / "build.py").read_text(encoding="utf-8")
    assert "cli" not in build_main.lower()
    script = ROOT / "scripts" / "build-cli-win.ps1"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "cli/__main__.py" in text
    assert "dist-cli" in text  # 独立产物目录
    assert "--name" in text and "interview-copilot-cli" in text  # 独立包名
