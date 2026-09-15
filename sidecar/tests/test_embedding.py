"""双 embedding 基建单测（F5.3/F6.5）。

覆盖：provider 批量/退避/成本日志（R14/R18）/错误映射；目录与运行时态；
compiler/vector 接线与维度守卫；切换端点全流程（mock 下）；失败不翻转；
重建期间检索不断链；SIGKILL 中断旧表一致（task-6 模式，见 rebuild_victim.py）。

运行时全局态（active provider / secrets / _JOBS）由 autouse fixture 每测隔离。
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import sqlite_vec

import routers.embedding as emb_router
from knowledge.compiler import compile_store
from knowledge.stores import create_store
from retrieval import embedding as emb
from retrieval.embedding import EmbeddingError

TESTS_DIR = Path(__file__).resolve().parent
TEST_TOKEN = "test-token-" + "x" * 32


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _unit(i, dim=512):
    v = [0.0] * dim
    v[i % dim] = 1.0
    return v


def _blob(i, dim=512):
    return sqlite_vec.serialize_float32(_unit(i, dim))


@pytest.fixture(autouse=True)
def _clean_state():
    """全局态隔离：active 回 local、清 secrets、取消并清重建任务。"""
    from core.secrets import clear_llm_secret

    emb.set_active("local")
    clear_llm_secret("openai")
    emb_router._JOBS.clear()
    yield
    for job in list(emb_router._JOBS.values()):
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
    emb_router._JOBS.clear()
    emb.set_active("local")
    clear_llm_secret("openai")


class FakeEmbeddingProvider:
    """可编排的假 provider（无网络、无权重）：instant / 慢速 / 中途抛错。"""

    def __init__(self, name="cloud", dim=3072, fail_after=None, sleep_per_batch=0):
        self.name = name
        self.dim = dim
        self.calls = []
        self.fail_after = fail_after
        self.sleep_per_batch = sleep_per_batch
        self.tokens_used = 0
        self._batches_done = 0

    def model_ready(self):
        return True

    async def check_reachable(self):
        return self.dim

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.sleep_per_batch:
            await asyncio.sleep(self.sleep_per_batch)
        if self.fail_after is not None and self._batches_done >= self.fail_after:
            raise EmbeddingError("http", "fake boom HTTP 500")
        self._batches_done += 1
        self.tokens_used += len(texts) * 7
        return [_unit(0, self.dim) for _ in texts]


def _seed_qa(conn, n, prefix="q"):
    store = create_store(conn, "emb库")
    qa = [
        {"id": f"{prefix}-{i:04d}", "standard_question": f"问题{prefix}{i:04d}内容",
         "official_answer": f"答{i}", "usage_status": "fixed"}
        for i in range(n)
    ]
    e = {q["id"]: _blob(i) for i, q in enumerate(qa)}
    compile_store(conn, store["id"], qa, [], embeddings=e)
    return store["id"]


def _mock_openai_client(handler, sleeps=None):
    sleep_calls = [] if sleeps is None else sleeps

    async def _sleep(s):
        sleep_calls.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, _sleep


def _ok_handler(vectors_dim=3072, per_text_tokens=7, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["input"]
        if seen is not None:
            seen.append(len(inputs))
        data = [{"embedding": _unit(0, vectors_dim)} for _ in inputs]
        return httpx.Response(200, json={
            "data": data,
            "usage": {"prompt_tokens": len(inputs) * per_text_tokens,
                      "total_tokens": len(inputs) * per_text_tokens},
        })

    return handler


# ---------------- provider 单元 ----------------

def test_catalog_shapes_content_free():
    entries = emb.catalog_entries()
    assert [e["name"] for e in entries] == ["local", "cloud"]
    by_name = {e["name"]: e for e in entries}
    assert (by_name["local"]["dim"], by_name["local"]["table"]) == (512, "vec_qa_local")
    assert (by_name["cloud"]["dim"], by_name["cloud"]["table"]) == (3072, "vec_qa_cloud")
    assert "sk-" not in str(entries)  # 内容无关，无 key


def test_build_unknown_raises():
    with pytest.raises(ValueError):
        emb.build_embedding_provider("nope")


def test_cloud_missing_key_raises_unavailable():
    with pytest.raises(EmbeddingError) as exc:
        emb.build_embedding_provider("cloud")
    assert exc.value.kind == "unavailable"


def test_cloud_uses_pushed_openai_slot():
    from core.secrets import clear_llm_secret, set_llm_secret

    try:
        set_llm_secret("openai", "sk-test-123")
        p = emb.build_embedding_provider("cloud")
        assert p.api_key == "sk-test-123"
        assert "sk-test-123" not in str(emb.catalog_entries())
    finally:
        clear_llm_secret("openai")


def test_openai_batch_splitting_and_token_accounting():
    seen = []
    client, _sleep = _mock_openai_client(_ok_handler(seen=seen))
    p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
    texts = [f"t{i}" for i in range(130)]

    async def _go():
        return await p.embed(texts)

    out = asyncio.run(_go())
    assert seen == [64, 64, 2]  # BATCH_SIZE 切批
    assert len(out) == 130 and all(len(v) == 3072 for v in out)
    assert p.tokens_used == 130 * 7


def test_openai_429_backoff_then_success():
    calls = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(429, json={"error": "slow"})
        return httpx.Response(200, json={
            "data": [{"embedding": _unit(0, 3072)}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3}})

    client, _sleep = _mock_openai_client(handler, sleeps)
    p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
    out = asyncio.run(p.embed(["a"]))
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # 指数退避，确定性（无抖动）
    assert len(out) == 1


@pytest.mark.parametrize("status, kind, calls", [
    (401, "auth", 1),
    (403, "auth", 1),
    (400, "http", 1),
    (429, "rate_limited", 6),
    (500, "http", 6),
])
def test_openai_status_mapping(status, kind, calls):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(status, json={"error": "x"})

    client, _sleep = _mock_openai_client(handler, [])
    p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(p.embed(["a"]))
    assert exc.value.kind == kind
    assert len(seen) == calls


def test_openai_transport_timeout_retries_then_maps():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        raise httpx.TimeoutException("t")

    client, _sleep = _mock_openai_client(handler, [])
    p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(p.embed(["a"]))
    assert exc.value.kind == "timeout"
    assert len(seen) == 6


def test_openai_protocol_errors():
    async def _expect(body, kind="protocol"):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        client, _sleep = _mock_openai_client(handler, [])
        p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
        with pytest.raises(EmbeddingError) as exc:
            await p.embed(["a"])
        assert exc.value.kind == kind

    async def _go():
        await _expect(b"not-json")
        await _expect(b'{"data": []}')  # 条目数不符
        await _expect(json.dumps(
            {"data": [{"embedding": [0.1, 0.2]}]}).encode())  # 维度不符

    asyncio.run(_go())


def test_openai_empty_input():
    client, _sleep = _mock_openai_client(_ok_handler(), [])
    p = emb.OpenAIEmbeddingProvider(api_key="k", client=client, sleep=_sleep)
    assert asyncio.run(p.embed([])) == []


def test_cost_log_counts_only(caplog):
    import logging

    secret_text = "发货周期秘密文本ZXQ"
    client, _sleep = _mock_openai_client(_ok_handler())
    p = emb.OpenAIEmbeddingProvider(api_key="sk-test-SECRETKEY", client=client,
                                    sleep=_sleep)
    with caplog.at_level(logging.INFO, logger="retrieval.embedding"):
        asyncio.run(p.embed([secret_text]))
    assert "prompt_tokens" in caplog.text
    assert secret_text not in caplog.text  # R14/R18：文本永不进日志
    assert "SECRETKEY" not in caplog.text  # R8：key 永不进日志


def test_local_missing_model_raises_unavailable(tmp_path):
    p = emb.LocalEmbeddingProvider(model_dir=tmp_path / "none")
    assert p.model_ready() is False
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(p.embed(["a"]))
    assert exc.value.kind == "unavailable"


def test_check_blob_dim_guard():
    assert emb.check_blob_dim("vec_qa_local", _blob(0, 512)) == 512
    assert emb.check_blob_dim("vec_qa_cloud", _blob(0, 3072)) == 3072
    with pytest.raises(ValueError):  # 512 写不进 cloud 表
        emb.check_blob_dim("vec_qa_cloud", _blob(0, 512))
    with pytest.raises(ValueError):  # 反之亦然
        emb.check_blob_dim("vec_qa_local", _blob(0, 3072))
    with pytest.raises(ValueError):  # 未知表（SQL 拼接白名单）
        emb.check_blob_dim("vec_qa_evil", _blob(0, 512))


def test_runtime_defaults_and_flip():
    assert emb.get_active() == "local"
    assert emb.active_table() == "vec_qa_local"
    assert emb.set_active("cloud") == "cloud"
    assert emb.active_table() == "vec_qa_cloud"
    with pytest.raises(ValueError):
        emb.set_active("nope")
    assert emb.get_active() == "cloud"  # 失败切换不改动原选择
    d = emb.describe()
    assert (d["active"], d["dim"], d["table"]) == ("cloud", 3072, "vec_qa_cloud")


# ---------------- compiler / vector 接线 ----------------

def test_compiler_writes_active_table(db):
    emb.set_active("cloud")
    store = create_store(db, "云库")
    qa = [{"id": "c1", "standard_question": "云问题一",
           "official_answer": "答", "usage_status": "fixed"}]
    e = {"c1": _blob(0, 3072)}
    stats = compile_store(db, store["id"], qa, [], embeddings=e)
    assert stats["vec_written"] == 1
    assert db.execute("SELECT COUNT(*) FROM vec_qa_cloud").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 0


def test_compiler_dim_guard_rejects_mismatch(db):
    emb.set_active("cloud")
    store = create_store(db, "错维库")
    qa = [{"id": "m1", "standard_question": "错维问题",
           "official_answer": "答", "usage_status": "fixed"}]
    with pytest.raises(ValueError):
        compile_store(db, store["id"], qa, [], embeddings={"m1": _blob(0, 512)})
    assert db.execute("SELECT COUNT(*) FROM vec_qa_cloud").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 0


def test_compiler_explicit_table_override(db):
    store = create_store(db, "显式库")
    qa = [{"id": "e1", "standard_question": "显式问题",
           "official_answer": "答", "usage_status": "fixed"}]
    compile_store(db, store["id"], qa, [], embeddings={"e1": _blob(0, 512)},
                  vec_table="vec_qa_local")
    assert db.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 1
    with pytest.raises(ValueError):
        compile_store(db, store["id"], qa, [], embeddings={"e1": _blob(0, 512)},
                      vec_table="vec_qa_evil")


def test_vector_search_table_param(db):
    from retrieval.vector_search import vector_search

    sid = _seed_qa(db, 2)
    # 模拟切后态：同批 qa_id 再写一份 3072 行进 cloud 表
    rows = db.execute("SELECT id FROM qa_pairs WHERE store_id=?", (sid,)).fetchall()
    from database.connection import write_lock
    with write_lock, db:
        for (qid,) in rows:
            db.execute("INSERT OR REPLACE INTO vec_qa_cloud(qa_id, embedding)"
                       " VALUES (?, ?)", (qid, _blob(0, 3072)))
    hits_local = vector_search(db, sid, _blob(0, 512), table="vec_qa_local")
    assert hits_local and hits_local[0]["route"] == "vec"
    hits_cloud = vector_search(db, sid, _blob(0, 3072), table="vec_qa_cloud")
    assert hits_cloud and hits_cloud[0]["route"] == "vec"
    with pytest.raises(ValueError):
        vector_search(db, sid, _blob(0, 512), table="vec_qa_evil")


# ---------------- 切换端点 ----------------

def _patch_router(monkeypatch, conn, fakes):
    monkeypatch.setattr(emb_router, "_conn", lambda: conn)
    monkeypatch.setattr(emb_router, "_build_provider", lambda name: fakes[name])


def _poll_done(client, jid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/embedding/rebuild/{jid}", headers=_h())
        assert r.status_code == 200
        if r.json()["status"] in ("done", "error"):
            return r.json()
        time.sleep(0.05)
    raise TimeoutError(f"重建任务 {jid} 未在 {timeout}s 内终结")


def test_provider_snapshot_shape(api_client, monkeypatch):
    client, _ = api_client
    _patch_router(monkeypatch, None, {})
    r = client.get("/embedding/provider", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["active"] == "local"
    assert body["dim"] == 512 and body["table"] == "vec_qa_local"
    assert body["rebuilding"] is None
    assert [e["name"] for e in body["available"]] == ["local", "cloud"]
    assert "sk-" not in r.text


def test_switch_unknown_400(api_client, monkeypatch):
    client, conn = api_client
    _patch_router(monkeypatch, conn, {})
    r = client.post("/embedding/provider", json={"provider": "nope"}, headers=_h())
    assert r.status_code == 400


def test_switch_same_noop(api_client, monkeypatch):
    client, conn = api_client
    fake = FakeEmbeddingProvider()
    _patch_router(monkeypatch, conn, {"local": fake, "cloud": fake})
    r = client.post("/embedding/provider", json={"provider": "local"}, headers=_h())
    assert r.status_code == 200
    assert r.json() == {"active": "local", "rebuilding": None}
    assert emb_router._JOBS == {}  # 空操作不建任务


def test_switch_unreachable_409_keeps_old(api_client, monkeypatch):
    client, conn = api_client
    # 真构造路径（不 patch _build_provider）：缺 key 即 unavailable，不联网、不建任务
    monkeypatch.setattr(emb_router, "_conn", lambda: conn)
    r = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "unavailable"
    assert emb.get_active() == "local"
    assert emb_router._JOBS == {}


def test_switch_full_flow_background(api_client, monkeypatch):
    client, conn = api_client
    sid = _seed_qa(conn, 5)
    fake = FakeEmbeddingProvider(name="cloud", dim=3072)
    _patch_router(monkeypatch, conn, {"cloud": fake})
    r = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    assert r.status_code == 200
    snap = r.json()["rebuilding"]
    assert snap["status"] == "queued" and snap["to"] == "cloud"
    # 请求已返回（后台跑，非请求生命周期内同步执行）
    assert emb.get_active() == "local"
    final = _poll_done(client, snap["rebuild_id"])
    assert final["status"] == "done"
    assert final["done"] == 5 and final["total"] == 5
    assert emb.get_active() == "cloud"  # 原子翻转
    assert conn.execute("SELECT COUNT(*) FROM vec_qa_cloud").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 5
    for (blob,) in conn.execute("SELECT embedding FROM vec_qa_cloud").fetchall():
        assert len(blob) == 3072 * 4  # 全维度正确
    assert final["tokens"] == 5 * 7
    # provider 快照同步翻转，重建态清空
    cur = client.get("/embedding/provider", headers=_h()).json()
    assert cur["active"] == "cloud" and cur["rebuilding"] is None


def test_switch_failure_never_flips(api_client, monkeypatch):
    client, conn = api_client
    sid = _seed_qa(conn, 130)  # 3 批（64/64/2），第 3 批前抛错
    fake = FakeEmbeddingProvider(name="cloud", dim=3072, fail_after=2)
    _patch_router(monkeypatch, conn, {"cloud": fake})
    r = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    jid = r.json()["rebuilding"]["rebuild_id"]
    final = _poll_done(client, jid)
    assert final["status"] == "error"
    assert "EmbeddingError" in final["error"]
    assert emb.get_active() == "local"  # 永不翻转
    assert conn.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 130
    # 旧表内容逐行一致（前两批只动了 cloud 表）
    for i, (blob,) in enumerate(
            conn.execute("SELECT embedding FROM vec_qa_local v JOIN qa_pairs q"
                         " ON q.id=v.qa_id WHERE q.store_id=? ORDER BY q.id", (sid,))):
        assert blob == _blob(i % 130)


def test_concurrent_switch_409(api_client, monkeypatch):
    client, conn = api_client
    _seed_qa(conn, 5)
    # 慢速假 provider：首个重建必在在途窗口内，第二个 POST  deterministic 撞 409
    #（首个 POST 建 record 时已同步置 queued，不依赖 worker 调度时序）。
    fake = FakeEmbeddingProvider(name="cloud", dim=3072, sleep_per_batch=0.5)
    _patch_router(monkeypatch, conn, {"cloud": fake})
    first = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    assert first.status_code == 200
    second = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "rebuild_in_progress"
    _poll_done(client, first.json()["rebuilding"]["rebuild_id"])
    assert emb.get_active() == "cloud"


def test_rebuild_unknown_404(api_client, monkeypatch):
    client, conn = api_client
    _patch_router(monkeypatch, conn, {})
    assert client.get("/embedding/rebuild/nope", headers=_h()).status_code == 404


def test_rebuild_continuity_serves_old_table():
    """重建期间检索不断链：同一循环内起后台重建，并发查旧表仍命中。"""
    import routers.embedding as R
    from database.connection import connect
    from database.schema import run_migrations
    from generation.router import answer_stream
    from retrieval.vector_search import vector_search
    import tempfile

    tmp = tempfile.mkdtemp(prefix="emb-cont-")
    conn = connect(str(Path(tmp) / "c.db"))
    run_migrations(conn)
    try:
        sid = _seed_qa(conn, 1)
        gate = asyncio.Event()
        started = asyncio.Event()

        class GateFake(FakeEmbeddingProvider):
            async def embed(self, texts):
                if not started.is_set():
                    started.set()
                    await gate.wait()  # 首批卡住：模拟长重建窗口
                return await super().embed(texts)

        fake = GateFake(name="cloud", dim=3072)
        old_build, old_conn = R._build_provider, R._conn
        R._build_provider = lambda name: fake  # noqa: E731
        R._conn = lambda: conn
        try:
            async def _scenario():
                jid = "cont-job"
                R._JOBS[jid] = {"status": "queued", "from": "local", "to": "cloud",
                                "total": None, "done": 0, "tokens": 0, "error": None,
                                "completed_at": None, "task": None}
                task = asyncio.create_task(R._run_rebuild(jid))
                R._JOBS[jid]["task"] = task
                await asyncio.wait_for(started.wait(), 10)
                # 重建卡住期间：旧表可查、answer_stream 无 warnings
                hits = vector_search(conn, sid, _blob(0, 512))
                assert hits and hits[0]["qa_id"] == "q-0000"

                class FakeLLM:
                    def generate(self, system, question, contexts):
                        async def _gen():
                            yield "x"
                        return _gen()

                events = [e async for e in answer_stream(
                    conn, sid, "问题q-0000内容", FakeLLM(), lambda t: [_unit(0)])]
                assert events[0]["warnings"] == []
                assert emb.get_active() == "local"  # 完成前不翻转
                gate.set()
                await asyncio.wait_for(task, 10)
                assert emb.get_active() == "cloud"
                assert R._JOBS[jid]["status"] == "done"

            asyncio.run(_scenario())
        finally:
            R._build_provider, R._conn = old_build, old_conn
    finally:
        conn.close()


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"sentinel 未出现：{path}")


def test_sigkill_mid_rebuild_keeps_old_table(tmp_path):
    """SIGKILL 中断回滚（task-6 模式）：kill 后旧表逐行一致、目标表全维度正确。

    victim 跑真实 `_run_rebuild`（慢速假 cloud provider，窗口可命中）；
    父进程在首批完成后 kill。active 翻转是内存态（跨进程不可观察），
    故“永不翻转”由 test_switch_failure_never_flips 在进程内确定性覆盖；
    本用例锁定“旧表一致 + 无撕裂行”。
    """
    from database.connection import connect

    db_path = tmp_path / "kill.db"
    sentinel = tmp_path / "ready"
    done_marker = tmp_path / "flipped"
    err_log = tmp_path / "victim.stderr.log"
    with open(err_log, "w", encoding="utf-8") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, str(TESTS_DIR / "rebuild_victim.py"),
             str(db_path), str(sentinel), str(done_marker)],
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
        )
        try:
            _wait_for(sentinel)
            time.sleep(0.4)
            proc.kill()  # TerminateProcess：不可捕获（等价 SIGKILL）
            ret = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                ret = proc.wait(timeout=60)

    err_text = err_log.read_text(encoding="utf-8", errors="replace")
    assert ret != 0, "victim 竟正常退出——kill 未命中，测试无效"
    assert "Traceback" not in err_text, f"victim 自行崩溃而非被 kill：{err_text[-500:]}"
    assert not done_marker.exists(), "翻转标记出现——kill 落在翻转之后，测试无效"

    conn = None
    deadline = time.time() + 15.0
    last_error = None
    while time.time() < deadline:
        try:
            conn = connect(str(db_path))
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            time.sleep(0.1)
    assert conn is not None, f"kill 后 DB 始终打不开：{last_error}"
    try:
        local_rows = conn.execute(
            "SELECT q.id, v.embedding FROM vec_qa_local v JOIN qa_pairs q"
            " ON q.id=v.qa_id ORDER BY q.id").fetchall()
        assert len(local_rows) == 300
        for i, (qid, blob) in enumerate(local_rows):
            assert qid == f"rk-{i:04d}"
            assert blob == _blob(i % 300), f"旧表第 {i} 行被改写"
        cloud_rows = conn.execute("SELECT embedding FROM vec_qa_cloud").fetchall()
        assert len(cloud_rows) > 0, "kill 落在首批写之前，窗口未命中"
        for (blob,) in cloud_rows:
            assert len(blob) == 3072 * 4, "目标表出现撕裂/错维行"
        print(f"\n[rebuild-rollback] local=300 intact, cloud_partial={len(cloud_rows)} dim-ok")
    finally:
        conn.close()
