"""字段直查（第 1 级，确定性证据，s=1.0 二值）。

归一化后与 field_aliases.alias 或 fields.field_name 精确相等即命中：
- 先按原文精确查，再按归一化（NFKC/去空白/小写）查一轮，两路去重。
- 同库同名可属不同 entity，命中全部返回（上限 limit）。
"""

import unicodedata


def normalize_query(query: str) -> str:
    q = unicodedata.normalize("NFKC", query or "")
    return "".join(q.split()).lower().strip()


def field_lookup(conn, store_id: str, query: str, limit: int = 5) -> list:
    raw = (query or "").strip()
    if not raw:
        return []
    tried: set = set()
    hits: dict = {}
    for text in (raw, normalize_query(raw)):
        if not text or text in tried:
            continue
        tried.add(text)
        rows = conn.execute(
            "SELECT f.id, f.entity, f.field_name, f.field_value FROM fields f"
            " WHERE f.store_id=? AND (f.field_name=? OR EXISTS (SELECT 1"
            " FROM field_aliases a WHERE a.field_id=f.id AND a.alias=?))"
            " LIMIT ?",
            (store_id, text, text, limit),
        ).fetchall()
        for fid, entity, name, value in rows:
            if fid not in hits:
                hits[fid] = {
                    "field_id": fid,
                    "entity": entity,
                    "field_name": name,
                    "field_value": value,
                    "source": "field",
                    "s": 1.0,
                }
    return list(hits.values())[:limit]
