"""答案生成路由：三路分派（固定 / LLM / Fail-Closed），与 Task 6 判定表对接。

- ≥0.75 直接返回（field 候选取 field_value；qa 候选取 official_answer），不调 LLM。
- 0.45–0.75：LLM 以 top1 为 context 流式生成；gap<0.15 时 context 含 top1+top2
  且 sources 同显两条——此为 PRD 未细化处的默认裁定，集中于本模块单点，
  后续可改纯展示模式（届时只动本文件）。
- <0.45 或无候选：Fail-Closed 固定文案，不调 LLM、不编造。
- LLM 异常（超时/连接/HTTP/协议）走 error 路径：返回失败文案，不降级自由回答。
- 降级链（P8，`generation/fallback.py`）：maybe 档的 llm 可以是
  `FallbackLLMProvider`（首选→备选按序尝试）；结果带 `provider`（实际出力
  的那级）与 `degraded`（`["备选:kind", ...]` 逐级失败摘要，复用 asr_final
  的 degraded 模式，R14 内容无关）。direct/fail_closed 未触碰 LLM，
  故 `provider` 为 null、`degraded` 为 []。Fail-Closed 分支在链之前返回，
  拒绝不参与降级、不换模型重试。
"""

from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search
from retrieval.query_prep import prepare
from retrieval.hybrid_rank import decide, fuse
from retrieval.vector_search import has_vectors, vector_search

from .provider import SYSTEM_PROMPT, ProviderError

FAIL_CLOSED_TEXT = "知识库未命中"
ERROR_TEXT = "答案生成失败，请稍后重试。"


def _candidate_text(cand) -> str:
    if cand["type"] == "field":
        return cand["payload"].get("field_value") or ""
    return cand["payload"].get("official_answer") or ""


def _degraded_trail(llm) -> list:
    """llm 对象的决策轨迹 → 内容无关字符串列表（`provider:kind`，R14）。

    普通单 provider 没有 attempts/last_provider 属性 → 空 trail，
    与今日行为一致。
    """
    attempts = getattr(llm, "attempts", None) or []
    return [f"{n}:{k}" for n, k in attempts]


def _produced_by(llm):
    """实际出力的 provider 名（链取末级，单体取自身名，都没有则 None）。"""
    return getattr(llm, "last_provider", None) or getattr(llm, "name", None)


def _tried_calls(llm) -> int:
    """LLM 实际调用级数（单体恒 1，与今日 `llm_calls` 口径一致）。"""
    tried = getattr(llm, "tried", None)
    return tried if isinstance(tried, int) else 1


def _sources(items, hl_map=None) -> list:
    """来源载荷：含贡献路由（UI 来源标签用；field 为 ["field"]）。

    `hl_map`（F2.3）：`{qa_id: (hl_question, hl_answer)}`，由 answer_stream
    从 fts 原始命中里组装（fuse 只管分数，不管展示）。有标才带
    `hl_question` / `hl_answer` 键——无标时形状与今日逐字节一致。
    """
    out = []
    for c in items:
        entry = {
            "type": c["type"],
            "key": c["key"],
            "score": c["score"],
            "routes": [r for r, s in c["s"].items() if s > 0],
            "payload": c["payload"],
        }
        if c["type"] == "qa" and hl_map:
            hl = hl_map.get(c["key"])
            if hl:
                if hl[0]:
                    entry["hl_question"] = hl[0]
                if hl[1]:
                    entry["hl_answer"] = hl[1]
        out.append(entry)
    return out


def _hl_map(fts_hits) -> dict:
    """fts 原始命中 → `{qa_id: (hl_question|None, hl_answer|None)}`（首个有标者胜出）。"""
    merged: dict = {}
    for h in fts_hits or []:
        key = h.get("qa_id")
        if key is None or key in merged:
            continue
        merged[key] = (h.get("hl_question"), h.get("hl_answer"))
    return merged


