"""知识库 CRUD（stores 表）。routers 与测试共用本模块，避免逻辑重复。

写串行（R3）：一切写函数内部持 database.connection.write_lock，
调用方（routers/测试）无需关心。
"""

import json
import uuid

from database.connection import write_lock


class StoreNotFound(Exception):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex


def create_store(conn, name: str, template_id=None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("知识库名称必填")
    sid = _new_id()
    with write_lock, conn:
        conn.execute(
            "INSERT INTO stores(id, name, template_id, is_current)"
            " VALUES (?, ?, ?, 0)",
            (sid, name, template_id),
        )
    return get_store(conn, sid)


def get_store(conn, store_id: str) -> dict:
    row = conn.execute(
        "SELECT id, name, template_id, created_at, is_current FROM stores WHERE id=?",
        (store_id,),
    ).fetchone()
    if row is None:
        raise StoreNotFound(store_id)
    return {
        "id": row[0],
        "name": row[1],
        "template_id": row[2],
        "created_at": row[3],
        "is_current": bool(row[4]),
        "qa_count": conn.execute(
            "SELECT COUNT(*) FROM qa_pairs WHERE store_id=?", (store_id,)
        ).fetchone()[0],
        "field_count": conn.execute(
            "SELECT COUNT(*) FROM fields WHERE store_id=?", (store_id,)
        ).fetchone()[0],
    }


def list_stores(conn) -> list:
    rows = conn.execute(
        "SELECT id, name, template_id, created_at, is_current FROM stores ORDER BY created_at"
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "id": row[0],
                "name": row[1],
                "template_id": row[2],
                "created_at": row[3],
                "is_current": bool(row[4]),
                "qa_count": conn.execute(
                    "SELECT COUNT(*) FROM qa_pairs WHERE store_id=?", (row[0],)
                ).fetchone()[0],
                "field_count": conn.execute(
                    "SELECT COUNT(*) FROM fields WHERE store_id=?", (row[0],)
                ).fetchone()[0],
            }
        )
    return out


def get_or_create_store(conn, store_id=None, store_name=None) -> dict:
    if store_id:
        return get_store(conn, store_id)
    if store_name:
        row = conn.execute(
            "SELECT id FROM stores WHERE name=?", (store_name.strip(),)
        ).fetchone()
        if row:
            return get_store(conn, row[0])
        return create_store(conn, store_name)
    raise ValueError("store_id 与 store_name 至少提供一个")


def set_current_store(conn, store_id: str) -> dict:
    """is_current 单选切换（同一短事务）。不存在 → StoreNotFound。"""
    with write_lock, conn:
        cur = conn.execute(
            "UPDATE stores SET is_current=1 WHERE id=?", (store_id,)
        ).rowcount
        if cur == 0:
            raise StoreNotFound(store_id)
        conn.execute("UPDATE stores SET is_current=0 WHERE id!=?", (store_id,))
    return get_store(conn, store_id)


def get_current_store_id(conn):
    row = conn.execute("SELECT id FROM stores WHERE is_current=1").fetchone()
    return row[0] if row else None


def delete_store(conn, store_id: str) -> None:
    """级联删除：vec 表无 FK 级联，需手工先清；qa/fields/aliases 由 FK+触发器处理。"""
    get_store(conn, store_id)  # 不存在即抛
    with write_lock, conn:
        conn.execute(
            "DELETE FROM vec_qa_local WHERE qa_id IN"
            " (SELECT id FROM qa_pairs WHERE store_id=?)",
            (store_id,),
        )
        conn.execute(
            "DELETE FROM vec_qa_cloud WHERE qa_id IN"
            " (SELECT id FROM qa_pairs WHERE store_id=?)",
            (store_id,),
        )
        conn.execute("DELETE FROM stores WHERE id=?", (store_id,))


def log_event(conn, event_type: str, metadata: dict | None = None) -> None:
    """content-free 事件：只记类型与计数，永不记问题/答案原文。"""
    conn.execute(
        "INSERT INTO session_events(event_type, metadata) VALUES (?, ?)",
        (event_type, json.dumps(metadata or {}, ensure_ascii=False)),
    )
