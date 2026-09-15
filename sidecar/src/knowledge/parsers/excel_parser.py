"""Excel（.xlsx）→ QA 对 + 字段（PRD §3.3 映射表，P1 导入 dry-run 一环）。

流程：`parse_excel_preview`（读所有 sheet → 表头探测 → 列映射提案，
不写库）→ 用户确认映射 → `apply_excel_mapping`（服务端重载重校验后
产出条目，交既有 compiler 短事务）。

openpyxl 说明（实现注释，坑位）：
- 以 `data_only=True` 打开：openpyxl **不计算公式**，此时公式单元格读到的是
  上次用 Excel 保存的**缓存结果值**；从未用 Excel 保存过的文件（如纯程序
  生成的含公式工作簿）读到 `None`，按空处理（不清零、不猜值）。
- 因此必须同时用 `data_only=True`（防公式串入库）+ 公式注入清洗
  （防 `=HYPERLINK` 类手工 XML 注入的原始字符串），见 `_sanitize_cell`。

安全防火墙（服务端 preview/commit 同走）：
- 文件 ≤ `MAX_FILE_BYTES`；sheet 数/行数/列数上限；超限即 `ValueError`。
- 单格字符数 ≤ `MAX_CELL_CHARS`，超限整文件拒绝（不明截断，不静默丢字）。
- 前导 `= + - @`（含全角 `＝＋－＠`）及制表/回车开头的字符串一律加 `'` 前缀
  中和（CSV/Excel 公式注入经典向量），并计数进 warnings。

表头探测：在前 `HEADER_SCAN_ROWS` 行内找首个“≥2 个非空格”行；找不到则
`header_row=None`，列名记 `COL-<A..>`，提案仅靠内容嗅探（低置信），
由用户在 commit 映射中手工指定。
"""

import io
import re
import zipfile
from datetime import date, datetime

import openpyxl
from openpyxl.utils import get_column_letter

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SHEETS = 32
MAX_ROWS_PER_SHEET = 20000
MAX_COLS_PER_SHEET = 64
MAX_CELL_CHARS = 8000
HEADER_SCAN_ROWS = 5
PREVIEW_SAMPLES = 3
SUGGEST_THRESHOLD = 0.6

ROLES = (
    "entity",
    "field_name",
    "field_value",
    "standard_question",
    "official_answer",
    "category",
    "usage_status",
    "followup_logic",
    "id",
    "ignore",
)
_MUTABLE_DEFAULT_ROLES = ("entity", "category")
SPECIAL_DEFAULTS_KEY = "__defaults__"

# 表头同义词（归一化后比对）：小写 + 去空白/`_`/`-`。
_HEADER_SYNONYMS: dict = {
    "entity": ["entity", "实体", "对象", "主体", "公司", "entityname", "所属实体"],
    "field_name": ["fieldname", "字段名", "字段", "属性", "项目", "名称", "field"],
    "field_value": ["fieldvalue", "字段值", "值", "内容", "内容值", "value", "取值"],
    "standard_question": ["standardquestion", "question", "问题", "标准问题", "提问",
                          "问", "题目", "用户问题"],
    "official_answer": ["officialanswer", "answer", "答案", "标准答案", "回答",
                        "答", "回复", "官方回答"],
    "category": ["category", "分类", "类别", "分组", "目录"],
    "usage_status": ["usagestatus", "状态", "使用状态", "启用状态"],
    "followup_logic": ["followuplogic", "后续逻辑", "跟进逻辑", "后续"],
    "id": ["id", "编号", "标识", "问答编号"],
}

_INJECTION_LEADS = ("=", "+", "-", "@", "\t", "\r",
                    "＝", "＋", "－", "＠")


def _norm_header(text: str) -> str:
    t = re.sub(r"[\s_\-]+", "", (text or "").strip().lower())
    return t


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _sanitize_cell(text: str) -> tuple:
    """返回 (安全文本, 是否被中和)。公式注入前导一律加 `'` 前缀。"""
    if text and text.lstrip().startswith(_INJECTION_LEADS):
        return "'" + text, True
    return text, False


def _load_workbook(content: bytes):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(
            f"Excel 文件过大（{len(content)} 字节，上限 {MAX_FILE_BYTES}）")
    try:
        # data_only=True：读公式缓存值，不读公式串（见模块注释）。
        return openpyxl.load_workbook(
            io.BytesIO(content), data_only=True, read_only=False)
    except zipfile.BadZipFile:
        raise ValueError("Excel 解析失败：不是有效的 .xlsx 文件")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Excel 解析失败：{type(e).__name__}")


