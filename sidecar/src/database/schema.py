"""Schema 版本管理：PRAGMA user_version 由 sidecar 独占管理。"""

from .migrations import v001_initial

CURRENT_VERSION = v001_initial.TARGET_VERSION

_MIGRATIONS = {
    v001_initial.TARGET_VERSION: v001_initial.DDL_STATEMENTS,
}


def run_migrations(conn) -> int:
    """将 DB 迁移到 CURRENT_VERSION，返回迁移后版本号（幂等）。

    每版 DDL 包在一个事务里执行，失败整体回滚并抛出。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > CURRENT_VERSION:
        raise RuntimeError(
            f"DB user_version={version} 高于 sidecar 支持的 {CURRENT_VERSION}，拒绝启动"
        )
    while version < CURRENT_VERSION:
        version += 1
        statements = _MIGRATIONS[version]
        try:
            with conn:
                for ddl in statements:
                    conn.execute(ddl)
                conn.execute(f"PRAGMA user_version = {version}")
        except Exception as e:
            raise RuntimeError(f"迁移到 v{version:03d} 失败，已回滚：{e}") from e
    return version
