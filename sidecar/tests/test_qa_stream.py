"""qa SSE 单测：事件顺序/无缓冲头/404/TTL/断开取消。"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import routers.qa as qa_mod
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items

EVAL_SEED = Path(__file__).resolve().parent / "eval" / "seed_demo.json"
TEST_TOKEN = "test-token-" + "x" * 32


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


class FakeLLM:
    def __init__(self, chunks=("甲", "乙")):
        self.calls = []
        self._chunks = chunks

    def generate(self, system, question, contexts):
        self.calls.append((system, question, list(contexts)))

        async def _gen():
            for ch in self._chunks:
                yield ch

        return _gen()

    async def embed(self, texts):
        return [[0.0] * 512 for _ in texts]


class FakeEmbed:
    def embed(self, texts):
        return [[0.0] * 512 for _ in texts]


@pytest.fixture()
def qa_client(api_client, monkeypatch):
    client, conn = api_client
    monkeypatch.setattr(qa_mod, "_conn", lambda: conn)
    monkeypatch.setattr(qa_mod, "_get_llm", lambda name: FakeLLM())
    monkeypatch.setattr(qa_mod, "_get_embedder", lambda: FakeEmbed())
    store = create_store(conn, "问答库")
    seed = json.loads(EVAL_SEED.read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    compile_store(conn, store["id"], qa, fields, vocab_miss=miss)
    return client, conn, store["id"]


def _sse_events(resp):
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def test_ask_validation(qa_client):
    client, _, sid = qa_client
    assert client.post("/qa/ask", json={"question": "   "}, headers=_h()).status_code == 400
    assert client.post("/qa/ask", json={"question": "q", "store_id": "nope"},
                       headers=_h()).status_code == 404


def test_direct_sse_order_and_headers(qa_client):
    client, _, sid = qa_client
    r = client.post("/qa/ask", json={"question": "公司成立时间", "store_id": sid},
                    headers=_h())
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    r = client.get("/qa/stream", params={"task_id": task_id}, headers=_h())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"

    events = _sse_events(r)
    assert [e["type"] for e in events] == ["retrieval", "done"]
    assert events[0]["action"] == "direct"
    assert events[1]["result"]["text"] == "公司成立于2015年，总部位于北京。"
    assert events[1]["result"]["llm_calls"] == 0

    # 读毕即清理：复读 404。
    assert client.get("/qa/stream", params={"task_id": task_id},
                      headers=_h()).status_code == 404


def test_chunk_passthrough_order(qa_client, monkeypatch):
    async def fake_stream(conn, store_id, question, llm, embed_fn):
        yield {"type": "decision", "action": "maybe_single", "top1_score": 0.5}
        yield {"type": "sources", "sources": [
            {"type": "qa", "key": "a", "score": 0.5, "payload": {}}]}
        yield {"type": "chunk", "text": "甲"}
        yield {"type": "chunk", "text": "乙"}
        yield {"type": "done", "result": {"type": "llm", "text": "甲乙",
                                          "sources": [], "llm_calls": 1}}

    def fake_answer_stream(conn, store_id, question, llm, embed_fn):
        return fake_stream(conn, store_id, question, llm, embed_fn)

    monkeypatch.setattr(qa_mod, "answer_stream", fake_answer_stream)
    client, _, sid = qa_client
    task_id = client.post("/qa/ask", json={"question": "q", "store_id": sid},
                          headers=_h()).json()["task_id"]
    r = client.get("/qa/stream", params={"task_id": task_id}, headers=_h())
    events = _sse_events(r)
    assert [e["type"] for e in events] == ["retrieval", "generation", "generation", "done"]
    assert events[0]["action"] == "maybe_single"
    assert [e["chunk"] for e in events if e["type"] == "generation"] == ["甲", "乙"]
    assert events[-1]["result"]["text"] == "甲乙"


def test_unknown_task_404(qa_client):
    client, _, _ = qa_client
    assert client.get("/qa/stream", params={"task_id": "nope"},
                      headers=_h()).status_code == 404


def test_ttl_expiry_404(qa_client, monkeypatch):
    monkeypatch.setattr(qa_mod, "TASK_TTL_S", 0.2)
    client, _, sid = qa_client
    task_id = client.post("/qa/ask", json={"question": "公司成立时间", "store_id": sid},
                          headers=_h()).json()["task_id"]
    time.sleep(0.35)
    assert client.get("/qa/stream", params={"task_id": task_id},
                      headers=_h()).status_code == 404


def test_disconnect_cancels_background(db, monkeypatch):
    """断开分支确定性验证（单循环内直驱 _event_gen + 可控 FakeRequest）。

    说明：starlette TestClient 不向服务端投递断开（is_disconnected 恒 False，
    实测 60s 兜底才退出）；真实 uvicorn 下 is_disconnected 为文档化机制。
    本用例验证“观测到断开 → 取消后台 + 清理”的分支逻辑本身。
    """

    gate = asyncio.Event()
    cancelled = asyncio.Event()

    def gated_stream(conn, store_id, question, llm, embed_fn):
        async def _gen():
            yield {"type": "decision", "action": "maybe_single", "top1_score": 0.5}
            yield {"type": "sources", "sources": []}
            try:
                await gate.wait()
                yield {"type": "done", "result": {"type": "llm", "text": "x",
                                                  "sources": [], "llm_calls": 1}}
            except asyncio.CancelledError:
                cancelled.set()
                raise

        return _gen()

    class FakeRequest:
        def __init__(self):
            self._disconnected = False

        async def is_disconnected(self):
            return self._disconnected

    class FakeEmbed:
        def embed(self, texts):
            return [[0.0] * 512 for _ in texts]

    monkeypatch.setattr(qa_mod, "answer_stream", gated_stream)
    monkeypatch.setattr(qa_mod, "_conn", lambda: db)
    monkeypatch.setattr(qa_mod, "_get_embedder", lambda: FakeEmbed())
    monkeypatch.setattr(qa_mod, "_get_llm", lambda name: object())

    async def _scenario():
        task = qa_mod._Task("q", "s", None)
        async with qa_mod.TASKS_LOCK:
            qa_mod.TASKS["tid-x"] = task
        task.bg = asyncio.create_task(qa_mod._run_task("tid-x"))
        req = FakeRequest()
        gen = qa_mod._event_gen("tid-x", req)
        try:
            first = await gen.__anext__()
            assert json.loads(first[5:].strip())["type"] == "retrieval"
            req._disconnected = True  # 模拟客户端断开
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
            await asyncio.wait_for(cancelled.wait(), 10)
            assert "tid-x" not in qa_mod.TASKS, "断开后 task 记录残留"
        finally:
            await gen.aclose()

    asyncio.run(_scenario())
