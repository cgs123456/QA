"""知识编译器主流程：解析后数据 → 校验 → 短事务入库 → 统计。

事务结构（R6 预留）：qa_pairs + fields + field_aliases（+ 未来 vec 行）
同属【一个短事务】；embedding 向量推理永远在事务外——调用方先算好经
`embeddings={qa_id: blob}` 传入，本函数只做写入（本次调用方传 None）。
"""

import json
import uuid

from database.connection import write_lock
from .stores import get_store, log_event


def _new_id() -> str:
    return uuid.uuid4().hex


def compile_store(
    conn,
    store_id: str,
    qa_items: list,
    field_items: list,
    stats_extra: dict | None = None,
    embeddings: dict | None = None,
    vocab_miss: int = 0,
) -> dict:
    """入库。返回 content-free 统计（只记行数）。"""
    get_store(conn, store_id)  # 不存在即抛 StoreNotFound

    existing_qa = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT standard_question, id, official_answer FROM qa_pairs"
            " WHERE store_id=?",
            (store_id,),
        ).fetchall()
    }
    existing_fields = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT entity, field_name, id FROM fields WHERE store_id=?",
            (store_id,),
        ).fetchall()
    }

    stats = {
        "qa_inserted": 0,
        "qa_updated": 0,
        "qa_skipped": 0,
        "fields_upserted": 0,
        "aliases_written": 0,
        "field_overwritten": 0,
        "vec_written": 0,
    }
    if stats_extra:
        stats.update(stats_extra)

    with write_lock, conn:  # 写串行（R3）之下的唯一短事务：qa + fields + aliases（+ vec）
        emb = dict(embeddings) if embeddings else {}
        taken_ids = {
            r[0] for r in conn.execute("SELECT id FROM qa_pairs").fetchall()
        }
        for item in qa_items or []:
            q = item["standard_question"]
            if q not in existing_qa:
                qid = item.get("id") or _new_id()
                if qid in taken_ids:
                    # 显式 id 已被他库占用（全局主键）：换新 id，embedding 跟随迁移。
                    # 同库重导走 update/skip 分支，不会到这里。
                    fresh = _new_id()
                    if qid in emb:
                        emb[fresh] = emb.pop(qid)
                    qid = fresh
                taken_ids.add(qid)
                conn.execute(
                    "INSERT INTO qa_pairs(id, store_id, standard_question,"
                    " official_answer, category, usage_status, followup_logic)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        qid, store_id, q, item["official_answer"],
                        item.get("category"), item["usage_status"],
                        item.get("followup_logic"),
                    ),
                )
                existing_qa[q] = (qid, item["official_answer"])
                stats["qa_inserted"] += 1
            else:
                qid, old_answer = existing_qa[q]
                if old_answer != item["official_answer"]:
                    conn.execute(
                        "UPDATE qa_pairs SET official_answer=?, category=?,"
                        " usage_status=?, followup_logic=? WHERE id=?",
                        (
                            item["official_answer"], item.get("category"),
                            item["usage_status"], item.get("followup_logic"), qid,
                        ),
                    )
                    existing_qa[q] = (qid, item["official_answer"])
                    stats["qa_updated"] += 1
                else:
                    stats["qa_skipped"] += 1
            if qid in emb:
                conn.execute(
                    "INSERT OR REPLACE INTO vec_qa_local(qa_id, embedding)"
                    " VALUES (?, ?)",
                    (qid, emb[qid]),
                )
                stats["vec_written"] += 1

        for item in field_items or []:
            key = (item["entity"], item["field_name"])
            if key in existing_fields:
                fid = existing_fields[key]
                conn.execute(
                    "UPDATE fields SET field_value=?, aliases=? WHERE id=?",
                    (item["field_value"], _aliases_json(item), fid),
                )
                stats["field_overwritten"] += 1
            else:
                fid = _new_id()
                conn.execute(
                    "INSERT INTO fields(id, store_id, entity, field_name,"
                    " field_value, aliases) VALUES (?,?,?,?,?,?)",
                    (
                        fid, store_id, item["entity"], item["field_name"],
                        item["field_value"], _aliases_json(item),
                    ),
                )
                existing_fields[key] = fid
            stats["fields_upserted"] += 1
            conn.execute("DELETE FROM field_aliases WHERE field_id=?", (fid,))
            for alias in item.get("aliases") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO field_aliases(field_id, alias)"
                    " VALUES (?, ?)",
                    (fid, alias),
                )
                stats["aliases_written"] += 1

        if stats["field_overwritten"]:
            log_event(conn, "field_overwritten", {"count": stats["field_overwritten"]})
        stats["vocab_miss"] = vocab_miss
        if vocab_miss:
            log_event(conn, "field_vocab_miss", {"count": vocab_miss})

    return stats


def _aliases_json(item) -> str:
    return json.dumps(item.get("aliases") or [], ensure_ascii=False)