def select_contexts(decision: dict, hl_map=None) -> tuple:
    """maybe 档 context 裁定单点：top1（gap<0.15 时含 top1+top2）。
    返回 (contexts, sources)；后续改纯展示模式只动本函数。"""
    items = decision["items"]
    return [_candidate_text(c) for c in items], _sources(items, hl_map)


async def answer_stream(conn, store_id: str, question: str, llm, embed_fn,
                      vec_table: str | None = None):
    """流式答案事件：decision → [chunk*] → done；direct/fail_closed 无 chunk。

    llm 仅在 maybe 档被调用一次（计数见 answer()）；direct/fail_closed/error
    路径绝不触碰 llm（单测以调用计数断言）。

    vec_table：vec 路查哪张表（None = 当前生效 provider 对应的表——重建完成前
    仍是旧表，故重建期间检索自动降级到旧向量 + FTS，不断链）。
    """
    question = (question or "").strip()
    if vec_table is None:
        from retrieval.embedding import active_table

        vec_table = active_table()
    # 查询预处理一次、两路共用：字段路的包含匹配与 FTS 路的关键词必须同源
    # （各算一遍可能因分词抖动给出不同的关键词，那种分歧无法复现）。
    prep = prepare(conn, question) if question else None
    field = field_lookup(conn, store_id, question, prepared=prep) if question else []
    fts = fts5_search(conn, store_id, question, prepared=prep) if question else []
    vec = []
    warnings = []
    vec_down = False
    if question:
        try:
            vec = vector_search(conn, store_id, embed_fn([question])[0],
                                table=vec_table)
            # 召回为空时再判一次：是"库里没向量"（路不可用）还是"没找到"（负证据）。
            vec_down = not vec and not has_vectors(conn, store_id, vec_table)
        except Exception as e:  # vec 路降级：不阻断其余三路（仅记错误种类）
            warnings.append(f"vec:{type(e).__name__}")
            vec_down = True
    # vec 挂了/还没建索引要把它的权重一起从分母里拿掉，否则剩余三路的天花板只有
    # (w_field+w_jieba+w_simple)/Σw ≈ 0.579 < TH_MAYBE，"降级"变成"全拒"。
    fused = fuse(
        field,
        [h for h in fts if h["route"] == "jieba"],
        [h for h in fts if h["route"] == "simple"],
        vec,
        unavailable=("vec",) if vec_down else (),
    )
    decision = decide(fused)
    action = decision["action"]
    yield {"type": "decision", "action": action,
           "top1_score": decision["top1_score"], "warnings": warnings}

    # 高亮映射与 fts 同源（F2.3）：fuse 只管分数，展示字段在此补回。
    hl = _hl_map(fts)
    if action == "direct":
        yield {"type": "done", "result": {
            "type": "direct",
            "text": _candidate_text(decision["items"][0]),
            "sources": _sources(decision["items"], hl),
            "llm_calls": 0,
            "provider": None,
            "degraded": [],
        }}
        return

    if action == "fail_closed":
        yield {"type": "done", "result": {
            "type": "fail_closed",
            "text": FAIL_CLOSED_TEXT,
            "sources": [],
            "llm_calls": 0,
            "provider": None,
            "degraded": [],
        }}
        return

    # maybe_single / maybe_multi：LLM 流式生成（默认裁定见模块 docstring）。
    contexts, sources = select_contexts(decision, hl)
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
            "llm_calls": _tried_calls(llm),
            "provider": None,
            "degraded": _degraded_trail(llm),
        }}
        return
    yield {"type": "done", "result": {
        "type": "llm",
        "text": "".join(chunks),
        "sources": sources,
        "llm_calls": _tried_calls(llm),
        "provider": _produced_by(llm),
        "degraded": _degraded_trail(llm),
    }}


async def answer(conn, store_id: str, question: str, llm, embed_fn) -> dict:
    """收集流式事件，返回最终 result（含 llm_calls 计数）。"""
    result = {"type": "error", "text": ERROR_TEXT, "llm_calls": 0}
    async for event in answer_stream(conn, store_id, question, llm, embed_fn):
        if event["type"] == "done":
            result = event["result"]
    return result