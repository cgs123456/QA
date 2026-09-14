"""simple 扩展五步冒烟（P0，关闭全项目唯一 P0 风险）。

步骤：① enable_load_extension + load libsimple → ② jieba_dict(vendor/dict 绝对路径)
→ ③ CREATE VIRTUAL TABLE t USING fts5(content, tokenize='simple')
→ ④ 插入中文 → ⑤ 双查询（jieba_query + simple_query 均命中），
第 ⑤ 步在【有数据状态下】追加重跑 MATCH / SELECT * / rebuild。

诊断纪律（任务要求）：失败时输出明确诊断（SQLite 版本差异 / 扩展变体错误 /
前置缺失），并停止——报告等待决策，不得绕过（不得用其他分词器代替、不得跳步）。

关键约束：
- jieba_dict() 只作用于【当前连接】：全文件共用同一个连接。
- 空表 MATCH 是假绿：一切 MATCH / rebuild 断言只在已插入数据后执行。
"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

VENDOR_DIR = (Path(__file__).resolve().parents[1] / "vendor").resolve()
DICT_DIR = VENDOR_DIR / "dict"
REQUIRED_DICT_FILES = ["jieba.dict.utf8", "hmm_model.utf8", "user.dict.utf8"]

if sys.platform == "win32":
    LIB_NAME = "libsimple.dll"
elif sys.platform == "darwin":
    LIB_NAME = "libsimple.dylib"
else:
    LIB_NAME = "libsimple.so"

LIB_PATH = VENDOR_DIR / LIB_NAME

SAMPLE_ZH = "你的发货周期是多久"

_timings: dict = {}


def _timed(step: str, fn):
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        _timings[step] = time.perf_counter() - t0


def _missing_prereqs() -> list:
    missing = []
    if not LIB_PATH.is_file():
        missing.append(f"扩展库缺失：{LIB_PATH}（{sys.platform} 应为 {LIB_NAME}）")
    for name in REQUIRED_DICT_FILES:
        if not (DICT_DIR / name).is_file():
            missing.append(f"词典缺失：{DICT_DIR / name}")
    return missing


@pytest.fixture(scope="module")
def conn():
    """步骤 ①+②：单连接上加载扩展并初始化词典。"""
    missing = _missing_prereqs()
    if missing:
        pytest.fail(
            "P0 冒烟前置缺失，停止等待决策（不得绕过）：\n  - "
            + "\n  - ".join(missing)
            + f"\n请将 {LIB_NAME} 放入 sidecar/vendor/，三词典放入 sidecar/vendor/dict/ 后重跑。"
        )
    assert hasattr(sqlite3.Connection, "enable_load_extension"), (
        "当前 Python 发行版裁剪了 sqlite3 load_extension 能力 "
        f"(sqlite_version={sqlite3.sqlite_version})：先更换官方 Python 构建再决策，不得绕过。"
    )
    c = sqlite3.connect(":memory:")

    def _load():
        c.enable_load_extension(True)
        c.load_extension(str(LIB_PATH))

    try:
        _timed("load_extension", _load)
    except sqlite3.OperationalError as e:
        c.close()
        pytest.fail(
            "load_extension 失败（疑似 SQLite 版本差异 / 扩展变体错误），停止等待决策："
            f"sqlite_version={sqlite3.sqlite_version}，lib={LIB_PATH}，error={e}。"
            "常见原因：扩展与 SQLite 大版本不兼容、32/64 位变体拿错（"
            "not a valid Win32 application 即变体错误）。"
        )

    def _dict():
        c.execute("SELECT jieba_dict(?)", (str(DICT_DIR),))

    try:
        _timed("jieba_dict", _dict)
    except sqlite3.Error as e:
        c.close()
        pytest.fail(f"jieba_dict 初始化失败，停止等待决策：dict={DICT_DIR}，error={e}。")
    yield c
    c.close()


def test_fts5_create_table(conn):
    """步骤 ③：建 simple 分词 FTS5 表。"""

    def _create():
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='simple')")

    _timed("create_table", _create)


def test_insert_chinese(conn):
    """步骤 ④：插入中文样本（后续一切 MATCH 的前提，反假绿）。"""

    def _insert():
        conn.execute("INSERT INTO t(content) VALUES (?)", (SAMPLE_ZH,))
        conn.commit()

    _timed("insert", _insert)
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 1, f"插入后行数异常：{n}"


def test_dual_query_with_data(conn):
    """步骤 ⑤：双查询均命中（jieba 词组路 + simple 拼音容错路）。"""

    def _jieba():
        return conn.execute(
            "SELECT content FROM t WHERE t MATCH jieba_query(?)", ("发货周期",)
        ).fetchall()

    def _simple():
        return conn.execute(
            "SELECT content FROM t WHERE t MATCH simple_query(?)", ("fahuo",)
        ).fetchall()

    rows_jieba = _timed("jieba_query", _jieba)
    rows_simple = _timed("simple_query", _simple)
    assert any(SAMPLE_ZH in r[0] for r in rows_jieba), f"jieba_query 未命中：{rows_jieba}"
    assert any(SAMPLE_ZH in r[0] for r in rows_simple), (
        f"simple_query 未命中：{rows_simple}"
    )


def test_rerun_with_data_present(conn):
    """步骤 ⑤ 追加：有数据状态下重跑 MATCH / SELECT * / rebuild。"""
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n > 0, "表为空时重跑是假绿：拒绝在空表上验证"
    rows = conn.execute(
        "SELECT content FROM t WHERE t MATCH jieba_query(?)", ("发货周期",)
    ).fetchall()
    assert rows, "有数据重跑 MATCH 无结果"
    all_rows = conn.execute("SELECT * FROM t").fetchall()
    assert len(all_rows) == n
    conn.execute("INSERT INTO t(t) VALUES('rebuild')")
    rows_after = conn.execute(
        "SELECT content FROM t WHERE t MATCH simple_query(?)", ("fahuo",)
    ).fetchall()
    assert rows_after, "rebuild 后 simple_query 无结果"


def test_report_timings():
    """打印五步各自耗时（供 docs/compatibility.md 落盘）。"""
    print("\n[simple-smoke-timings] " + ", ".join(
        f"{k}={v * 1000:.1f}ms" for k, v in _timings.items()
    ))
    print(f"[simple-smoke-env] sqlite_version={sqlite3.sqlite_version} lib={LIB_PATH}")
