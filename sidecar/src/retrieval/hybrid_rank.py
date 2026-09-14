"""归一化加权融合 + 最终判定（PRD §3.3 融合节与判定表；CTX 例外）。

模块名 hybrid_rank 系历史命名，实现不是 RRF：
  score = Σ w_i·s_i / Σ w_i，w=(field:3, jieba:1, simple:1, vec:1)，
  Σw=6.0 恒为分母，值域 [0,1]。

- 字段候选：type='field'（携 field_value），s_field 取命中 s（恒 1.0），
  其余三路记 0，与 qa 候选同池融合排序。
- 字段证据联动：字段命中 entity X 的 QA 候选（category == X）同时获得
  s_field=1.0——fields.entity 与 qa_pairs.category 同源（Markdown H1），
  这是「直接返回档隐含要求字段直查命中」得以成立的唯一机制：若无联动，
  0.75 将永远不可达（字段候选恒 0.5、纯模糊恒 ≤0.5），直接返回档死亡。
  联动是结构性的（有/无二值），强度仍由各路 s_i 表达；跨 entity 不联动，
  category 缺失不联动。标定阶段若需调整联动规则，记录在案。
- 输入 s 先钳入 [0,1]（防上游越界污染融合分值域）。
- 判定阈值（初值）：TH_DIRECT=0.75 / TH_MAYBE=0.45 / GAP=0.15。
"""

W_FIELD = 3.0
W_JIEBA = 1.0
W_SIMPLE = 1.0
W_VEC = 1.0
W_SUM = W_FIELD + W_JIEBA + W_SIMPLE + W_VEC  # 6.0

TH_DIRECT = 0.75
TH_MAYBE = 0.45
GAP = 0.15


def _clamp01(x) -> float:
    if x is None:
        return 0.0
    return min(1.0, max(0.0, float(x)))


def fuse(field_hits=(), jieba_hits=(), simple_hits=(), vec_hits=()) -> list:
    """四路候选同池融合，按 score 降序（并列按 type/key 保证确定性）。

    候选：{"type": "field"|"qa", "key", "score", "s": {field,jieba,simple,vec},
    载荷透传（field_value / standard_question / official_answer / route 等）}。
    同一 qa 多次命中同路时取该路最好 s。
    """
    pool: dict = {}

    def _slot(kind, key):
        if (kind, key) not in pool:
            pool[(kind, key)] = {
                "type": kind,
                "key": key,
                "s": {"field": 0.0, "jieba": 0.0, "simple": 0.0, "vec": 0.0},
                "payload": {},
            }
        return pool[(kind, key)]

    field_hits = list(field_hits or [])
    for h in field_hits:
        slot = _slot("field", h["field_id"])
        slot["s"]["field"] = max(slot["s"]["field"], _clamp01(h.get("s", 1.0)))
        slot["payload"] = {
            "entity": h.get("entity"),
            "field_name": h.get("field_name"),
            "field_value": h.get("field_value"),
        }
    linked_entities = {
        h.get("entity") for h in field_hits if h.get("entity") is not None
    }
    for route, hits in (
        ("jieba", jieba_hits),
        ("simple", simple_hits),
        ("vec", vec_hits),
    ):
        for h in hits or []:
            slot = _slot("qa", h["qa_id"])
            slot["s"][route] = max(slot["s"][route], _clamp01(h.get("s", 0.0)))
            if h.get("category") in linked_entities:
                slot["s"]["field"] = 1.0
            slot["payload"] = {
                "standard_question": h.get("standard_question"),
                "official_answer": h.get("official_answer"),
                "category": h.get("category"),
            }

    fused = []
    for slot in pool.values():
        s = slot["s"]
        score = (
            W_FIELD * s["field"]
            + W_JIEBA * s["jieba"]
            + W_SIMPLE * s["simple"]
            + W_VEC * s["vec"]
        ) / W_SUM
        fused.append({**slot, "score": score})
    fused.sort(key=lambda c: (-c["score"], c["type"], str(c["key"])))
    return fused


def decide(fused: list) -> dict:
    """最终判定。返回 {"action", "items", "top1_score"}，action ∈
    direct（≥0.75 直接返回）/ maybe_single（0.45–0.75 且 gap≥0.15，标注可能相关）/
    maybe_multi（0.45–0.75 且 gap<0.15，交用户判断）/ fail_closed（<0.45 或空表）。"""
    if not fused:
        return {"action": "fail_closed", "items": [], "top1_score": 0.0}
    top1 = fused[0]["score"]
    if top1 >= TH_DIRECT:
        return {"action": "direct", "items": [fused[0]], "top1_score": top1}
    if top1 < TH_MAYBE:
        return {"action": "fail_closed", "items": [], "top1_score": top1}
    gap = top1 - (fused[1]["score"] if len(fused) > 1 else 0.0)
    if gap >= GAP:
        return {"action": "maybe_single", "items": [fused[0]], "top1_score": top1}
    return {
        "action": "maybe_multi",
        "items": fused[:2],
        "top1_score": top1,
    }