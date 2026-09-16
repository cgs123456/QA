"""评测 runner：两级检索 evaluate() + 四路融合基线 evaluate_full()。

- scored 题（expected 非 null）：top3/top5 命中；field_hit 记录字段覆盖度。
- null 题（应 Fail-Closed）：不计命中率；evaluate_full 中按判定 action
  统计 fail_closed（含 null 题的 null_fail_closed_rate）。
- evaluate_full 用当前默认阈值（TH_DIRECT=0.75/TH_MAYBE=0.45/GAP=0.15），
  阈值调整按 PRD 标定顺序（s_i→w_i→阈值）并记录，本函数不改阈值。
- `qa_docs` 随摘要一起返回：Top-3 这类召回指标在小语料上与语料规模强相关
  （FTS5 idf = ln((N-n+0.5)/(n+0.5))，N 小时塌缩），报告必须带上 N 才能解读。
"""

import time

from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search
from retrieval.hybrid_rank import decide, fuse
from retrieval.query_prep import prepare
from retrieval.vector_search import vector_search


def evaluate(conn, store_id: str, items: list) -> dict:
    details = []
    top3_hits = top5_hits = field_hits = scored = 0
    null_items = 0
    field_items = field_found = 0
    ms_field = ms_fts = ms_prep = 0.0

    for it in items:
        t0 = time.perf_counter()
        prep = prepare(conn, it.question)
        ms_prep += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        field = field_lookup(conn, store_id, it.question, prepared=prep)
        t_field = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        fts = fts5_search(conn, store_id, it.question, prepared=prep)
        t_fts = (time.perf_counter() - t0) * 1000
        ms_field += t_field
        ms_fts += t_fts
        ranked = [h["qa_id"] for h in fts]

        if it.expected_field is not None:
            # P6.1 v2 纯字段探针：不进 Top-3 分母（ranked_qa 无 field 行），
            # 单列“字段名命中”率。action 判定在此两级 runner 无 decide，不统计。
            field_items += 1
            found = any(h.get("field_name") == it.expected_field for h in field)
            field_found += found
            details.append(
                {
                    "question": it.question,
                    "scored": False,
                    "field_probe": True,
                    "expected_field": it.expected_field,
                    "field_found": found,
                }
            )
            continue

        if it.expected_qa_id is None:
            null_items += 1
            details.append(
                {
                    "question": it.question,
                    "scored": False,
                    "returned_any": bool(ranked or field),
                }
            )
            continue

        scored += 1
        hit3 = it.expected_qa_id in ranked[:3]
        hit5 = it.expected_qa_id in ranked[:5]
        top3_hits += hit3
        top5_hits += hit5
        has_field = bool(field)
        field_hits += has_field
        details.append(
            {
                "question": it.question,
                "expected_qa_id": it.expected_qa_id,
                "scored": True,
                "top3_hit": hit3,
                "top5_hit": hit5,
                "field_hit": has_field,
                "ranked_top3": ranked[:3],
                "ms": {"field": t_field, "fts": t_fts},
            }
        )

    n = len(items)
    return {
        "n": n,
        "scored": scored,
        "null_items": null_items,
        "field_items": field_items,
        "field_found_rate": (field_found / field_items) if field_items else 0.0,
        "top3_rate": (top3_hits / scored) if scored else 0.0,
        "top5_rate": (top5_hits / scored) if scored else 0.0,
        "field_hit_rate": (field_hits / scored) if scored else 0.0,
        "avg_ms": {
            "prep": ms_prep / n if n else 0.0,
            "field": ms_field / n if n else 0.0,
            "fts": ms_fts / n if n else 0.0,
        },
        "details": details,
    }


