"""知识库导出 / 恢复（P9 F4.5/F4.6/F11.6）。

两条通道，用途不同， fidelity 不同：

- **JSON**：忠实通道。全列（含 id / usage_status / followup_logic / aliases），
  同一内容多次导出逐字节相同（无时钟字段、无集合迭代），且
  export → import（新库）→ export **逐字节等价**。id 稳定策略：导出带 id；
  恢复时 id 空闲即保留，被占则 compiler 换新 id（既有行为，见 compiler 注释）。
- **Markdown**：人类可读回放（H1/H2 结构与 `markdown_parser` 契约对齐）。
  有损边界（已文档化、单测锁定）：① 字段多段 value 只回首段；
  ② 别名不渲染（重建时由 `extract` 按词表 + 快照内显式 aliases 恢复）；
  ③ 空白归一（见 `_norm_*`）。MD 往返保证的是“规范内容稳定”，
  JSON 才是逐字节契约。

列清单纪律（坑位）：导出列由 `*_EXPORT_COLUMNS` 常量显式声明；
`test_export_import.py::test_export_covers_all_columns` 断言清单 ==
线上 schema 列 − {作用域列，外生状态列}。迁移加列必须同步更新清单，
否则测试变红——新列漏导出在此被卡住，而不是在用户丢数据时才发现。

读一致性：导出函数只做 SELECT，不开写事务（调用方提供连接即可；
CLI 用 `mode=ro` 只读连接 + user_version 校验，见 `cli/commands.py`）。
"""

import json

from .stores import StoreNotFound, get_store

FORMAT_TAG = "interview-copilot-store"
SNAPSHOT_VERSION = 1

# 导出列清单（顺序即快照键顺序，决定字节稳定性）：
# - qa_pairs：去掉 store_id（导出以 store 为作用域）、created_at（时钟，逐字节杀手）。
# - fields：去掉 store_id；aliases 取 fields.aliases JSON 列（规范源），
#   field_aliases 展开表是派生数据，恢复时由 extract 重建。
# - stores：去掉 created_at（时钟）、is_current（本机状态，非内容）。
QA_EXPORT_COLUMNS = (
    "id", "standard_question", "official_answer", "category",
    "usage_status", "followup_logic",
)
FIELD_EXPORT_COLUMNS = ("id", "entity", "field_name", "field_value", "aliases")
STORE_EXPORT_COLUMNS = ("id", "name", "template_id")

# 上面三清单相对线上 schema 的合法排除项（列锁定测试用，改这里要改测试）。
_SCHEMA_EXCLUSIONS = {
    "qa_pairs": {"store_id", "created_at"},
    "fields": {"store_id"},
    "stores": {"created_at", "is_current"},
}

_QA_ORDER = "ORDER BY rowid"
_FIELD_ORDER = "ORDER BY rowid"


