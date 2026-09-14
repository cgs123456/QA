"""导入校验去重（content-free：统计只记行数，不记内容）。

- QA 必填：standard_question / official_answer（去首尾空白后非空）；
  usage_status 缺省 'fixed'，非法取值记 invalid；
  显式 id 若提供必须非空字符串。
- 字段必填：entity / field_name / field_value（同上）。
- 去重（批内，后出现覆盖先出现）：QA 按 standard_question，字段按
  (entity, field_name)。
"""

ALLOWED_USAGE = ("fixed", "refresh", "conditional", "rejected")


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def validate_qa_items(raw_items: list) -> tuple:
    """返回 (valid, stats)。valid 项含 standard_question/official_answer/
    category/usage_status/followup_logic/id（id 可能缺失，由 compiler 生成）。"""
    valid_by_q: dict = {}
    invalid = 0
    dup = 0
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        q = _clean(raw.get("standard_question"))
        a = _clean(raw.get("official_answer"))
        usage = _clean(raw.get("usage_status")) or "fixed"
        qid = raw.get("id")
        if not q or not a or usage not in ALLOWED_USAGE:
            invalid += 1
            continue
        if qid is not None and not _clean(qid):
            invalid += 1
            continue
        item = {
            "standard_question": q,
            "official_answer": a,
            "category": _clean(raw.get("category")) or None,
            "usage_status": usage,
            "followup_logic": _clean(raw.get("followup_logic")) or None,
        }
        if qid is not None:
            item["id"] = _clean(qid)
        if q in valid_by_q:
            dup += 1
        valid_by_q[q] = item
    valid = list(valid_by_q.values())
    stats = {
        "total": len(raw_items or []),
        "valid": len(valid),
        "invalid": invalid,
        "duplicate_dropped": dup,
    }
    return valid, stats


def validate_field_items(raw_items: list) -> tuple:
    """返回 (valid, stats)。valid 项含 entity/field_name/field_value/aliases。"""
    valid_by_key: dict = {}
    invalid = 0
    dup = 0
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        entity = _clean(raw.get("entity"))
        name = _clean(raw.get("field_name"))
        value = _clean(raw.get("field_value"))
        if not entity or not name or not value:
            invalid += 1
            continue
        aliases = raw.get("aliases") or []
        aliases = [_clean(a) for a in aliases if _clean(a)] if isinstance(
            aliases, list
        ) else []
        key = (entity, name)
        if key in valid_by_key:
            dup += 1
        valid_by_key[key] = {
            "entity": entity,
            "field_name": name,
            "field_value": value,
            "aliases": aliases,
        }
    valid = list(valid_by_key.values())
    stats = {
        "total": len(raw_items or []),
        "valid": len(valid),
        "invalid": invalid,
        "duplicate_dropped": dup,
    }
    return valid, stats