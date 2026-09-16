"""评测集加载器：校验 schema，不合法即 ValueError（行号定位）。"""

import json
from dataclasses import dataclass, field


@dataclass
class EvalItem:
    question: str
    expected_qa_id: str | None
    tags: list = field(default_factory=list)


def load_eval_items(path) -> list:
    """加载并校验；任一问题即 ValueError（行号定位）。

    三条硬校验（P6 评测集冻结）：
      1. schema —— question 非空字符串 / expected_qa_id 为 string 或 null / tags 为 string 数组；
      2. 去重   —— question（strip 后）不得重复。重复题会把同一道题在指标里
                   计权两次，使 Top-3 与拒答率随"某题抄了几遍"漂移，破坏冻结语义；
      3. 引用   —— expected_qa_id 必须能在语料里解析（在 test_eval.py 里做，
                   因为需要建库，loader 不持有语料）。
    """
    items = []
    seen: dict[str, int] = {}
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
            key = q.strip()
            if key in seen:
                raise ValueError(
                    f"第 {lineno} 行 question 重复（首次出现在第 {seen[key]} 行）：{key!r}"
                )
            seen[key] = lineno
            items.append(EvalItem(question=key, expected_qa_id=exp, tags=tags))
    return items
