"""知识编译/列表/两级检索 API（全部鉴权，由 app.py 挂载时统一加 verify_token）。

GET /knowledge/search 为 DoD 驱动的最小 F2.1 骨架（字段直查 + FTS 双路，
无 LLM/融合——融合与答案生成是 D6 的事）。
"""

import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database.connection import get_connection
from knowledge import stores
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.validator import validate_field_items, validate_qa_items
from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search
from retrieval.query_prep import prepare

router = APIRouter()

_vocab_cache = None


def _conn():
    """DB 连接（测试 monkeypatch 本函数指向 tmp 库）。"""
    return get_connection()


def _vocab():
    global _vocab_cache
    if _vocab_cache is None:
        _vocab_cache = load_vocab()
    return _vocab_cache


class CompileBody(BaseModel):
    store_id: str | None = None
    store_name: str | None = None
    format: Literal["markdown", "json"]
    content: str
    filename: str | None = None


@router.post("/knowledge/compile")
def knowledge_compile(body: CompileBody):
    conn = _conn()
    try:
        store = stores.get_or_create_store(conn, body.store_id, body.store_name)
    except stores.StoreNotFound:
        raise HTTPException(status_code=404, detail="store not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if body.format == "markdown":
            parsed = parse_markdown(body.content, body.filename)
            skipped = 0
        else:
            try:
                data = json.loads(body.content)
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"JSON 解析失败：{e}")
            parsed = parse_json(data, body.filename)
            skipped = parsed.get("skipped", 0)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    qa_items, qa_stats = validate_qa_items(parsed["qa_items"])
    field_raw, field_stats = validate_field_items(parsed["field_items"])
    field_items, vocab_miss = extract(field_raw, _vocab())
    stats = compile_store(
        conn, store["id"], qa_items, field_items, vocab_miss=vocab_miss
    )
    stats.update(
        {
            "qa_invalid": qa_stats["invalid"],
            "qa_duplicate_dropped": qa_stats["duplicate_dropped"],
            "field_invalid": field_stats["invalid"],
            "field_duplicate_dropped": field_stats["duplicate_dropped"],
            "parse_skipped": skipped,
            "vocab_miss": vocab_miss,
        }
    )
    return {"store_id": store["id"], "stats": stats}


@router.get("/knowledge/list")
def knowledge_list():
    return stores.list_stores(_conn())


@router.get("/knowledge/search")
def knowledge_search(q: str, store_id: str | None = None):
    """两级检索：字段直查 + FTS 双路，附各路服务端耗时（评测/验收用）。"""
    conn = _conn()
    if store_id is None:
        store_id = stores.get_current_store_id(conn)
        if store_id is None:
            raise HTTPException(status_code=400, detail="无当前知识库")
    else:
        try:
            stores.get_store(conn, store_id)
        except stores.StoreNotFound:
            raise HTTPException(status_code=404, detail="store not found")

    # 与生产回答路径同源：预处理一次、两路共用（诊断端点要能复现真实检索行为）。
    t0 = time.perf_counter()
    prep = prepare(conn, q)
    t_prep = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    field_hits = field_lookup(conn, store_id, q, prepared=prep)
    t_field = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    fts_hits = fts5_search(conn, store_id, q, prepared=prep)
    t_fts = (time.perf_counter() - t0) * 1000

    return {
        "store_id": store_id,
        "query": q,
        "keywords": list(prep.keywords),
        "field_hits": field_hits,
        "fts_hits": fts_hits,
        "timings_ms": {"prep": t_prep, "field": t_field, "fts": t_fts},
    }
