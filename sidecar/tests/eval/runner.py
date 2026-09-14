"""评测 runner：两级检索 evaluate() + 四路融合基线 evaluate_full()。

- scored 题（expected 非 null）：top3/top5 命中；field_hit 记录字段覆盖度。
- null 题（应 Fail-Closed）：不计命中率；evaluate_full 中按判定 action
  统计 fail_closed（含 null 题的 null_fail_closed_rate）。
- evaluate_full 用当前默认阈值（TH_DIRECT=0.75/TH_MAYBE=0.45/GAP=0.15），
  阈值调整按 PRD 标定顺序（s_i→w_i→阈值）并记录，本函数不改阈值。
"""

import time

from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search
from retrieval.hybrid_rank import decide, fuse
from retrieval.vector_search import vector_search


def evaluate(conn, store_id: str, items: list) -> dict:
    details = []
    top3_hits = top5_hits = field_hits = scored = 0
    null_items = 0
    ms_field = ms_fts = 0.0

    for it in items:
        t0 = time.perf_counter()
        field = field_lookup(conn, store_id, it.question)
        t_field = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        fts = fts5_search(conn, store_id, it.question)
        t_fts = (time.perf_counter() - t0) * 1000
        ms_field += t_field
        ms_fts += t_fts
        ranked = [h["qa_id"] for h in fts]

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
        "top3_rate": (top3_hits / scored) if scored else 0.0,
        "top5_rate": (top5_hits / scored) if scored else 0.0,
        "field_hit_rate": (field_hits / scored) if scored else 0.0,
        "avg_ms": {
            "field": ms_field / n if n else 0.0,
            "fts": ms_fts / n if n else 0.0,
        },
        "details": details,
    }


def evaluate_full(conn, store_id: str, items: list, embed_fn) -> dict:
    """四路 + 融合 + 判定基线。embed_fn(texts) -> 向量序列（供 vector 路）。

    Top-3/Top-5 按融合后 qa 候选排名；Fail-Closed 率按 decide() 统计。
    """
    details = []
    top3 = top5 = scored = 0
    null_items = null_fc = 0
    actions: dict = {}
    ms = {"field": 0.0, "fts": 0.0, "vec": 0.0, "embed": 0.0, "fuse": 0.0}

    for it in items:
        t0 = time.perf_counter()
        field = field_lookup(conn, store_id, it.question)
        ms["field"] += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        fts = fts5_search(conn, store_id, it.question)
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
                "n_vec": len(vec),
            }
        )

    n = len(items)
    return {
        "n": n,
        "scored": scored,
        "null_items": null_items,
        "top3_rate": (top3 / scored) if scored else 0.0,
        "top5_rate": (top5 / scored) if scored else 0.0,
        "direct_rate": actions.get("direct", 0) / n if n else 0.0,
        "fail_closed_rate": actions.get("fail_closed", 0) / n if n else 0.0,
        "null_fail_closed_rate": (null_fc / null_items) if null_items else 0.0,
        "actions": actions,
        "avg_ms": {k: (v / n if n else 0.0) for k, v in ms.items()},
        "details": details,
    }
