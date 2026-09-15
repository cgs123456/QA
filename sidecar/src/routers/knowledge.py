"""知识编译/列表/两级检索/P1 格式导入 API（全部鉴权，由 app.py 挂载时统一加 verify_token）。

GET /knowledge/search 为 DoD 驱动的最小 F2.1 骨架（字段直查 + FTS 双路，
无 LLM/融合——融合与答案生成是 D6 的事）。

P1 格式导入（dry-run → 确认 → 编译）：
- POST /knowledge/import/preview：解析文件 → 返回列映射提案（Excel，
  逐 sheet 独立）或条目预览（PDF）+ 每类样例，不写库。
- POST /knowledge/import/commit：携带用户确认后的映射，服务端重新解码、
  重解析、严格校验映射（不信任前端结构）后，走既有 compiler 短事务入库。
- 扫描件/图片型 PDF → 422 `{"error": "unsupported", ...}`（P1 语义：
  明确拒绝，不静默跳过）；文件损坏/超限/映射非法 → 400。
"""

import base64
import binascii
import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database.connection import get_connection
from knowledge import stores
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.excel_parser import (
    MAX_FILE_BYTES as EXCEL_MAX_BYTES,
)
from knowledge.parsers.excel_parser import (
    apply_excel_mapping,
    parse_excel_preview,
)
from knowledge.parsers.json_parser import parse_json
from knowledge.parsers.markdown_parser import parse_markdown
from knowledge.parsers.pdf_parser import (
    MAX_FILE_BYTES as PDF_MAX_BYTES,
)
from knowledge.parsers.pdf_parser import (
    UnsupportedPDFError,
    parse_pdf_items,
    parse_pdf_preview,
)
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


class ImportPreviewBody(BaseModel):
    format: Literal["excel", "pdf"]
    content_b64: str
    filename: str | None = None


class ImportCommitBody(BaseModel):
    store_id: str | None = None
    store_name: str | None = None
    format: Literal["excel", "pdf"]
    content_b64: str
    filename: str | None = None
    mapping: dict | None = None


def _decode_import_content(content_b64: str, fmt: str) -> bytes:
    """base64 解码 + 体积上限（preview/commit 同走，服务端不信任前端）。"""
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="文件内容不是合法 base64")
    cap = EXCEL_MAX_BYTES if fmt == "excel" else PDF_MAX_BYTES
    if len(raw) > cap:
        raise HTTPException(
            status_code=400, detail=f"文件过大（{len(raw)} 字节，上限 {cap}）")
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")
    return raw


def _compile_import_items(conn, store_id: str, qa_raw: list, field_raw: list,
                          parse_skipped: int, extra: dict | None = None) -> dict:
    """校验 → 别名展开 → 既有短事务编译（与 /knowledge/compile 同形状返回）。

    stats 额外键：parse_skipped / vocab_miss / qa_invalid 等（content-free，
    只记行数）；PDF 部分扫描页时另有 pdf_unsupported_pages。
    """
    qa_items, qa_stats = validate_qa_items(qa_raw)
    field_raw_ok, field_stats = validate_field_items(field_raw)
    field_items, vocab_miss = extract(field_raw_ok, _vocab())
    stats = compile_store(
        conn, store_id, qa_items, field_items, vocab_miss=vocab_miss)
    stats.update({
        "qa_invalid": qa_stats["invalid"],
        "qa_duplicate_dropped": qa_stats["duplicate_dropped"],
        "field_invalid": field_stats["invalid"],
        "field_duplicate_dropped": field_stats["duplicate_dropped"],
        "parse_skipped": parse_skipped,
        "vocab_miss": vocab_miss,
    })
    if extra:
        stats.update(extra)
    return {"store_id": store_id, "stats": stats}


@router.post("/knowledge/import/preview")
def knowledge_import_preview(body: ImportPreviewBody):
    """dry-run：返回映射提案/条目预览 + 样例，不写库。"""
    raw = _decode_import_content(body.content_b64, body.format)
    try:
        if body.format == "excel":
            return parse_excel_preview(raw, body.filename)
        return parse_pdf_preview(raw, body.filename)
    except UnsupportedPDFError as e:
        raise HTTPException(status_code=422, detail={
            "error": "unsupported", "reason": "scanned",
            "message": str(e), "pages": e.pages})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/knowledge/import/commit")
def knowledge_import_commit(body: ImportCommitBody):
    """确认导入：服务端重解码/重解析/重校验映射后短事务编译。"""
    conn = _conn()
    try:
        store = stores.get_or_create_store(conn, body.store_id, body.store_name)
    except stores.StoreNotFound:
        raise HTTPException(status_code=404, detail="store not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw = _decode_import_content(body.content_b64, body.format)
    try:
        if body.format == "excel":
            if body.mapping is None:
                raise ValueError("commit 映射必填（preview 只读提案，不做导入依据）")
            if not isinstance(body.mapping, dict):
                raise ValueError("commit 映射非法：须为对象")
            applied = apply_excel_mapping(raw, body.mapping)
            extra = {"neutralized_cells": applied.get("neutralized", 0)}
            return _compile_import_items(
                conn, store["id"], applied["qa_items"], applied["field_items"],
                applied["skipped"], extra)
        entity_override = None
        if body.mapping is not None:
            if (not isinstance(body.mapping, dict)
                    or set(body.mapping) - {"entity"}):
                raise ValueError("PDF 映射非法：仅允许 {\"entity\": \"...\"} 或省略")
            entity_override = body.mapping.get("entity")
        try:
            applied = parse_pdf_items(raw, body.filename, entity_override)
        except UnsupportedPDFError as e:
            raise HTTPException(status_code=422, detail={
                "error": "unsupported", "reason": "scanned",
                "message": str(e), "pages": e.pages})
        if not applied["qa_items"] and not applied["field_items"]:
            raise ValueError("PDF 未提取到任何条目（冒号字段/QA 均无），拒绝空导入")
        return _compile_import_items(
            conn, store["id"], applied["qa_items"], applied["field_items"],
            applied["skipped"],
            {"pdf_unsupported_pages": applied["unsupported_pages"]})
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
