"""迁移单测：user_version、触发器 insert/update/delete、幂等、vec 表可用。"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite_vec

from database.connection import connect
from database.schema import CURRENT_VERSION, run_migrations


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
    ).fetchall()
    return {r[0] for r in rows}


def test_migrate_to_v1(db):
    assert run_migrations(db) == CURRENT_VERSION == 1
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_expected_objects_exist(db):
    run_migrations(db)
    names = _tables(db)
    for expected in [
        "stores", "qa_pairs", "fields", "field_aliases", "ft_qa",
        "ft_qa_ai", "ft_qa_ad", "ft_qa_au",
        "vec_qa_local", "vec_qa_cloud", "session_events",
    ]:
        assert expected in names, f"缺失：{expected}"


def _seed(db):
    db.execute("INSERT INTO stores(id, name, is_current) VALUES ('s1','t',1)")
    db.execute(
        "INSERT INTO qa_pairs(id, store_id, standard_question, official_answer,"
        " usage_status) VALUES ('q1','s1','你的发货周期是多久','三天内发货','fixed')"
    )
    db.commit()


def test_trigger_insert_syncs_fts(db):
    run_migrations(db)
    _seed(db)
    rows = db.execute(
        "SELECT id FROM ft_qa WHERE ft_qa MATCH jieba_query(?)", ("发货周期",)
    ).fetchall()
    assert [r[0] for r in rows] == ["q1"]


def test_trigger_update_syncs_fts(db):
    run_migrations(db)
    _seed(db)
    db.execute(
        "UPDATE qa_pairs SET standard_question='退货期限是几天' WHERE id='q1'"
    )
    db.commit()
    assert (
        db.execute(
            "SELECT id FROM ft_qa WHERE ft_qa MATCH jieba_query(?)", ("退货期限",)
        ).fetchall()
        != []
    )
    assert (
        db.execute(
            "SELECT id FROM ft_qa WHERE ft_qa MATCH jieba_query(?)", ("发货周期",)
        ).fetchall()
        == []
    )


def test_trigger_delete_syncs_fts(db):
    run_migrations(db)
    _seed(db)
    db.execute("DELETE FROM qa_pairs WHERE id='q1'")
    db.commit()
    assert (
        db.execute(
            "SELECT id FROM ft_qa WHERE ft_qa MATCH jieba_query(?)", ("发货周期",)
        ).fetchall()
        == []
    )


def test_usage_status_check_constraint(db):
    run_migrations(db)
    db.execute("INSERT INTO stores(id, name) VALUES ('s1','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO qa_pairs(id, store_id, standard_question, official_answer,"
            " usage_status) VALUES ('q9','s1','q','a','bogus')"
        )


def test_migrations_idempotent(db):
    assert run_migrations(db) == 1
    assert run_migrations(db) == 1
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_vec_tables_writable(db):
    run_migrations(db)
    blob = sqlite_vec.serialize_float32([0.0] * 512)
    db.execute("INSERT INTO vec_qa_local(qa_id, embedding) VALUES ('q1', ?)", (blob,))
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 1
