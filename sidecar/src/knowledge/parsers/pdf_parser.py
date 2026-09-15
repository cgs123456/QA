"""PDF → QA 对 + 字段（P1 导入 dry-run 一环）。

选型：pypdf（而非 pdfplumber）。理由：
- pypdf 纯 Python、无 Pillow/pdfminer 等二进制依赖，sidecar 走 PyInstaller
  onedir 打包，每加一个原生依赖都是体积与签名风险；本任务只需文本提取，
  不需要 pdfplumber 的版式/表格还原能力。
- 代价：无版式分析，故第一版**仅支持单栏顺序文本**；双栏/跨页表格的
  文本顺序错乱是已知坑，不做启发式猜测，如实标 `layout_support`，
  需要人工核对（见 preview 返回）。

文本型判定：逐页 `extract_text()`，去空白后字符数 ≥ `MIN_CHARS_PER_PAGE`
即文本页，否则记图片/扫描页。全为扫描页 → `UnsupportedPDFError`
（P1 语义：明确拒绝，不静默跳过）；部分扫描 → preview/commit 照常返回，
但 `unsupported_pages` 显式列出，commit 统计 `pdf_unsupported_pages`。

提取规则（单栏顺序文本）：
- 章节标题行（短行、无句末标点、带编号或 ≤10 字）→ 当前 entity。
- `^(.{1,20})[:：]\\s*(.+)$` → 字段（entity=当前标题域）。
- `Q/问：` 行 + 后续 `A/答：` 行 → QA 对（复用 markdown 问答语义）。
- 控制字符（`\\x00` 等 pypdf 偶发杂质）一律剥除，只留 `\\n\\t`。
"""

import io
import re

from pypdf import PdfReader

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 200
MIN_CHARS_PER_PAGE = 50
#: 冒号字段名限长（与模块文档 `^(.{1,20})` 一致，超长冒号行按正文跳过）。
MAX_FIELD_NAME_CHARS = 20

_COLON_RE = re.compile(r"^(.{1,20})[:：]\s*(.+)$")
_Q_RE = re.compile(r"^(Q|问)\s*[:：]\s*(.*\S)\s*$")
_A_RE = re.compile(r"^(A|答)\s*[:：]\s*(.*)$")
_END_PUNCT = ("。", "；", "！", "？", ".", "!", "?", ";", ":", "：", ",", "，")
_NUM_PREFIX = re.compile(r"^(第.+[章节条部分]|（?[一二三四五六七八九十\d]+[、.．]）?|\d+(\.\d+)*\.?)\s*\S")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class UnsupportedPDFError(ValueError):
    """扫描件/图片型 PDF（无可提取文本），P1 明确拒绝。"""

    def __init__(self, message: str, pages: list):
        super().__init__(message)
        self.pages = pages


def _clean_text(text: str) -> str:
    return _CTRL_RE.sub("", text or "")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 24:
        return False
    if ":" in s or "：" in s:
        return False
    if s[-1] in _END_PUNCT:
        return False
    return bool(_NUM_PREFIX.match(s)) or len(s) <= 10


def _read_pages(content: bytes):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(
            f"PDF 文件过大（{len(content)} 字节，上限 {MAX_FILE_BYTES}）")
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception:
        raise ValueError("PDF 解析失败：不是有效的 PDF 文件")
    if reader.is_encrypted:
        raise ValueError("PDF 已加密，本版不支持（请提供未加密文本型 PDF）")
    if len(reader.pages) == 0:
        raise ValueError("PDF 无页面")
    if len(reader.pages) > MAX_PAGES:
        raise ValueError(f"PDF 页数超限（{len(reader.pages)}，上限 {MAX_PAGES}）")
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
        text = _clean_text(raw)
        chars = len(re.sub(r"\s", "", text))
        pages.append({"index": i, "chars": chars,
                      "supported": chars >= MIN_CHARS_PER_PAGE, "text": text})
    return pages


