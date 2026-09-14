"""评测集 schema + 加载器（PRD §3.3：冻结后不得为提分而改）。

行格式（JSONL，每行一个）：
  {"question": str, "expected_qa_id": str | null, "tags": [str]}

- expected_qa_id 为 null 表示该题应 Fail-Closed（runner 只记 returned_any，
  不计入命中率；判定逻辑是 D6 的事）。
- tags 可选，缺省 []。
"""