def _read_grid(ws):
    """读出字符串网格；整文件防火墙（维度/超大格）在此统一执行。"""
    if ws.max_column > MAX_COLS_PER_SHEET:
        raise ValueError(
            f"工作表《{ws.title}》列数超限（{ws.max_column}，上限 {MAX_COLS_PER_SHEET}）")
    if ws.max_row > MAX_ROWS_PER_SHEET:
        raise ValueError(
            f"工作表《{ws.title}》行数超限（{ws.max_row}，上限 {MAX_ROWS_PER_SHEET}）")
    grid: list = []
    neutralized = 0
    for row in ws.iter_rows():
        cells = list(row)[:MAX_COLS_PER_SHEET]
        texts: list = []
        for c in cells:
            s = _stringify(c.value).strip()
            if len(s) > MAX_CELL_CHARS:
                raise ValueError(
                    f"工作表《{ws.title}》单元格 {c.coordinate} 超大"
                    f"（{len(s)} 字符，上限 {MAX_CELL_CHARS}），已拒绝整个文件")
            s, hit = _sanitize_cell(s)
            neutralized += 1 if hit else 0
            texts.append(s)
        # 去尾部全空列，保持矩形。
        while texts and not texts[-1].strip("'"):
            texts.pop()
        grid.append(texts)
    width = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (width - len(r)) for r in grid]
    return grid, neutralized


def _detect_header(grid: list) -> int | None:
    """返回表头行下标（0-based），找不到返回 None。"""
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        if sum(1 for c in row if c.strip("'")) >= 2:
            return i
    return None


def _sniff_candidates(samples: list) -> list:
    """内容嗅探（低置信，仅当表头无命中时兜底）。"""
    vals = [s for s in samples if s]
    if not vals:
        return [{"role": "ignore", "confidence": 0.3, "reason": "该列无内容，默认忽略"}]
    n = len(vals)
    q_marks = sum(1 for v in vals if v.rstrip().endswith(("?", "？"))
                  or re.search(r"(吗|呢|多少|怎么|什么|如何|是否|可以|多久|哪里).{0,4}$", v))
    avg_len = sum(len(v) for v in vals) / n
    uniq_ratio = len(set(vals)) / n
    out = []
    if q_marks / n >= 0.5:
        out.append({"role": "standard_question", "confidence": 0.5,
                    "reason": f"内容嗅探：{q_marks}/{n} 格似问句"})
    if avg_len >= 15:
        out.append({"role": "official_answer", "confidence": 0.45,
                    "reason": f"内容嗅探：平均长度 {avg_len:.0f} 字，似答案"})
    if uniq_ratio <= 0.5 and avg_len <= 12:
        out.append({"role": "entity", "confidence": 0.45,
                    "reason": "内容嗅探：取值重复且短，似实体列"})
        out.append({"role": "category", "confidence": 0.4,
                    "reason": "内容嗅探：取值重复且短，似分类列"})
    out.append({"role": "ignore", "confidence": 0.3, "reason": "默认忽略"})
    return out


def _propose_column(header: str, samples: list) -> list:
    norm = _norm_header(header.strip("'"))
    if norm:
        for role in _HEADER_SYNONYMS:
            if norm == _norm_header(role):
                return [{"role": role, "confidence": 0.95,
                         "reason": f"表头“{header}”与角色名一致"}]
        for role, syns in _HEADER_SYNONYMS.items():
            if norm in syns:
                return [{"role": role, "confidence": 0.85,
                         "reason": f"表头“{header}”命中同义词（{role}）"}]
    return _sniff_candidates(samples)


def _sheet_proposal(ws, grid: list, neutralized: int) -> dict:
    header_idx = _detect_header(grid)
    if header_idx is None:
        headers = [f"COL-{get_column_letter(i + 1)}"
                   for i in range(len(grid[0]) if grid else 0)]
        data = grid
    else:
        headers = [h if h else f"COL-{get_column_letter(i + 1)}"
                   for i, h in enumerate(grid[header_idx])]
        data = grid[header_idx + 1:]
    data = [r for r in data if any(c.strip("'") for c in r)]
    n_cols = len(headers)
    columns = []
    suggested: dict = {}
    for i in range(n_cols):
        col_samples = [r[i] for r in data if r[i]][:50]
        preview = [r[i] for r in data if r[i]][:PREVIEW_SAMPLES]
        cands = _propose_column(headers[i], col_samples)
        columns.append({"index": i, "header": headers[i],
                        "samples": preview, "candidates": cands})
        best = cands[0]
        if best["role"] != "ignore" and best["confidence"] >= SUGGEST_THRESHOLD:
            # 同一角色不重复建议（第一列优先）。
            if best["role"] not in suggested.values():
                suggested[str(i)] = best["role"]
    warnings = []
    if header_idx is None:
        warnings.append("未识别表头（前 %d 行无≥2 非空列），需人工映射" % HEADER_SCAN_ROWS)
    if neutralized:
        warnings.append(f"中和公式注入前导 {neutralized} 格（加 ' 前缀）")
    # data_only 缓存缺失无法区分“空值”与“公式无缓存”，如实说明。
    warnings.append("公式按缓存值读取（data_only=True），无缓存的公式格视为空")
    return {"sheet": ws.title,
            "header_row": (header_idx + 1) if header_idx is not None else None,
            "n_rows": len(data), "n_cols": n_cols, "headers": headers,
            "columns": columns, "suggested_mapping": suggested,
            "warnings": warnings}