def _aliases_of(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except ValueError:
        return []
    return [a for a in val if isinstance(a, str) and a.strip()] if isinstance(val, list) else []


def export_store_snapshot(conn, store_id: str) -> dict:
    """读出某库全量快照（只 SELECT）。不存在 → StoreNotFound。"""
    store = get_store(conn, store_id)  # 不存在即抛
    qa_rows = conn.execute(
        "SELECT id, standard_question, official_answer, category,"
        " usage_status, followup_logic FROM qa_pairs"
        " WHERE store_id=? " + _QA_ORDER,
        (store_id,),
    ).fetchall()
    field_rows = conn.execute(
        "SELECT id, entity, field_name, field_value, aliases FROM fields"
        " WHERE store_id=? " + _FIELD_ORDER,
        (store_id,),
    ).fetchall()
    return {
        "format": FORMAT_TAG,
        "version": SNAPSHOT_VERSION,
        "store": {
            "id": store["id"],
            "name": store["name"],
            "template_id": store["template_id"],
        },
        "qa_pairs": [
            {
                "id": r[0],
                "standard_question": r[1],
                "official_answer": r[2],
                "category": r[3],
                "usage_status": r[4],
                "followup_logic": r[5],
            }
            for r in qa_rows
        ],
        "fields": [
            {
                "id": r[0],
                "entity": r[1],
                "field_name": r[2],
                "field_value": r[3],
                "aliases": _aliases_of(r[4]),
            }
            for r in field_rows
        ],
    }


def snapshot_to_json(snapshot: dict) -> str:
    """快照 → 确定性 JSON 文本（ensure_ascii=False + indent=2；无时钟字段故稳定）。"""
    return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"


def write_json_snapshot(conn, store_id: str, fp, batch: int = 500) -> int:
    """分块流式写 JSON 快照（大库常数内存；与 snapshot_to_json 逐字节一致）。

    返回 qa+fields 总行数。batch 为每批行数（仅影响 IO 次数，不影响字节）。
    """
    snap = export_store_snapshot(conn, store_id)
    fp.write('{\n  "format": ')
    fp.write(json.dumps(snap["format"], ensure_ascii=False))
    fp.write(',\n  "version": ')
    fp.write(str(snap["version"]))
    fp.write(',\n  "store": ')
    fp.write(json.dumps(snap["store"], ensure_ascii=False, indent=2).replace("\n", "\n  "))
    # 上面 indent=2 的 store 块整体缩进 2 格，与 snapshot_to_json 的嵌套缩进对齐。
    fp.write(',\n  "qa_pairs": [')
    qa = snap["qa_pairs"]
    for i in range(0, len(qa), batch):
        for j, item in enumerate(qa[i:i + batch], start=i):
            fp.write("\n    " if j == 0 else ",\n    ")
            fp.write(json.dumps(item, ensure_ascii=False, indent=2).replace("\n", "\n    "))
    fp.write("\n  ],\n" if qa else "],\n")
    fp.write('  "fields": [')
    fields = snap["fields"]
    for i in range(0, len(fields), batch):
        for j, item in enumerate(fields[i:i + batch], start=i):
            fp.write("\n    " if j == 0 else ",\n    ")
            fp.write(json.dumps(item, ensure_ascii=False, indent=2).replace("\n", "\n    "))
    fp.write("\n  ]\n" if fields else "]\n")
    fp.write("}\n")
    return len(qa) + len(fields)


def _norm_text_block(text: str) -> str:
    """MD 渲染归一：去首尾空行，3+ 连空行压成 1 个（parser 本就忽略空行数）。"""
    lines = (text or "").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out, blanks = [], 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line.rstrip())
        elif not blanks:
            blanks = 1
            out.append("")
    return "\n".join(out)


def _norm_single_line(text: str) -> str:
    """Q/A 单行归一：一切空白串（含换行）压成单个空格（parser 按行 join " "）。"""
    return " ".join((text or "").split())


def _is_question(text: str) -> bool:
    return text.rstrip().endswith(("?", "？"))


def render_markdown(snapshot: dict) -> str:
    """快照 → 规范 Markdown（`markdown_parser` 可完整回读的子集）。

    结构：按 entity 分 `# H1`（entity 取 QA.category，无 category 取 "default"，
    与 parser 缺 H1 语义同源）；entity 内先字段（`## name` + value），
    后问答——`?` 结尾走 `## H2`（答案换行保留），其余走 `Q:/A:` 块
    （同 H2 下可多组，parser 逐组提取）。
    """
    _check_snapshot(snapshot)
    entities: dict = {}
    order: list = []

    def _bucket(entity):
        entity = (entity or "").strip() or "default"
        if entity not in entities:
            entities[entity] = {"fields": [], "qa": []}
            order.append(entity)
        return entities[entity]

    for f in snapshot["fields"]:
        _bucket(f["entity"])["fields"].append(f)
    for q in snapshot["qa_pairs"]:
        _bucket(q.get("category"))["qa"].append(q)

    parts: list = []
    for entity in order:
        parts.append(f"# {entity}\n")
        for f in entities[entity]["fields"]:
            parts.append(f"## {f['field_name']}\n")
            parts.append(f"{_norm_text_block(f['field_value'])}\n")
        qa_blocks = []
        for q in entities[entity]["qa"]:
            question = _norm_single_line(q["standard_question"])
            if not question:
                continue
            if _is_question(question):
                parts.append(f"## {question}\n")
                parts.append(f"{_norm_text_block(q['official_answer'])}\n")
            else:
                qa_blocks.append(
                    f"Q：{question}\nA：{_norm_single_line(q['official_answer'])}\n"
                )
        if qa_blocks:
            parts.append("## 问答\n")
            parts.extend(qa_blocks)
    return "\n".join(parts)


