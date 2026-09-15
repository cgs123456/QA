"""答案生成路由：三路分派（固定 / LLM / Fail-Closed），与 Task 6 判定表对接。

- ≥0.75 直接返回（field 候选取 field_value；qa 候选取 official_answer），不调 LLM。
- 0.45–0.75：LLM 以 top1 为 context 流式生成；gap<0.15 时 context 含 top1+top2
  且 sources 同显两条——此为 PRD 未细化处的默认裁定，集中于本模块单点，
  后续可改纯展示模式（届时只动本文件）。
- <0.45 或无候选：Fail-Closed 固定文案，不调 LLM、不编造。
- LLM 异常（超时/连接/HTTP/协议）走 error 路径：返回失败文案，不降级自由回答。
"""

from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search
from retrieval.query_prep import prepare
from retrieval.hybrid_rank import decide, fuse
from retrieval.vector_search import vector_search

from .provider import SYSTEM_PROMPT, ProviderError

FAIL_CLOSED_TEXT = "知识库未命中"
ERROR_TEXT = "答案生成失败，请稍后重试。"


def _candidate_text(cand) -> str:
    if cand["type"] == "field":
        return cand["payload"].get("field_value") or ""
    return cand["payload"].get("official_answer") or ""


def _sources(items) -> list:
    """来源载荷：含贡献路由（UI 来源标签用；field 为 ["field"]）。"""
    return [
        {
            "type": c["type"],
            "key": c["key"],
            "score": c["score"],
            "routes": [r for r, s in c["s"].items() if s > 0],
            "payload": c["payload"],
        }
        for c in items
    ]


def select_contexts(decision: dict) -> tuple:
    """maybe 档 context 裁定单点：top1（gap<0.15 时含 top1+top2）。
    返回 (contexts, sources)；后续改纯展示模式只动本函数。"""
    items = decision["items"]
    return [_candidate_text(c) for c in items], _sources(items)


async def answer_stream(conn, store_id: str, question: str, llm, embed_fn):
    """流式答案事件：decision → [chunk*] → done；direct/fail_closed 无 chunk。

    llm 仅在 maybe 档被调用一次（计数见 answer()）；direct/fail_closed/error
    路径绝不触碰 llm（单测以调用计数断言）。
    """
    question = (question or "").strip()
    # 查询预处理一次、两路共用：字段路的包含匹配与 FTS 路的关键词必须同源
    # （各算一遍可能因分词抖动给出不同的关键词，那种分歧无法复现）。
    prep = prepare(conn, question) if question else None
    field = field_lookup(conn, store_id, question, prepared=prep) if question else []
    fts = fts5_search(conn, store_id, question, prepared=prep) if question else []
    vec = []
    warnings = []
    if question:
        try:
            vec = vector_search(conn, store_id, embed_fn([question])[0])
        except Exception as e:  # vec 路降级：不阻断其余三路（仅记错误种类）
            warnings.append(f"vec:{type(e).__name__}")
    fused = fuse(
        field,
        [h for h in fts if h["route"] == "jieba"],
        [h for h in fts if h["route"] == "simple"],
        vec,
    )
    decision = decide(fused)
    action = decision["action"]
    yield {"type": "decision", "action": action,
           "top1_score": decision["top1_score"], "warnings": warnings}

    if action == "direct":
        yield {"type": "done", "result": {
            "type": "direct",
            "text": _candidate_text(decision["items"][0]),
            "sources": _sources(decision["items"]),
            "llm_calls": 0,
        }}
        return

    if action == "fail_closed":
        yield {"type": "done", "result": {
            "type": "fail_closed",
            "text": FAIL_CLOSED_TEXT,
            "sources": [],
            "llm_calls": 0,
        }}
        return

    # maybe_single / maybe_multi：LLM 流式生成（默认裁定见模块 docstring）。
    contexts, sources = select_contexts(decision)
    yield {"type": "sources", "sources": sources}
    try:
        chunks = []
        async for chunk in llm.generate(SYSTEM_PROMPT, question, contexts):
            chunks.append(chunk)
            yield {"type": "chunk", "text": chunk}
    except ProviderError as e:
        yield {"type": "done", "result": {
            "type": "error",
            "text": ERROR_TEXT,
            "error": f"{e.kind}",
            "sources": sources,
            "llm_calls": 1,
        }}
        return
    yield {"type": "done", "result": {
        "type": "llm",
        "text": "".join(chunks),
        "sources": sources,
        "llm_calls": 1,
    }}


async def answer(conn, store_id: str, question: str, llm, embed_fn) -> dict:
    """收集流式事件，返回最终 result（含 llm_calls 计数）。"""
    result = {"type": "error", "text": ERROR_TEXT, "llm_calls": 0}
    async for event in answer_stream(conn, store_id, question, llm, embed_fn):
        if event["type"] == "done":
            result = event["result"]
    return result