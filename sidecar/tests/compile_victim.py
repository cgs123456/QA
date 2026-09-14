"""SIGKILL 回滚测试的 victim 子进程（R6：qa+vec 同一短事务）。

用法：python compile_victim.py <db_path> <store_name> <n> <sentinel>
行为：建库/建表/建 store → 构造 n 条 QA + 512 维向量（事务外）→ 写 sentinel
→ compile_store（唯一短事务，含 FTS 索引 + vec 写入；窗口以秒计）。
父进程在 sentinel 出现后 kill（Windows TerminateProcess，等价 SIGKILL，
均不可捕获），重启后 qa/vec 行数必须一致、vec 零残留。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite_vec

from database.connection import connect
from database.schema import run_migrations
from knowledge.compiler import compile_store
from knowledge.stores import create_store


def main() -> None:
    db_path, store_name, n_str, sentinel = sys.argv[1:5]
    n = int(n_str)
    conn = connect(db_path)
    run_migrations(conn)
    store = create_store(conn, store_name)
    qa, emb = [], {}
    for i in range(n):
        qid = f"victim-{i:05d}"
        qa.append(
            {
                "id": qid,
                "standard_question": f"回滚测试问题{i:05d}第{i}号",
                "official_answer": f"回滚测试答案{i:05d}",
                "usage_status": "fixed",
            }
        )
        emb[qid] = sqlite_vec.serialize_float32(
            [((i * 7 + j) % 13) / 13.0 for j in range(512)]
        )
    Path(sentinel).write_text("ready", encoding="utf-8")
    compile_store(conn, store["id"], qa, [], embeddings=emb)
    conn.close()
    print(f"VICTIM_DONE n={n}")


if __name__ == "__main__":
    main()
