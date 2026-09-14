"""评测集加载器：校验 schema，不合法即 ValueError（行号定位）。"""

import json
from dataclasses import dataclass, field


@dataclass
class EvalItem:
    question: str
    expected_qa_id: str | None
    tags: list = field(default_factory=list)


def load_eval_items(path) -> list:
    items = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"第 {lineno} 行 JSON 非法：{e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"第 {lineno} 行必须是 object")
            q = obj.get("question")
            if not isinstance(q, str) or not q.strip():
                raise ValueError(f"第 {lineno} 行 question 缺失/非空字符串")
            exp = obj.get("expected_qa_id")
            if exp is not None and (not isinstance(exp, str) or not exp.strip()):
                raise ValueError(f"第 {lineno} 行 expected_qa_id 须为 string 或 null")
            tags = obj.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
                raise ValueError(f"第 {lineno} 行 tags 须为 string 数组")
            items.append(EvalItem(question=q.strip(), expected_qa_id=exp, tags=tags))
    return items
