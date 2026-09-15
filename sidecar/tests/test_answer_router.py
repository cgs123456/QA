"""答案路由单测：direct 零 LLM 调用 / 流式 maybe / Fail-Closed / error 路径。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from generation.provider import ProviderError
from generation.router import (
    ERROR_TEXT,
    FAIL_CLOSED_TEXT,
    answer,
    answer_stream,
    select_contexts,
)
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items

EVAL_DIR = Path(__file__).resolve().parent / "eval"


class FakeProvider:
    """调用计数在 generate() 调用时（非迭代时）记录——direct/fail 路径
    若触碰 llm 即被计数器捕获。"""

    def __init__(self, chunks=("甲", "乙"), error=None):
        self.calls = []
        self._chunks = chunks
        self._error = error

    def generate(self, system, question, contexts):
        self.calls.append((system, question, list(contexts)))

        async def _gen():
            if self._error is not None:
                raise self._error
            for ch in self._chunks:
                yield ch

        return _gen()

    async def embed(self, texts):
        return [[0.0] * 512 for _ in texts]


def _zeros(texts):
    return [[0.0] * 512 for _ in texts]


@pytest.fixture()
def demo_db(db):
    store = create_store(db, "答案库")
    seed = (EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8")
    import json as _json

    parsed = parse_json(_json.loads(seed))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return db, store["id"]


@pytest.mark.anyio
async def test_direct_returns_answer_with_zero_llm_calls(demo_db):
    db, sid = demo_db
    fake = FakeProvider()
    result = await answer(db, sid, "退货期限", fake, _zeros)
    assert result["type"] == "direct"
    assert result["text"] == "七天内无理由退货，运费由买家承担首重。"
    assert result["llm_calls"] == 0
    assert fake.calls == []


@pytest.mark.anyio
async def test_fail_closed_text_and_zero_llm_calls(demo_db):
    db, sid = demo_db
    fake = FakeProvider()
    result = await answer(db, sid, "量子电动力学xyz", fake, _zeros)
    assert result["type"] == "fail_closed"
    assert result["text"] == FAIL_CLOSED_TEXT
    assert result["llm_calls"] == 0
    assert fake.calls == []


@pytest.mark.anyio
async def test_empty_question_fail_closed(demo_db):
    db, sid = demo_db
    fake = FakeProvider()
    result = await answer(db, sid, "   ", fake, _zeros)
    assert result["type"] == "fail_closed"
    assert fake.calls == []


@pytest.mark.anyio
async def test_maybe_single_streams_with_top1_context(monkeypatch, db):
    """`db` 必需：路由会先 `prepare(conn, question)` 取关键词（用扩展分词），
    连接不能是 None —— 三路虽然被打桩，但预处理是真的。"""
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    # a: jieba .9 + simple .9 + vec .9 = 2.7/6 = 0.45 → maybe_single（含边界）。
    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", 0.9, "jieba"),
                         _hit("a", "标准答案甲。", 0.9, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", 0.9, "vec"))],
    )
    fake = FakeProvider()
    events = [e async for e in router_mod.answer_stream(db, "s", "测试问题", fake, _zeros)]
    kinds = [e["type"] for e in events]
    assert kinds == ["decision", "sources", "chunk", "chunk", "done"]
    assert events[0]["action"] == "maybe_single"
    done = events[-1]["result"]
    assert done["type"] == "llm"
    assert done["text"] == "甲乙"
    assert done["llm_calls"] == 1
    assert len(done["sources"]) == 1
    assert len(fake.calls) == 1
    _system, _q, contexts = fake.calls[0]
    assert contexts == ["标准答案甲。"]


@pytest.mark.anyio
async def test_maybe_multi_context_top2(monkeypatch, db):
    import generation.router as router_mod

    def _hit(qid, text, s):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "s": s}

    # 打桩签名要带上 `prepared`：路由把同一份预处理结果透传给两路（关键词同源）。
    def fake_field(conn, store_id, query, limit=5, prepared=None):
        return []

    def fake_fts(conn, store_id, query, top_k=5, prepared=None):
        return [dict(_hit("a", "答案甲", 0.9), route="jieba"),
                dict(_hit("b", "答案乙", 0.89), route="jieba"),
                dict(_hit("a", "答案甲", 0.9), route="simple"),
                dict(_hit("b", "答案乙", 0.89), route="simple")]

    def fake_vec(conn, store_id, vector, top_k=5):
        return [dict(_hit("a", "答案甲", 0.9), route="vec"),
                dict(_hit("b", "答案乙", 0.89), route="vec")]

    monkeypatch.setattr(router_mod, "field_lookup", fake_field)
    monkeypatch.setattr(router_mod, "fts5_search", fake_fts)
    monkeypatch.setattr(router_mod, "vector_search", fake_vec)

    # fts 命中按 route 分发给 jieba/simple 两路；vec 同理 →
    # a:2.7/6=0.45，b:2.67/6=0.445，gap 0.005 → maybe_multi。
    fake = FakeProvider()
    result = await router_mod.answer(db, "s", "测试问题", fake, _zeros)
    assert result["type"] == "llm"
    assert len(result["sources"]) == 2
    assert len(fake.calls) == 1
    _system, _q, contexts = fake.calls[0]
    assert contexts == ["答案甲", "答案乙"]


@pytest.mark.anyio
async def test_llm_error_path(monkeypatch, db):
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", 0.9, "jieba"),
                         _hit("a", "标准答案甲。", 0.9, "simple")],
    )
    # 三路和 2.7/6=0.45 → maybe_single，LLM 必被调用一次后超时。
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", 0.9, "vec"))],
    )
    fake = FakeProvider(error=ProviderError("timeout", "x 超时（30.0s）"))
    result = await router_mod.answer(db, "s", "测试问题", fake, _zeros)
    assert result["type"] == "error"
    assert result["text"] == ERROR_TEXT
    assert result["error"] == "timeout"
    assert result["llm_calls"] == 1


@pytest.mark.anyio
async def test_vec_failure_degrades_not_breaks(demo_db):
    """embed 缺模型 → vec=[] + warnings，direct 照常（retrieval 恒先到达）。"""
    db, sid = demo_db
    fake = FakeProvider()

    def _boom(texts):
        raise RuntimeError("本地 embedding 模型缺失")

    events = [e async for e in answer_stream(db, sid, "退货期限", fake, _boom)]
    assert events[0]["type"] == "decision"
    assert events[0]["warnings"] == ["vec:RuntimeError"]
    done = events[-1]["result"]
    assert done["type"] == "direct"
    assert done["text"] == "七天内无理由退货，运费由买家承担首重。"
    assert fake.calls == []


def test_select_contexts_single_point():
    items = [
        {"type": "qa", "key": "q1", "score": 0.5,
         "s": {"field": 0.0, "jieba": 0.9, "simple": 0.9, "vec": 0.9},
         "payload": {"official_answer": "A1"}},
        {"type": "field", "key": "f1", "score": 0.5,
         "s": {"field": 1.0, "jieba": 0.0, "simple": 0.0, "vec": 0.0},
         "payload": {"field_value": "V2"}},
    ]
    contexts, sources = select_contexts({"items": items})
    assert contexts == ["A1", "V2"]
    assert [(s["type"], s["key"]) for s in sources] == [("qa", "q1"), ("field", "f1")]
    assert sources[0]["routes"] == ["jieba", "simple", "vec"]
    assert sources[1]["routes"] == ["field"]
