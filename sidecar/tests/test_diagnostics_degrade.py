"""三链降级统一计数单测（P8，R14 内容无关）。

覆盖：计数器形状/未知链拒绝；ASR 适配器接线；LLM 链失败计数；
embedding 重建失败与问答时向量故障计数；`/diagnostics/degrade` 端点形状。
全局态（计数器 + LLM 断路器）每测隔离。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import routers.embedding as emb_router
from diagnostics.degrade import (
    asr_on_degrade,
    llm_on_degrade,
    record,
    reset,
    snapshot,
)

TEST_TOKEN = "test-token-" + "x" * 32
EVAL_DIR = Path(__file__).resolve().parent / "eval"


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _clean_state():
    from generation.fallback import reset_circuits

    reset()
    reset_circuits()
    yield
    reset()
    reset_circuits()


def test_record_snapshot_reset_shapes():
    assert snapshot() == {
        "asr": {"total": 0, "by_kind": {}, "by_provider": {}},
        "llm": {"total": 0, "by_kind": {}, "by_provider": {}},
        "embedding": {"total": 0, "by_kind": {}, "by_provider": {}},
    }
    record("llm", "groq", "rate_limited")
    record("llm", "groq", "rate_limited")
    record("asr", "faster-whisper", "provider_timeout")
    snap = snapshot()
    assert snap["llm"] == {"total": 2, "by_kind": {"rate_limited": 2},
                           "by_provider": {"groq": 2}}
    assert snap["asr"]["total"] == 1
    assert snap["embedding"]["total"] == 0
    # 快照是深拷贝：改它动不了内部。
    snap["llm"]["total"] = 999
    assert snapshot()["llm"]["total"] == 2
    reset()
    assert snapshot()["llm"]["total"] == 0


def test_record_rejects_unknown_chain():
    with pytest.raises(ValueError):
        record("nope", "x", "y")


def test_asr_singleton_wires_adapter():
    """进程单例切换板的 on_degrade 即统一计数适配器（describe 形状不动）。

    只读 `_on_degrade` 身份不断言构造副作用；读完即恢复未构造态，
    不污染其它测试的运行时单例。
    """
    import asr.runtime as runtime_mod

    try:
        sb = runtime_mod.get_switchboard()
        assert sb._on_degrade is asr_on_degrade
        assert set(sb.describe()) >= {"selected", "ready", "available"}
    finally:
        runtime_mod.set_switchboard(None)


def test_asr_chain_failure_records():
    """ASR 链失败经适配器进计数（stub provider，无权重无网络）。"""
    from asr.fallback import FallbackASRProvider
    from asr.provider import ASRError

    class Boom:
        name = "stub-asr"

        async def transcribe(self, pcm_f32, sample_rate=16000, path=None):
            raise ASRError("provider_error", "boom")

    async def _go():
        chain = FallbackASRProvider([Boom()], on_degrade=asr_on_degrade,
                                    manual_input=False)
        with pytest.raises(ASRError):
            await chain.transcribe(b"\x00" * 480, 16000, "mic")

    asyncio.run(_go())
    assert snapshot()["asr"] == {
        "total": 1, "by_kind": {"provider_error": 1},
        "by_provider": {"stub-asr": 1}}


def test_llm_chain_failure_records():
    """LLM 链失败经适配器进计数（与 ASR 同形：名 + kind，无 prompt）。"""
    from generation.fallback import FallbackLLMProvider
    from generation.provider import ProviderError

    class BoomLLM:
        name = "groq"

        def generate(self, system, question, contexts):
            async def _gen():
                raise ProviderError("rate_limited", "x 限流")
                yield None

            return _gen()

    async def _go():
        chain = FallbackLLMProvider([BoomLLM()], on_degrade=llm_on_degrade)
        with pytest.raises(ProviderError):
            async for _ in chain.generate("s", "QUESTION-XYZ", ["CTX"]):
                pass

    asyncio.run(_go())
    snap = snapshot()["llm"]
    assert snap == {"total": 1, "by_kind": {"rate_limited": 1},
                    "by_provider": {"groq": 1}}


def _seed_demo(conn):
    from knowledge.compiler import compile_store
    from knowledge.field_extractor import extract, load_vocab
    from knowledge.parsers.json_parser import parse_json
    from knowledge.stores import create_store
    from knowledge.validator import validate_field_items, validate_qa_items
    import json as _json

    store = create_store(conn, "诊断库")
    parsed = parse_json(_json.loads((EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8")))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    compile_store(conn, store["id"], qa, fields, vocab_miss=miss)
    return store["id"]


class _FakeLLM:
    def generate(self, system, question, contexts):
        async def _gen():
            yield "好"

        return _gen()


class _FakeEmbed:
    def embed(self, texts):
        return [[0.0] * 512 for _ in texts]


def _maybe_stubs(monkeypatch):
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *x, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *x, **k: [_hit("a", "标准答案甲。", 0.9, "jieba"),
                         _hit("a", "标准答案甲。", 0.9, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *x, **k: [dict(_hit("a", "标准答案甲。", 0.9, "vec"))],
    )


def _sse_done(client, task_id):
    import json as _json

    r = client.get("/qa/stream", params={"task_id": task_id}, headers=_h())
    assert r.status_code == 200
    for line in r.text.splitlines():
        if line.startswith("data:"):
            event = _json.loads(line[5:].strip())
            if event["type"] == "done":
                return event["result"]
    raise AssertionError("SSE 流无 done 事件")


def test_embedding_rebuild_failure_records(api_client, monkeypatch):
    """重建失败进 embedding 计数（mock 全程，无网络）。"""
    from retrieval.embedding import EmbeddingError

    client, conn = api_client
    monkeypatch.setattr(emb_router, "_conn", lambda: conn)

    _seed_demo(conn)

    class AlwaysBoom:
        name = "cloud"
        dim = 3072
        tokens_used = 0

        def model_ready(self):
            return True

        async def check_reachable(self):
            return self.dim

        async def embed(self, texts):
            raise EmbeddingError("http", "fake HTTP 500")

    monkeypatch.setattr(emb_router, "_build_provider", lambda name: AlwaysBoom())
    r = client.post("/embedding/provider", json={"provider": "cloud"}, headers=_h())
    assert r.status_code == 200
    jid = r.json()["rebuilding"]["rebuild_id"]
    import time as _time

    deadline = _time.time() + 10.0
    while _time.time() < deadline:
        st = client.get(f"/embedding/rebuild/{jid}", headers=_h()).json()["status"]
        if st in ("done", "error"):
            break
        _time.sleep(0.05)
    assert st == "error"
    d = client.get("/diagnostics/degrade", headers=_h())
    assert d.status_code == 200
    emb = d.json()["chains"]["embedding"]
    assert emb["total"] == 1
    assert emb["by_kind"] == {"http": 1}
    assert emb["by_provider"] == {"cloud": 1}


def test_embedding_query_failure_records(api_client, monkeypatch):
    """问答时向量故障进 embedding 计数（链不断：done 仍是 llm）。

    embed 挂掉则 vec=[]（且库里无向量 → 该路权重从分母剔除），故用 fts 两路
    按降级后的分母反解到 maybe 档，走完 LLM 全路径。
    """
    import generation.router as router_mod
    import routers.qa as qa_mod

    client, conn = api_client
    monkeypatch.setattr(qa_mod, "_conn", lambda: conn)
    monkeypatch.setattr(qa_mod, "_get_llm", lambda name, fallbacks=(): _FakeLLM())

    class _BoomEmbed:
        def embed(self, texts):
            raise RuntimeError("本地 embedding 模型缺失")

    monkeypatch.setattr(qa_mod, "_get_embedder", lambda: _BoomEmbed())
    # 不能用字段桩顶分：P6 后精确字段命中（s=1.0）直接判 direct，走不到 LLM。
    # vec 挂掉后分母只剩 Σw - w_vec，故按**降级后的分母**反解 maybe 档中点。
    from tests.conftest import band_s
    from retrieval import hybrid_rank as hr

    _s = band_s((hr.TH_MAYBE + hr.TH_DIRECT) / 2.0, routes=("jieba", "simple"),
                denom=hr.W_SUM - hr.W_VEC)

    def _hit(route):
        return {"qa_id": "a", "standard_question": "Q", "official_answer": "A",
                "category": "E", "route": route, "s": _s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *x, **k: [])
    monkeypatch.setattr(router_mod, "fts5_search",
                        lambda *x, **k: [_hit("jieba"), _hit("simple")])
    monkeypatch.setattr(router_mod, "vector_search", lambda *x, **k: [])
    sid = _seed_demo(conn)
    task_id = client.post("/qa/ask", json={"question": "测试问题", "store_id": sid},
                          headers=_h()).json()["task_id"]
    done = _sse_done(client, task_id)
    assert done["type"] == "llm"  # vec 坏不断链（warnings 路 + LLM 照常）
    assert done["text"] == "好"
    d = client.get("/diagnostics/degrade", headers=_h()).json()["chains"]["embedding"]
    assert d["total"] == 1
    assert d["by_kind"] == {"RuntimeError": 1}


def test_degrade_endpoint_shape(api_client):
    client, _ = api_client
    r = client.get("/diagnostics/degrade", headers=_h())
    assert r.status_code == 200
    chains = r.json()["chains"]
    assert set(chains) == {"asr", "llm", "embedding"}
    for name, snap in chains.items():
        assert set(snap) == {"total", "by_kind", "by_provider"}, name
        assert isinstance(snap["total"], int)
        assert all(isinstance(v, int) for v in snap["by_kind"].values())
        assert all(isinstance(v, int) for v in snap["by_provider"].values())