def parse_excel_preview(content: bytes, filename: str | None = None) -> dict:
    """dry-run：只读提案，不写库。"""
    wb = _load_workbook(content)
    names = wb.sheetnames
    if not names:
        raise ValueError("Excel 无工作表")
    if len(names) > MAX_SHEETS:
        raise ValueError(f"工作表数超限（{len(names)}，上限 {MAX_SHEETS}）")
    sheets = []
    for name in names:
        ws = wb[name]
        grid, neutralized = _read_grid(ws)
        if not any(any(c.strip("'") for c in r) for r in grid):
            sheets.append({"sheet": name, "header_row": None, "n_rows": 0,
                           "n_cols": 0, "headers": [], "columns": [],
                           "suggested_mapping": {},
                           "warnings": ["空表，无可用数据"]})
            continue
        sheets.append(_sheet_proposal(ws, grid, neutralized))
    return {"format": "excel", "filename": filename or "upload.xlsx",
            "sheets": sheets}


def _normalize_mapping(mapping: dict, wb) -> dict:
    """严格校验用户映射（不信任前端结构）；返回 {sheet: {col: role, ...}, ...}。

    允许的特殊键：每表 `__defaults__` = {"entity": ..., "category": ...}（可选）。
    """
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("commit 映射必填（preview 只读提案，不做导入依据）")
    out: dict = {}
    for sheet, spec in mapping.items():
        if sheet not in wb.sheetnames:
            raise ValueError(f"映射非法：未知工作表《{sheet}》")
        if not isinstance(spec, dict):
            raise ValueError(f"映射非法：《{sheet}》映射须为对象")
        ws = wb[sheet]
        ncols = ws.max_column
        cols: dict = {}
        defaults: dict = {}
        for k, v in spec.items():
            if k == SPECIAL_DEFAULTS_KEY:
                if not isinstance(v, dict):
                    raise ValueError(f"映射非法：《{sheet}》默认值须为对象")
                for dk, dv in v.items():
                    if dk not in _MUTABLE_DEFAULT_ROLES:
                        raise ValueError(f"映射非法：《{sheet}》不支持默认值 {dk!r}")
                    if not isinstance(dv, str) or not dv.strip() or len(dv.strip()) > 64:
                        raise ValueError(f"映射非法：《{sheet}》默认值 {dk} 非法")
                    defaults[dk] = dv.strip()
                continue
            try:
                col = int(k)
            except (TypeError, ValueError):
                raise ValueError(f"映射非法：《{sheet}》列键 {k!r} 非整数")
            if not 0 <= col < ncols:
                raise ValueError(f"映射非法：《{sheet}》列 {col} 越界（0–{ncols - 1}）")
            if v not in ROLES:
                raise ValueError(f"映射非法：《{sheet}》列 {col} 角色 {v!r} 未知")
            if v != "ignore" and v in cols.values():
                raise ValueError(f"映射非法：《{sheet}》角色 {v!r} 重复映射到多列")
            cols[col] = v
        if not cols and not defaults:
            raise ValueError(f"映射非法：《{sheet}》空映射")
        out[sheet] = {"cols": cols, "defaults": defaults}
    return out


def apply_excel_mapping(content: bytes, mapping: dict) -> dict:
    """服务端重载 + 重校验 + 产出原始条目（再交 validator/compiler）。"""
    wb = _load_workbook(content)
    norm = _normalize_mapping(mapping, wb)
    qa_items: list = []
    field_items: list = []
    skipped = 0
    neutralized_total = 0
    for sheet, spec in norm.items():
        ws = wb[sheet]
        grid, neutralized = _read_grid(ws)
        neutralized_total += neutralized
        header_idx = _detect_header(grid)
        data = grid[(header_idx + 1) if header_idx is not None else 0:]
        cols, defaults = spec["cols"], spec["defaults"]
        for row in data:
            got = {}
            for col, role in cols.items():
                val = row[col].strip() if col < len(row) else ""
                if role == "ignore" or not val.strip("'"):
                    continue
                got[role] = val
            for dk, dv in defaults.items():
                got.setdefault(dk, dv)
            made = False
            q = got.get("standard_question", "").strip()
            a = got.get("official_answer", "").strip()
            if q.strip("'") and a.strip("'"):
                item = {"standard_question": q, "official_answer": a}
                for opt in ("category", "usage_status", "followup_logic", "id"):
                    if got.get(opt, "").strip():
                        item[opt] = got[opt].strip()
                qa_items.append(item)
                made = True
            e = got.get("entity", "").strip()
            n = got.get("field_name", "").strip()
            v = got.get("field_value", "").strip()
            if e.strip("'") and n.strip("'") and v.strip("'"):
                field_items.append({"entity": e, "field_name": n, "field_value": v})
                made = True
            if not made:
                skipped += 1
    if not qa_items and not field_items:
        raise ValueError("映射未产出任何条目（行全空或全被忽略），拒绝空导入")
    return {"qa_items": qa_items, "field_items": field_items,
            "skipped": skipped, "neutralized": neutralized_total}
