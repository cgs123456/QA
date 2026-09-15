"""SIGKILL 中断回滚测试的 victim 子进程（P5：重建只写目标表，旧表 untouched）。

用法：python rebuild_victim.py <db_path> <sentinel> <done_marker>
行为：建库/建表/建 store → 300 条 QA + 512 维本地向量入库（短事务）→
启动真实 `routers.embedding._run_rebuild`（假 cloud provider，每批 sleep
拉长窗口，首批写完后写 sentinel）→ 完成后写 done_marker。
父进程在 sentinel 出现后 kill（Windows TerminateProcess，等价 SIGKILL，
均不可捕获），重启后旧表必须逐行一致、目标表全维度正确、无翻转标记。
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite_vec

import routers.embedding as emb_router
from database.connection import connect
from database.schema import run_migrations
from knowledge.compiler import compile_store
from knowledge.stores import create_store
from retrieval.embedding import EmbeddingError  # noqa: F401 — 断言 kind 用

N = 300


def _unit(i, dim=512):
    v = [0.0] * dim
    v[i % dim] = 1.0
    return v


class SlowFakeCloud:
    """确定性慢速假 cloud（无网络）：每批 sleep 拉长 kill 窗口，首批写 sentinel。"""

    name = "cloud"
    dim = 3072

    def __init__(self, sentinel: Path):
        self._sentinel = sentinel
        self.calls = 0
        self.tokens_used = 0

    def model_ready(self):
        return True

    async def check_reachable(self):
        return self.dim

    async def embed(self, texts):
        self.calls += 1
        await asyncio.sleep(0.15)
        if self.calls == 1:
            # 首批已写完（worker 按批“先写后推进”，见 _run_rebuild）再立旗：
            # 父进程此时 kill，目标表必为“部分行、全维度正确”。
            self._sentinel.write_text("ready", encoding="utf-8")
        self.tokens_used += len(texts)
        return [_unit(0, self.dim) for _ in texts]


def main() -> None:
    db_path, sentinel_s, done_s = sys.argv[1:4]
    sentinel, done_marker = Path(sentinel_s), Path(done_s)
    conn = connect(db_path)
    run_migrations(conn)
    store = create_store(conn, "killstore")
    qa, e = [], {}
    for i in range(N):
        qid = f"rk-{i:04d}"
        qa.append(
            {
                "id": qid,
                "standard_question": f"重建回滚测试问题{i:04d}",
                "official_answer": f"重建回滚测试答案{i:04d}",
                "usage_status": "fixed",
            }
        )
        e[qid] = sqlite_vec.serialize_float32(_unit(i % N))
    compile_store(conn, store["id"], qa, [], embeddings=e)

    emb_router._conn = lambda: conn  # noqa: E731 — victim 单进程，直指自建库
    emb_router._build_provider = lambda name: SlowFakeCloud(sentinel)  # noqa: E731
    emb_router._JOBS["victim"] = {"status": "queued", "from": "local", "to": "cloud",
                                  "total": None, "done": 0, "tokens": 0, "error": None,
                                  "completed_at": None, "task": None}
    asyncio.run(emb_router._run_rebuild("victim"))
    # 只有翻转成功才会走到这里（kill 命中则永不到达）。
    done_marker.write_text("flipped", encoding="utf-8")
    conn.close()
    print(f"VICTIM_DONE n={N}")


if __name__ == "__main__":
    main()
