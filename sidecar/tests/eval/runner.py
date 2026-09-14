"""评测 runner 骨架：字段直查 + FTS 双路 Top-3/Top-5 命中率 + 各路延迟。

- scored 题（expected 非 null）：top3/top5 命中按 FTS 双路合并排名；
  field_hit 记录字段直查是否命中（确定性证据覆盖度）。
- null 题（应 Fail-Closed）：不计命中率，只记 returned_any 供未来判定评估。
- 融合与 Fail-Closed 判定是 D6 的事，本 runner 只量两级检索。
"""

import time

from retrieval.field_lookup import field_lookup
from retrieval.fts5_search import fts5_search


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
