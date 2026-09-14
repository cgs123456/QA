"""Markdown → QA 对 + 字段（PRD §3.3 映射表）。

约定（文档即契约）：
- `# H1` → entity；无 H1 时取文件名 stem，无文件名取 "default"。
- `## H2` 以 `?`/`？` 结尾 → QA 对（question=H2 原文，answer=其下正文）。
- 其他 `## H2` → 字段（field_name=H2，field_value=其下首段）。
- 正文中的 `Q:/问：` 行与后续 `A:/答：` 行 → QA 对（可出现在任何 H2 下，
  附着到当前 entity；所在 H2 若同时有非问答正文，仍按字段规则提取）。
- H2 之前的正文（引言）忽略。
"""

import re
from pathlib import Path

_H1 = re.compile(r"^#\s+(.*\S)\s*$")
_H2 = re.compile(r"^##\s+(.*\S)\s*$")
_Q = re.compile(r"^(Q|问)\s*[:：]\s*(.*\S)\s*$")
_A = re.compile(r"^(A|答)\s*[:：]\s*(.*)$")
_QUESTION_END = ("?", "？")


def _flush_qa_block(q_lines, a_lines, entity, qa_items):
    q = " ".join(t for t in q_lines if t).strip()
    a = " ".join(t for t in a_lines if t).strip()
    if q and a:
        qa_items.append(
            {"standard_question": q, "official_answer": a, "category": entity}
        )


def parse_markdown(text: str, filename: str | None = None) -> dict:
    entity = Path(filename).stem if filename else "default"
    qa_items: list = []
    field_items: list = []
    h2: str | None = None
    body: list = []

    def flush():
        if h2 is None:
            body.clear()
            return
        # 正文内 Q/A 块优先提成 QA 对。
        q_lines: list = []
        a_lines: list = []
        in_answer = False
        plain: list = []
        for line in body:
            m_q = _Q.match(line)
            m_a = _A.match(line)
            if m_q:
                if q_lines or a_lines:
                    _flush_qa_block(q_lines, a_lines, entity, qa_items)
                q_lines, a_lines, in_answer = [m_q.group(2)], [], False
            elif m_a and (q_lines or a_lines):
                a_lines.append(m_a.group(2))
                in_answer = True
            elif in_answer and line:
                a_lines.append(line)
            elif q_lines and not in_answer and line:
                q_lines.append(line)
            else:
                # 含空行：plain 用空串作分段标记。
                plain.append(line)
        if q_lines or a_lines:
            _flush_qa_block(q_lines, a_lines, entity, qa_items)
        if h2.endswith(_QUESTION_END):
            answer = "\n".join(t for t in plain if t).strip()
            if answer:
                qa_items.append(
                    {
                        "standard_question": h2,
                        "official_answer": answer,
                        "category": entity,
                    }
                )
        else:
            # plain 保留空行作分段标记，首段为 field_value。
            paras, current = [], []
            for t in plain:
                if t:
                    current.append(t)
                elif current:
                    paras.append(" ".join(current))
                    current = []
            if current:
                paras.append(" ".join(current))
            value = paras[0].strip() if paras else ""
            # Q/A 行已在上一步消费，不再进入字段。
            if value:
                field_items.append(
                    {"entity": entity, "field_name": h2, "field_value": value}
                )
        body.clear()

    for raw in (text or "").splitlines():
        line = raw.strip()
        m1 = _H1.match(line)
        if m1:
            flush()
            h2 = None
            entity = m1.group(1)
            continue
        m2 = _H2.match(line)
        if m2:
            flush()
            h2 = m2.group(1)
            continue
        body.append(line)
    flush()
    return {"qa_items": qa_items, "field_items": field_items}