def evaluate_full(conn, store_id: str, items: list, embed_fn) -> dict:
    """四路 + 融合 + 判定基线。embed_fn(texts) -> 向量序列（供 vector 路）。

    Top-3/Top-5 按融合后 qa 候选排名；Fail-Closed 率按 decide() 统计。
    field_hit_rate：scored 题中字段路有返回的比例（与 evaluate() 同定义）。
    wrong_answer_rate：**给出了答案但答案是错的**（action != fail_closed 且
    top1 不是期望 QA）——PRD 红线的直接度量。拒答不计入分母（宁可错杀）。
    """
    details = []
    top3 = top5 = scored = field_hits = 0
    answered = wrong = 0
    null_items = null_fc = 0
    field_items = field_direct_ok = 0
    actions: dict = {}
    ms = {"prep": 0.0, "field": 0.0, "fts": 0.0, "vec": 0.0, "embed": 0.0, "fuse": 0.0}
    qa_docs = conn.execute(
        "SELECT COUNT(*) FROM qa_pairs WHERE store_id=?", (store_id,)
    ).fetchone()[0]

    for it in items:
        # 查询预处理只做一次，字段路与 FTS 路共用（两条路的关键词必须同源）。
        t0 = time.perf_counter()
        prep = prepare(conn, it.question)
        ms["prep"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        field = field_lookup(conn, store_id, it.question, prepared=prep)
        ms["field"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        fts = fts5_search(conn, store_id, it.question, prepared=prep)
        ms["fts"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        vecs = embed_fn([it.question])
        ms["embed"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        vec = vector_search(conn, store_id, vecs[0])
        ms["vec"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        fused = fuse(
            field,
            [h for h in fts if h["route"] == "jieba"],
            [h for h in fts if h["route"] == "simple"],
            vec,
        )
        decision = decide(fused)
        ms["fuse"] += (time.perf_counter() - t0) * 1000

        action = decision["action"]
        actions[action] = actions.get(action, 0) + 1
        ranked_qa = [c["key"] for c in fused if c["type"] == "qa"]
        top1 = fused[0] if fused else None
        top1_name = (
            (top1["payload"] or {}).get("field_name")
            if top1 is not None and top1["type"] == "field"
            else (top1["payload"] or {}).get("standard_question")
            if top1 is not None
            else None
        )
        if it.expected_field is not None:
            # P6.1 v2 纯字段探针：精确字段硬规则 direct 即对（top1 须为该字段本身）；
            # 被同 entity QA 劫持答出（action!=FC 但 top1 非该字段）计答错；
            # FC 为错杀（不计错，但拉低 field_direct_rate）。
            # top1 取 decision 落子的 items[0]（P6.1 救援改写落子后，fused[0]
            # 仍是排序首位——判卷必须看落子，不能看排序）。
            field_items += 1
            landed = decision["items"][0] if decision["items"] else top1
            ok = (action == "direct" and landed is not None
                  and landed["type"] == "field"
                  and (landed["payload"] or {}).get("field_name") == it.expected_field)
            field_direct_ok += ok
            answered_here = action != "fail_closed" and bool(decision["items"])
            is_wrong = answered_here and not ok
            if answered_here:
                answered += 1
                wrong += is_wrong
            details.append(
                {
                    "question": it.question,
                    "scored": False,
                    "field_probe": True,
                    "expected_field": it.expected_field,
                    "action": action,
                    "top1_score": decision["top1_score"],
                    "top1": (
                        {"type": landed["type"], "key": landed["key"],
                         "name": (landed["payload"] or {}).get("field_name"),
                         "score": landed["score"]}
                        if landed
                        else None
                    ),
                    "field_ok": ok,
                    "answered": answered_here,
                    "wrong": is_wrong,
                    "n_vec": len(vec),
                }
            )
            continue

        if it.expected_qa_id is None:
            null_items += 1
            null_fc += action == "fail_closed"
            details.append(
                {
                    "question": it.question,
                    "scored": False,
                    "action": action,
                    "top1_score": decision["top1_score"],
                    "n_vec": len(vec),
                }
            )
            continue

        scored += 1
        hit3 = it.expected_qa_id in ranked_qa[:3]
        hit5 = it.expected_qa_id in ranked_qa[:5]
        top3 += hit3
        top5 += hit5
        field_hits += bool(field)
        # 红线：只有"真的答了"才可能答错；拒答是安全侧，不计入分母。
        answered_here = action != "fail_closed" and bool(decision["items"])
        top1_key = top1["key"] if top1 is not None and top1["type"] == "qa" else None
        is_wrong = answered_here and top1_key != it.expected_qa_id
        if answered_here:
            answered += 1
            wrong += is_wrong
        details.append(
            {
                "question": it.question,
                "expected_qa_id": it.expected_qa_id,
                "scored": True,
                "top3_hit": hit3,
                "top5_hit": hit5,
                "action": action,
                "top1_score": decision["top1_score"],
                "top1": (
                    {"type": top1["type"], "key": top1["key"],
                     "score": top1["score"]}
                    if top1
                    else None
                ),
                "answered": answered_here,
                "wrong": is_wrong,
                "n_vec": len(vec),
            }
        )

    n = len(items)
    return {
        "n": n,
        "qa_docs": qa_docs,
        "scored": scored,
        "null_items": null_items,
        "field_items": field_items,
        "field_direct_ok": field_direct_ok,
        "field_direct_rate": (field_direct_ok / field_items) if field_items else 0.0,
        "top3_rate": (top3 / scored) if scored else 0.0,
        "top5_rate": (top5 / scored) if scored else 0.0,
        "field_hit_rate": (field_hits / scored) if scored else 0.0,
        "answered": answered,
        "wrong_answers": wrong,
        "wrong_answer_rate": (wrong / answered) if answered else 0.0,
        "direct_rate": actions.get("direct", 0) / n if n else 0.0,
        "fail_closed_rate": actions.get("fail_closed", 0) / n if n else 0.0,
        "null_fail_closed_rate": (null_fc / null_items) if null_items else 0.0,
        "actions": actions,
        "avg_ms": {k: (v / n if n else 0.0) for k, v in ms.items()},
        "details": details,
    }
