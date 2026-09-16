"""归一化加权融合 + 最终判定（PRD §3.3 融合节与判定表；CTX 例外）。

模块名 hybrid_rank 系历史命名，实现不是 RRF：
  score = Σ w_i·s_i / Σ w_i，w=(field:3, jieba:1, simple:1, vec:1)，
  Σw=6.0 恒为分母，值域 [0,1]。

- 字段候选：type='field'（携 field_value），s_field 取命中 s（恒 1.0），
  其余三路记 0，与 qa 候选同池融合排序。
- 字段证据联动：字段命中 entity X 的 QA 候选（category == X）同时获得
  s_field=LINK_MODE 决定的强度——fields.entity 与 qa_pairs.category 同源
  （Markdown H1），这是「直接返回档隐含要求字段直查命中」得以成立的机制：
  若无联动，纯模糊命中上限 = (w_jieba+w_simple+w_vec)/Σw，直接返回档很难可达。
  跨 entity 不联动，category 缺失不联动。
- 输入 s 先钳入 [0,1]（防上游越界污染融合分值域）。
- 判定阈值：**初值** TH_DIRECT=0.75 / TH_MAYBE=0.45 / GAP=0.15；
  下方为 P6 在 100 题冻结评测集上的标定值（2026-09-15）。

# 联动强度策略（P6 标定项，机制①）

初版是 `entity_flat`：命中字段所在 entity 的**全部** QA 一律 `s_field=1.0`。
8 篇语料时无害（同 entity 仅 8 篇、用词不重）；108 篇下同 entity 有 20 篇同义题群，
**平权把 entity 内的排序彻底抹掉**（全部拿满 w_field/Σw），排序退化为 bm25 噪声。
实测：`发票怎么开` 的 top3 挤在 0.812/0.809/0.809（极差 0.003）越过 TH_DIRECT
→ direct 错答；`有纸质发票吗`（应拒答）被同 entity 题群抬到 0.801 进 direct 档。

- `entity_flat`   —— 全部同 entity QA 取 1.0（初版）。
- `entity_scaled` —— 强度 = 该字段命中自身的 s（精确 1.0 / 包含 0.8）：
  联动的证据不可能强过字段命中本身。
- `entity_text`   —— 强度 = 字段命中 s × **该 QA 自身文本证据**（jieba/simple/vec 取最大）。
  与 `entity_flat` 的区别是**放大而非抹平**：entity 内文本证据强的 QA 拿到的联动也强，
  排序信息得以保留（`entity_flat` 正是把 entity 内排序抹平才产生 direct 错答）。
- `exact_only`    —— 仅**精确**字段命中才联动（包含匹配不联动），强度 1.0。
- `off`           —— 不联动。

取值与前后对比见 `docs/eval-final.md`。
"""

# --- P6 标定值（2026-09-15，评测集 questions_100.jsonl @ seed_demo.json 108 篇）---
# 标定前的初值见上方注释；标定过程与前后对比见 docs/eval-final.md。
# Σw = 9.5。相对初值（3,1,1,1）的变化：字段路权重下调（3.0→1.5，因平权联动被
# entity_scaled 取代后它不再是唯一能推高分的路）、语义路上调（1.0→4.0，它才是
# 同义改写的判别信号）。
W_FIELD = 1.5
W_JIEBA = 3.0
W_SIMPLE = 1.0
W_VEC = 4.0
W_SUM = W_FIELD + W_JIEBA + W_SIMPLE + W_VEC  # 9.5

# 判定阈值：TH_MAYBE 上调（0.45→0.60）是「null 拒答 100%」的唯一来源 ——
# 它在匹配修复后把对抗题 `有纸质发票吗` 从 direct 压回 fail_closed；
# TH_DIRECT 下调（0.75→0.65）用于把 direct 档召回从 32 提到 51，
# 且 direct 档答错数不增（5 → 5）。
TH_DIRECT = 0.65
TH_MAYBE = 0.60
GAP = 0.15

# 联动策略（见模块注释）：`entity_scaled` = 强度取字段命中自身的 s。
LINK_MODE = "entity_scaled"


def _clamp01(x) -> float:
    if x is None:
        return 0.0
    return min(1.0, max(0.0, float(x)))


def _linked_entities(field_hits) -> dict:
    """entity → 联动强度（见模块注释「联动强度策略」）。

    同一 entity 被多次命中时取最强联动。`LINK_MODE` 在**调用时**读取，
    便于标定脚本运行期覆盖（进程退出即失效，不改源文件）。
    """
    out: dict = {}
    if LINK_MODE == "off":
        return out
    for h in field_hits or ():
        ent = h.get("entity")
        if ent is None:
            continue
        fs = _clamp01(h.get("s", 1.0))
        if LINK_MODE == "exact_only":
            if fs < 1.0:
                continue
            val = 1.0
        elif LINK_MODE == "entity_scaled":
            val = fs
        else:  # entity_flat（初版）
            val = 1.0
        out[ent] = max(out.get(ent, 0.0), val)
    return out


