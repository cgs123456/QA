"""JSON → QA 对 + 字段（PRD §3.3 映射表）。

- dict 输入：顶层 key → entity（保留字 `qa_pairs` 除外，其值为 QA 列表）；
  二级 key → field_name，叶子标量（str/int/float/bool）→ field_value；
  更深的嵌套/非标量叶子跳过并计数。
- list 输入：每项为 QA（含 standard_question|question + official_answer|answer）
  或字段（含 entity + field_name + field_value）。
- QA 别名键透传：question→standard_question、answer→official_answer；
  category / usage_status / followup_logic / id 保留交 validator 校验。
"""

_QA_Q_KEYS = ("standard_question", "question")
_QA_A_KEYS = ("official_answer", "answer")
_FIELD_KEYS = ("entity", "field_name", "field_value")
_QA_PASSTHROUGH = ("category", "usage_status", "followup_logic", "id")


def _norm_qa(raw: dict) -> dict:
    out: dict = {}
    for key in _QA_Q_KEYS:
        if raw.get(key) is not None:
            out["standard_question"] = raw[key]
            break
    for key in _QA_A_KEYS:
        if raw.get(key) is not None:
            out["official_answer"] = raw[key]
            break
    for key in _QA_PASSTHROUGH:
        if raw.get(key) is not None:
            out[key] = raw[key]
    return out


def _looks_like_qa(raw: dict) -> bool:
    return any(k in raw for k in _QA_Q_KEYS) and any(k in raw for k in _QA_A_KEYS)


def _looks_like_field(raw: dict) -> bool:
    return all(k in raw for k in _FIELD_KEYS)


def parse_json(data, filename: str | None = None) -> dict:
    qa_items: list = []
    field_items: list = []
    skipped = 0

    if isinstance(data, dict):
        for top_key, top_val in data.items():
            if top_key == "qa_pairs":
                if isinstance(top_val, list):
                    for item in top_val:
                        if isinstance(item, dict) and _looks_like_qa(item):
                            qa_items.append(_norm_qa(item))
                        else:
                            skipped += 1
                else:
                    skipped += 1
                continue
            entity = str(top_key)
            if isinstance(top_val, dict):
                for sub_key, leaf in top_val.items():
                    if isinstance(leaf, str) or isinstance(leaf, (int, float)):
                        field_items.append(
                            {
                                "entity": entity,
                                "field_name": str(sub_key),
                                "field_value": str(leaf),
                            }
                        )
                    else:
                        skipped += 1
            else:
                skipped += 1
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                skipped += 1
                continue
            if _looks_like_qa(item):
                qa_items.append(_norm_qa(item))
            elif _looks_like_field(item):
                field_items.append(
                    {
                        "entity": item["entity"],
                        "field_name": item["field_name"],
                        "field_value": item["field_value"],
                        **(
                            {"aliases": item["aliases"]}
                            if item.get("aliases") is not None
                            else {}
                        ),
                    }
                )
            else:
                skipped += 1
    else:
        raise ValueError("JSON 顶层必须是 object 或 array")

    return {
        "qa_items": qa_items,
        "field_items": field_items,
        "skipped": skipped,
    }
