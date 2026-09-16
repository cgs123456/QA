"""interview-copilot 本地运维 CLI（P9 F4.5/F4.6/F11.6）。

用法（仓库根）：
    python -m cli export --store-id xxx --format json --out bak.json
    python -m cli export --store-name 演示库 --format md --out bak.md
    python -m cli import --file bak.json --store-name 恢复库
    python -m cli import --file bak.md --store-id xxx --overwrite

- 直接读库，不经 HTTP：CLI 与 sidecar 同机，R2 token 握手是给 Rust 壳的；
  一致性由共用 `database/` 层保证（读用只读连接，写走同一 connect+迁移）。
- `--db` 缺省与 sidecar 同库（`default_db_path()`，可用 INTERVIEWCOPILOT_DB 覆盖）。
- 退出码：0 成功；1 运行失败（原因已打 stderr）；2 参数用法错误（argparse）。
"""

import argparse
import sys

from .commands import do_export, do_import


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="interview-copilot",
                                 description="本地知识库导出/恢复（直接读库）")
    sub = ap.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="按 store 导出 JSON/Markdown")
    ex.add_argument("--store-id", default=None)
    ex.add_argument("--store-name", default=None)
    ex.add_argument("--format", choices=("json", "md"), default="json")
    ex.add_argument("--out", default=None, help="缺省输出到 stdout")
    ex.add_argument("--db", default=None)

    im = sub.add_parser("import", help="从导出文件恢复")
    im.add_argument("--file", required=True)
    im.add_argument("--format", choices=("json", "md"), default=None,
                    help="缺省按后缀推断（.json/.md/.markdown）")
    im.add_argument("--store-id", default=None)
    im.add_argument("--store-name", default=None)
    im.add_argument("--overwrite", action="store_true",
                    help="目标非空时先清空再入库（不加则非空拒绝）")
    im.add_argument("--db", default=None)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "export":
        return do_export(store_id=args.store_id, store_name=args.store_name,
                         format=args.format, out=args.out, db=args.db)
    if args.command == "import":
        return do_import(file=args.file, format=args.format,
                         store_id=args.store_id, store_name=args.store_name,
                         overwrite=args.overwrite, db=args.db)
    return 2  # 不可达（required=True），防未来加子命令漏分支


if __name__ == "__main__":
    sys.exit(main())