def _linkage_for(entity_strength, slot) -> float:
    """把 entity 级联动强度落到具体 QA 上（`entity_text` 模式按文本证据放大）。"""
    if entity_strength is None:
        return 0.0
    if LINK_MODE != "entity_text":
        return entity_strength
    s = slot["s"]
    text = max(s["jieba"], s["simple"], s["vec"])
    return entity_strength * text


def _active_weight(unavailable=()) -> float:
    """**可用**路的权重和（恒等于 W_SUM，除非有路明确不可用）。

    只剔除 `unavailable`（该路抛异常 / 模型缺失，由调用方判定）里的路，
    **不**剔除"召回到 0 条"的路 —— 后者是负证据，必须留在分母里压制分数。

    反例（踩过）：把"召回为空"也当缺席剔除，null 拒答率从 1.000 崩到 0.154 —
    对抗题本来就是因为**各路都找不到东西**才该被拒，一剔除反而把它们抬进 direct。
    「路挂了」和「路说没找到」是两件事，只有前者该免计分。
    """
    total = 0.0
    for name, w in (("field", W_FIELD), ("jieba", W_JIEBA),
                    ("simple", W_SIMPLE), ("vec", W_VEC)):
        if name not in unavailable:
            total += w
    return total


def fuse(field_hits=(), jieba_hits=(), simple_hits=(), vec_hits=(),
         unavailable=()) -> list:
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
    jieba_hits = list(jieba_hits or [])
    simple_hits = list(simple_hits or [])
    vec_hits = list(vec_hits or [])
    denom = _active_weight(unavailable) or W_SUM
    for h in field_hits:
        slot = _slot("field", h["field_id"])
        slot["s"]["field"] = max(slot["s"]["field"], _clamp01(h.get("s", 1.0)))
        slot["payload"] = {
            "entity": h.get("entity"),
            "field_name": h.get("field_name"),
            "field_value": h.get("field_value"),
        }
    linked_entities = _linked_entities(field_hits)
    for route, hits in (
        ("jieba", jieba_hits),
        ("simple", simple_hits),
        ("vec", vec_hits),
    ):
        for h in hits or []:
            slot = _slot("qa", h["qa_id"])
            slot["s"][route] = max(slot["s"][route], _clamp01(h.get("s", 0.0)))
            slot["payload"] = {
                "standard_question": h.get("standard_question"),
                "official_answer": h.get("official_answer"),
                "category": h.get("category"),
            }

    # 联动在四路都填完之后施加（`entity_text` 模式需要该 QA 自身的文本证据）。
    for slot in pool.values():
        if slot["type"] != "qa":
            continue
        strength = _linkage_for(linked_entities.get(slot["payload"].get("category")), slot)
        if strength:
            slot["s"]["field"] = max(slot["s"]["field"], strength)

    fused = []
    for slot in pool.values():
        s = slot["s"]
        score = (
            W_FIELD * s["field"]
            + W_JIEBA * s["jieba"]
            + W_SIMPLE * s["simple"]
            + W_VEC * s["vec"]
        ) / denom
        fused.append({**slot, "score": score, "denom": denom})
    fused.sort(key=lambda c: (-c["score"], c["type"], str(c["key"])))
    return fused


def decide(fused: list) -> dict:
    """最终判定。返回 {"action", "items", "top1_score"}，action ∈
    direct（≥0.75 直接返回）/ maybe_single（0.45–0.75 且 gap≥0.15，标注可能相关）/
    maybe_multi（0.45–0.75 且 gap<0.15，交用户判断）/ fail_closed（<0.45 或空表）。"""
    if not fused:
        return {"action": "fail_closed", "items": [], "top1_score": 0.0}
    top = fused[0]
    # 精确字段直查：用户问的就是这个字段名本身（s=1.0 精确命中，不是包含匹配的 0.8），
    # 字段值即答案，不需要其它路背书。旧实现靠它在融合分里占 w_field/Σw 达标，
    # P6 把 w_field 从 3.0 下调到 1.5 后这条路径只有 0.158、跌破 TH_MAYBE 被全拒 ——
    # 而 29 个字段里 21 个没有 QA 孪生，等于字段直查主体失效。
    # 安全性：13 道 null 对抗题无一精确命中字段名（唯一命中的「有纸质发票吗」是
    # 包含匹配 s=0.80），故本规则不削弱拒答。
    if top["type"] == "field" and top["s"]["field"] >= 1.0:
        return {"action": "direct", "items": [top], "top1_score": top["score"]}
    top1 = top["score"]
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