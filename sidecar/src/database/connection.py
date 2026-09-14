"""SQLite 连接管理（R1/R3）：Python sidecar 是唯一 DB 访问者。

连接建立顺序（R3）：enable_load_extension → load libsimple →
jieba_dict(绝对路径) → load sqlite-vec。

路径锚定：BASE = INTERVIEWCOPILOT_BASE（环境变量覆盖）
  → sys._MEIPASS（PyInstaller 打包态）
  → 项目根（开发态）。
vendor/dict 与 DB 默认路径都由 BASE 导出为绝对路径，永不依赖 CWD。

并发：单连接（R3）。sqlite3 连接以 check_same_thread=False 创建，
写操作必须持有 write_lock 串行化；读（SELECT）可并发（WAL 下读不被写阻塞）。
RLock：防未来嵌套调用自死锁。
"""

import os
import sqlite3
import sys
import threading
from pathlib import Path

REQUIRED_DICT_FILES = [
    "jieba.dict.utf8",
    "hmm_model.utf8",
    "user.dict.utf8",
    # task-3 实测：缺 idf.utf8 时 jieba_query 触发扩展内 abort()（不可捕获），
    # 故 idf/stop_words 同为必需（PRD §3.13 原注已过时）。
    "idf.utf8",
    "stop_words.utf8",
]

write_lock = threading.RLock()

_singleton = None
_singleton_lock = threading.Lock()


def _platform_lib_name() -> str:
    if sys.platform == "win32":
        return "libsimple.dll"
    if sys.platform == "darwin":
        return "libsimple.dylib"
    return "libsimple.so"


def resolve_base() -> Path:
    """资源锚点（绝对路径，只读资源用）：env 覆盖 → 打包态 → 开发态 sidecar 目录。

    注：BASE 仅锚定只读资源（vendor/dict、models）。可写数据（DB）打包态走
    用户数据目录（见 default_db_path），绝不进 bundle。
    （任务原文写「项目根（开发）」，但 vendor/ 实际位于 sidecar/ 下；
    为使 vendor 恒为 BASE/vendor（与打包态 _MEIPASS/vendor 同构），
    开发态 BASE 取 sidecar 目录。不变的是 R3 要求：绝对锚定 + env 可覆盖。）
    """
    override = os.environ.get("INTERVIEWCOPILOT_BASE")
    if override:
        return Path(override).resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return Path(meipass).resolve()
    # 开发态：本文件 sidecar/src/database/connection.py → parents[2] = sidecar/。
    return Path(__file__).resolve().parents[2]


def vendor_dir() -> Path:
    return resolve_base() / "vendor"


def dict_dir() -> Path:
    return vendor_dir() / "dict"


def libsimple_path() -> Path:
    return vendor_dir() / _platform_lib_name()


def default_db_path() -> Path:
    override = os.environ.get("INTERVIEWCOPILOT_DB")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        # 打包态：写数据绝不进 bundle（_MEIPASS 只读假设 + 避免污染体积基线），
        # 进 OS 用户数据目录。
        return _user_data_dir() / "interviewcopilot.db"
    return resolve_base() / "data" / "interviewcopilot.db"


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "InterviewCopilot" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "InterviewCopilot" / "data"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "interviewcopilot" / "data"


def check_vendor_files() -> list:
    """返回缺失的 vendor 相对路径（空 = 齐全）。用于启动自检（缺文件即拒启，
    绝不在缺 idf 时进入查询——扩展会 abort 整个进程）。"""
    missing = []
    lib = libsimple_path()
    if not lib.is_file():
        missing.append(str(lib.name))
    d = dict_dir()
    for name in REQUIRED_DICT_FILES:
        if not (d / name).is_file():
            missing.append(f"dict/{name}")
    return missing


def connect(db_path) -> sqlite3.Connection:
    """新建一个完整初始化的连接（WAL + 双扩展 + 词典）。失败抛 RuntimeError
   （带诊断），调用方应视为致命错误（打日志到 stderr 后非零退出）。"""
    db_path = Path(db_path)
    if db_path.name != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    except sqlite3.Error as e:
        raise RuntimeError(f"SQLite 连接失败 db={db_path}：{e}") from e

    if db_path.name != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if not hasattr(conn, "enable_load_extension"):
        conn.close()
        raise RuntimeError(
            "当前 Python 发行版裁剪了 sqlite3 load_extension 能力："
            "请更换官方 Python 构建。"
        )
    conn.enable_load_extension(True)

    lib = libsimple_path()
    try:
        conn.load_extension(str(lib))
    except sqlite3.Error as e:
        conn.close()
        raise RuntimeError(
            f"libsimple 加载失败（疑似 SQLite 版本差异/扩展变体错误）："
            f"sqlite_version={sqlite3.sqlite_version} lib={lib} error={e}"
        ) from e

    try:
        conn.execute("SELECT jieba_dict(?)", (str(dict_dir()),))
    except sqlite3.Error as e:
        conn.close()
        raise RuntimeError(f"jieba_dict 初始化失败 dict={dict_dir()} error={e}") from e

    try:
        import sqlite_vec

        sqlite_vec.load(conn)
    except Exception as e:
        conn.close()
        raise RuntimeError(f"sqlite-vec 加载失败：{e}") from e

    return conn


def get_connection(db_path=None) -> sqlite3.Connection:
    """进程单例连接（R3）。首次调用建连（含迁移由调用方执行一次）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = connect(
                    default_db_path() if db_path is None else db_path
                )
    return _singleton
