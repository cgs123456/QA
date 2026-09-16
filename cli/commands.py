"""本地运维命令实现（thin：参数已由 __main__ 校验，这里只做事）。

- 直接读库，不经 HTTP：CLI 与 sidecar 同机，R2 的 token 握手是给 Rust 壳的，
  本地工具走数据库层（`database.connection` + `run_migrations`）保证一致性。
- 导出只读：`mode=ro` 连接 + user_version 校验，不跑迁移（读路径不写库）；
  版本对不上就大声失败，提示先跑一次 sidecar（由它做迁移）。
- 恢复走写连接：与 sidecar 同一 `connect()` + `run_migrations`，再调
  `exporter.restore_store`（复用 validator/extract/compiler 短事务）。
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))

from database.connection import connect, default_db_path  # noqa: E402
from database.schema import CURRENT_VERSION, run_migrations  # noqa: E402
from knowledge.exporter import (  # noqa: E402
    render_markdown,
    restore_store,
    snapshot_to_json,
    write_json_snapshot,
)
from knowledge.stores import StoreNotFound  # noqa: E402


def resolve_db(db_arg: str | None) -> Path:
    if db_arg:
        return Path(db_arg)
    return default_db_path()


def _connect_ro(db_path: Path):
    """只读连接（URI mode=ro；不跑迁移，不写 user_version）。"""
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != CURRENT_VERSION:
        conn.close()
        raise RuntimeError(
            f"数据库 schema 版本为 {version}，本工具只读 v{CURRENT_VERSION}："
            "请先跑一次 sidecar 完成迁移后再导出（读路径不做写迁移）"
        )
    return conn


def do_export(*, store_id=None, store_name=None, format="json", out=None,
              db=None) -> int:
    """导出某库。返回码：0 成功；1 运行失败（stderr 已说明）。"""
    from knowledge.stores import get_store

    db_path = resolve_db(db)
    try:
        conn = _connect_ro(db_path)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"export 失败：{e}", file=sys.stderr, flush=True)
        return 1
    try:
        if store_id is not None:
            sid = get_store(conn, store_id)["id"]
        elif store_name is not None:
            row = conn.execute(
                "SELECT id FROM stores WHERE name=?", (store_name.strip(),)
            ).fetchone()
            if row is None:
                print(f"export 失败：知识库不存在（name={store_name.strip()}）",
                      file=sys.stderr, flush=True)
                return 1
            sid = row[0]
        else:
            print("export 失败：须指定 --store-id 或 --store-name",
                  file=sys.stderr, flush=True)
            return 1
        if format == "json":
            if out is None:
                from knowledge.exporter import export_store_snapshot

                sys.stdout.write(snapshot_to_json(export_store_snapshot(conn, sid)))
                sys.stdout.flush()
            else:
                with open(out, "w", encoding="utf-8", newline="") as fp:
                    n = write_json_snapshot(conn, sid, fp)
                print(f"已导出 {n} 行 → {out}", flush=True)
        elif format == "md":
            from knowledge.exporter import export_store_snapshot

            text = render_markdown(export_store_snapshot(conn, sid))
            if out is None:
                sys.stdout.write(text)
                sys.stdout.flush()
            else:
                Path(out).write_text(text, encoding="utf-8")
                print(f"已导出 → {out}", flush=True)
        else:
            print(f"export 失败：未知格式 {format!r}（json|md）",
                  file=sys.stderr, flush=True)
            return 1
        return 0
    except StoreNotFound as e:
        print(f"export 失败：知识库不存在（id={e}）", file=sys.stderr, flush=True)
        return 1
    except OSError as e:
        print(f"export 失败：写文件出错：{e}", file=sys.stderr, flush=True)
        return 1
    finally:
        conn.close()


def do_import(*, file, format=None, store_id=None, store_name=None,
              overwrite=False, db=None) -> int:
    """从导出文件恢复。返回码：0 成功；1 运行失败；2 用法错误由 argparse 处理。"""
    path = Path(file)
    fmt = format
    if fmt is None:
        suffix = path.suffix.lower()
        if suffix == ".json":
            fmt = "json"
        elif suffix in (".md", ".markdown"):
            fmt = "md"
        else:
            print(f"import 失败：无法从后缀推断格式 {path.suffix!r}，请显式 --format",
                  file=sys.stderr, flush=True)
            return 1
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"import 失败：读文件出错：{e}", file=sys.stderr, flush=True)
        return 1
    try:
        if fmt == "json":
            snapshot = json.loads(text)
        elif fmt == "md":
            from knowledge.parsers.markdown_parser import parse_markdown

            parsed = parse_markdown(text, path.name)
            # MD 快照重建：parser 输出 + 空 store 骨架，再走统一校验入库。
            # id 缺席 → compiler 生成新 id（validator 允许缺 id）。
            snapshot = {
                "format": "interview-copilot-store",
                "version": 1,
                "store": {"id": None, "name": path.stem, "template_id": None},
                "qa_pairs": parsed["qa_items"],
                "fields": parsed["field_items"],
            }
        else:
            print(f"import 失败：未知格式 {fmt!r}（json|md）",
                  file=sys.stderr, flush=True)
            return 1
    except ValueError as e:
        print(f"import 失败：文件解析失败：{e}", file=sys.stderr, flush=True)
        return 1
    try:
        conn = connect(resolve_db(db))
        run_migrations(conn)
    except RuntimeError as e:
        print(f"import 失败：{e}", file=sys.stderr, flush=True)
        return 1
    try:
        stats = restore_store(conn, snapshot, store_id=store_id,
                              store_name=store_name, overwrite=overwrite)
    except StoreNotFound:
        print("import 失败：指定的 --store-id 不存在", file=sys.stderr, flush=True)
        return 1
    except ValueError as e:
        print(f"import 失败：{e}", file=sys.stderr, flush=True)
        return 1
    finally:
        conn.close()
    print(f"已恢复：问答新增 {stats['qa_inserted']}、字段 {stats['fields_upserted']}、"
          f"跳过 {stats['qa_skipped']}", flush=True)
    return 0
