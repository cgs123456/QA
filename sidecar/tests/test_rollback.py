"""R6 回滚测试：编译中途 SIGKILL → 重启后 qa/vec 行数一致、vec 零残留。

Windows Popen.kill() = TerminateProcess，不可捕获，等价 SIGKILL。
victim 的 4000 行编译（FTS 中文索引 + vec 写入，单短事务，窗口以秒计），
父进程在 sentinel 出现后 0.2s 杀掉，必定位于事务提交前。
"""

import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(TESTS_DIR.parents[1] / "src"))

from database.connection import connect

N = 4000


def _wait_for(path: Path, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"sentinel 未出现：{path}")


def test_sigkill_mid_compile_rolls_back(tmp_path):
    db_path = tmp_path / "kill.db"
    sentinel = tmp_path / "ready"
    err_log = tmp_path / "victim.stderr.log"
    with open(err_log, "w", encoding="utf-8") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, str(TESTS_DIR / "compile_victim.py"),
             str(db_path), "killstore", str(N), str(sentinel)],
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
        )
        try:
            _wait_for(sentinel)
            time.sleep(0.2)
            proc.kill()  # TerminateProcess：不可捕获（等价 SIGKILL）
            ret = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                ret = proc.wait(timeout=60)

    err_text = err_log.read_text(encoding="utf-8", errors="replace")
    assert ret != 0, "victim 竟正常退出——kill 未命中，测试无效"
    assert "Traceback" not in err_text, f"victim 自行崩溃而非被 kill：{err_text[-500:]}"

    conn = connect(str(db_path))
    try:
        qa_count = conn.execute("SELECT COUNT(*) FROM qa_pairs").fetchone()[0]
        vec_count = conn.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM vec_qa_local v LEFT JOIN qa_pairs q"
            " ON q.id = v.qa_id WHERE q.id IS NULL"
        ).fetchone()[0]
        qa_without_vec = conn.execute(
            "SELECT COUNT(*) FROM qa_pairs q LEFT JOIN vec_qa_local v"
            " ON v.qa_id = q.id WHERE v.qa_id IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"\n[rollback] qa={qa_count} vec={vec_count} orphans={orphans}")
    assert orphans == 0, "孤儿向量残留"
    assert qa_count == vec_count, "qa/vec 行数不一致（事务被撕裂）"
    assert (qa_count, vec_count) == (0, 0), (
        f"kill 未命中事务窗口（qa={qa_count}），请调大 N 或 sleep 后重跑"
    )
    assert qa_without_vec == 0