def _check_snapshot(snapshot) -> dict:
    """快照形状校验（不信任输入：类型/版本/顶层键，明细错进 ValueError）。"""
    if not isinstance(snapshot, dict):
        raise ValueError("快照顶层必须是 object")
    if snapshot.get("format") != FORMAT_TAG:
        raise ValueError(f"未知快照格式：{snapshot.get('format')!r}")
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"不支持的快照版本：{snapshot.get('version')!r}")
    for key in ("store", "qa_pairs", "fields"):
        if key not in snapshot:
            raise ValueError(f"快照缺顶层键：{key}")
    if not isinstance(snapshot["qa_pairs"], list) or not isinstance(
        snapshot["fields"], list
    ):
        raise ValueError("快照 qa_pairs/fields 必须是 array")
    if not isinstance(snapshot["store"], dict):
        raise ValueError("快照 store 必须是 object")
    return snapshot


def restore_store(conn, snapshot: dict, store_id=None, store_name=None,
                  overwrite: bool = False) -> dict:
    """从快照恢复：复用 validator + extract + compiler（同 P1 导入同一校验与短事务）。

    目标：store_id 存在则用；否则 store_name 存在则用；否则新建
    （默认名取快照 store 名）。目标非空且 overwrite=False → 拒绝（覆盖须确认）；
    overwrite=True → 先清空内容（保留 store 行）再入库。
    返回 compiler stats（调用方可再 update 附加计数）。
    """
    from .compiler import compile_store
    from .field_extractor import extract, load_vocab
    from .validator import validate_field_items, validate_qa_items
    from .stores import clear_store_content, create_store, get_store

    _check_snapshot(snapshot)
    if store_id is not None:
        target = get_store(conn, store_id)  # 不存在即抛 StoreNotFound
    elif store_name is not None:
        target = _get_or_create_by_name(conn, store_name)
    else:
        snap_name = (snapshot["store"].get("name") or "").strip() or "导入库"
        target = _get_or_create_by_name(conn, snap_name)
    tid = target["id"]

    qa_count = conn.execute(
        "SELECT COUNT(*) FROM qa_pairs WHERE store_id=?", (tid,)
    ).fetchone()[0]
    field_count = conn.execute(
        "SELECT COUNT(*) FROM fields WHERE store_id=?", (tid,)
    ).fetchone()[0]
    if (qa_count or field_count) and not overwrite:
        raise ValueError(
            f"目标库非空（问答 {qa_count} / 字段 {field_count}），"
            "覆盖须显式确认（overwrite=True）"
        )
    if overwrite and (qa_count or field_count):
        clear_store_content(conn, tid)

    qa_items, _ = validate_qa_items(snapshot["qa_pairs"])
    field_raw, _ = validate_field_items(snapshot["fields"])
    field_items, miss = extract(field_raw, load_vocab())
    return compile_store(conn, tid, qa_items, field_items, vocab_miss=miss)


def _get_or_create_by_name(conn, name: str) -> dict:
    from .stores import create_store

    row = conn.execute("SELECT id FROM stores WHERE name=?", (name.strip(),)).fetchone()
    if row:
        from .stores import get_store

        return get_store(conn, row[0])
    return create_store(conn, name)