def _extract_items(pages: list, entity_default: str) -> dict:
    qa_items: list = []
    field_items: list = []
    entity = entity_default
    pending_q: str | None = None
    pending_a: list = []
    skipped = 0

    def flush_qa() -> int:
        nonlocal pending_q, pending_a
        q = (pending_q or "").strip()
        a = " ".join(t for t in pending_a if t).strip()
        pending_q, pending_a = None, []
        if q and a:
            qa_items.append({"standard_question": q, "official_answer": a,
                             "category": entity})
            return 0
        # 半截问答块：不成条目，计数跳过（不明猜）。
        return 1 if (q or a) else 0

    def emit_field(line: str) -> None:
        nonlocal skipped
        m_c = _COLON_RE.match(line)
        if m_c:
            name, value = m_c.group(1).strip(), m_c.group(2).strip()
            if name and value:
                field_items.append({"entity": entity, "field_name": name,
                                    "field_value": value})
                return
        skipped += 1

    for pg in pages:
        if not pg["supported"]:
            continue
        for raw_line in pg["text"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m_q = _Q_RE.match(line)
            m_a = _A_RE.match(line)
            if m_q:
                skipped += flush_qa()
                pending_q, pending_a = m_q.group(2), []
                continue
            if m_a and pending_q is not None:
                pending_a.append(m_a.group(2))
                continue
            if m_a:
                # 无 Q 的孤 A 行：不成条目也不成字段（防 “A” 字段垃圾）。
                skipped += 1
                continue
            if _is_heading(line):
                # 标题切换域：先结算悬空问答块（属旧域，不跨域）。
                skipped += flush_qa()
                entity = line.strip()[:64]
                continue
            if pending_a:
                # 答案续行（含多段，空行已在上游丢弃，段落以空格连接）。
                pending_a.append(line)
                continue
            if pending_q is not None:
                # Q 后首个非 A 行：标题/冒号行按普通行处理（孤 Q 作废），
                # 否则视为多行问题的续行。
                if _is_heading(line):
                    skipped += flush_qa()
                    entity = line.strip()[:64]
                elif _COLON_RE.match(line):
                    skipped += flush_qa()
                    emit_field(line)
                else:
                    pending_q += line
                continue
            emit_field(line)
    skipped += flush_qa()
    unsupported = [p["index"] for p in pages if not p["supported"]]
    return {"qa_items": qa_items, "field_items": field_items,
            "skipped": skipped, "unsupported_pages": unsupported,
            "entity_default": entity_default}


def parse_pdf_items(content: bytes, filename: str | None = None,
                    entity_override: str | None = None) -> dict:
    """产出原始条目（preview/commit 共用；全扫描页即抛 UnsupportedPDFError）。"""
    from pathlib import Path

    pages = _read_pages(content)
    if all(not p["supported"] for p in pages):
        raise UnsupportedPDFError(
            "PDF 为扫描件/图片型（逐页可提取字符均 "
            f"< {MIN_CHARS_PER_PAGE}），本版不支持 OCR："
            "请提供文本型 PDF（单栏顺序文本）后重试",
            pages=[p["index"] for p in pages])
    if entity_override is not None:
        if (not isinstance(entity_override, str) or not entity_override.strip()
                or len(entity_override.strip()) > 64):
            raise ValueError("PDF 映射非法：entity 须为 1–64 字符字符串")
        default_entity = entity_override.strip()
    else:
        stem = Path(filename).stem if filename else ""
        default_entity = stem.strip() or "default"
    out = _extract_items(pages, default_entity)
    out.update({"pages": [{"index": p["index"], "chars": p["chars"],
                           "supported": p["supported"]} for p in pages],
                "n_pages": len(pages),
                "layout_support": "single-column-only",
                "layout_note": "仅支持单栏顺序文本；双栏/跨页表格顺序可能错乱，需人工核对"})
    return out


def parse_pdf_preview(content: bytes, filename: str | None = None) -> dict:
    """dry-run：条目 + 每类样例 3 行，不写库。"""
    out = parse_pdf_items(content, filename)
    return {"format": "pdf", "filename": filename or "upload.pdf",
            "supported": True, "n_pages": out["n_pages"], "pages": out["pages"],
            "unsupported_pages": out["unsupported_pages"],
            "layout_support": out["layout_support"],
            "layout_note": out["layout_note"],
            "qa_samples": out["qa_items"][:3],
            "field_samples": out["field_items"][:3],
            "qa_count": len(out["qa_items"]),
            "field_count": len(out["field_items"]),
            "warnings": (["部分页面为图片/扫描页，已跳过并列出（不静默）："
                          + ",".join(map(str, out["unsupported_pages"]))]
                         if out["unsupported_pages"] else [])}
