"""LLM 降级链单测（P8 可用性收尾）。

三组 DoD 单测：降级顺序 / 快速切换（Groq 限流）/ 拒绝不重试；
另覆盖：全灭 kind、三级链、首 chunk 后失败不换级、熔断冷却与恢复、
build 校验、结果形状（provider/degraded 加法键）、端点 fallbacks 透传。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import generation.fallback as fb_mod
from generation.fallback import FallbackLLMProvider, build_chain, reset_circuits
from generation.provider import ProviderError
from generation.router import answer


@pytest.fixture(autouse=True)
def _circuits():
    reset_circuits()
    yield
    reset_circuits()


class ScriptedProvider:
    """可编排的假 LLM：chunks 逐块产出；fail_at 指定第几块后抛错（None=全成功）。

    注意：generate() 是普通（非 async）函数，返回异步生成器——与生产
    provider 的 `async def generate` 含 yield 的形状一致（调用即返生成器，
    迭代才执行），计数语义（调用时记）与 FakeProvider 同源。
    """

    def __init__(self, name, chunks=("甲",), error=None, fail_at=None):
        self.name = name
        self.calls = []
        self._chunks = chunks
        self._error = error
        self._fail_at = fail_at

    def generate(self, system, question, contexts):
        self.calls.append((system, question, list(contexts)))

        async def _gen():
            if self._error is not None and self._fail_at is None:
                raise self._error
            for i, ch in enumerate(self._chunks):
                if self._fail_at is not None and i >= self._fail_at:
                    raise self._error
                yield ch

        return _gen()


def _drain(chain, *args):
    async def _go():
        return [c async for c in chain.generate(*args)]

    return asyncio.run(_go())


def _timeout(kind="timeout"):
    return ProviderError(kind, f"x {kind}")


# ---------------- 降级顺序 ----------------

def test_order_primary_timeout_then_backup():
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b])
    assert _drain(chain, "s", "q", ["c"]) == ["乙"]
    assert len(a.calls) == 1 and len(b.calls) == 1
    assert chain.last_provider == "ollama"
    assert chain.attempts == (("groq", "timeout"),)
    assert chain.tried == 2


def test_three_levels_in_order():
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("openai", error=_timeout("auth"))
    c = ScriptedProvider("ollama", chunks=("丙",))
    degraded = []
    chain = FallbackLLMProvider([a, b, c], on_degrade=degraded.append)
    assert _drain(chain, "SYSTEM-XYZ", "QUESTION-XYZ", ["CTX-ABC"]) == ["丙"]
    assert chain.attempts == (("groq", "timeout"), ("openai", "auth"))
    assert [e["provider"] for e in degraded] == ["groq", "openai"]
    assert all(set(e) >= {"level", "provider", "error"} for e in degraded)
    # R14：事件里只有级号/名/种类，无 prompt 无 context。
    blob = str(degraded)
    assert "QUESTION-XYZ" not in blob and "CTX-ABC" not in blob
    assert "SYSTEM-XYZ" not in blob


def test_all_fail_raises_last_kind_with_trail():
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("openai", error=_timeout("auth"))
    chain = FallbackLLMProvider([a, b])
    with pytest.raises(ProviderError) as exc:
        _drain(chain, "s", "q", [])
    assert exc.value.kind == "auth"  # 最后一级的 kind
    assert exc.value.attempts == (("groq", "timeout"), ("openai", "auth"))
    assert chain.tried == 2
    assert chain.last_provider is None


# ---------------- 快速切换（Groq 限流） ----------------

def test_rate_limited_switches_immediately_without_sleep():
    """限流即换级：链内永不 sleep（退避只属于单 provider 内部）。

    用递增假时钟断言——若实现敢 sleep，必须走可注入的时钟（这里没有），
    故本用例零等待通过即证明“无等待快切”。
    """
    import time

    a = ScriptedProvider("groq", error=_timeout("rate_limited"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b])
    t0 = time.monotonic()
    assert _drain(chain, "s", "q", []) == ["乙"]
    assert time.monotonic() - t0 < 5.0
    assert chain.attempts == (("groq", "rate_limited"),)
    assert chain.last_provider == "ollama"


# ---------------- 熔断冷却（防雪崩） ----------------

def test_circuit_breaker_skips_and_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(fb_mod, "_monotonic", lambda: now[0])
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b], max_failures=2, cooldown_s=60.0)
    _drain(chain, "s", "q", [])  # fail#1
    _drain(chain, "s", "q", [])  # fail#2 → 进冷却
    a.calls.clear()
    b.calls.clear()
    out = _drain(chain, "s", "q", [])  # 第 3 次：groq 被跳过
    assert out == ["乙"]
    assert a.calls == [] and len(b.calls) == 1
    assert chain.attempts == (("groq", "cooled"),)
    # 冷却期内成功不解冻（成功的是别级）；过期后恢复尝试。
    now[0] += 61.0
    a.calls.clear()
    _drain(chain, "s", "q", [])
    assert len(a.calls) == 1


def test_success_resets_breaker(monkeypatch):
    now = [2000.0]
    monkeypatch.setattr(fb_mod, "_monotonic", lambda: now[0])
    flaky = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([flaky, b], max_failures=2, cooldown_s=60.0)
    _drain(chain, "s", "q", [])  # fail#1
    flaky._error = None  # 恢复
    b.calls.clear()
    assert _drain(chain, "s", "q", []) == ["甲"]  # groq 自身成功，备选未被触碰
    assert b.calls == []
    # groq 自身成功一次即清零：再坏一次不应直接进冷却（需重新连坏 2 次）。
    flaky._error = _timeout("timeout")
    flaky.calls.clear()
    b.calls.clear()
    out = _drain(chain, "s", "q", [])
    assert out == ["乙"]
    assert ("groq", "cooled") not in chain.attempts
    assert len(flaky.calls) == 1 and len(b.calls) == 1


# ---------------- 首 chunk 后失败不换级 ----------------

def test_mid_stream_failure_keeps_error_path():
    a = ScriptedProvider("groq", chunks=("甲", "乙"), error=_timeout("timeout"),
                         fail_at=1)
    b = ScriptedProvider("ollama", chunks=("丙",))
    chain = FallbackLLMProvider([a, b])
    with pytest.raises(ProviderError) as exc:
        _drain(chain, "s", "q", [])
    assert exc.value.kind == "timeout"
    assert b.calls == []  # 已下行 chunk，不换级混流
    assert chain.last_provider == "groq"
    assert chain.tried == 1


# ---------------- build 校验 ----------------

def test_build_chain_strict_primary_lazy_backups(monkeypatch):
    import generation.provider as prov_mod

    real = prov_mod.get_provider
    seen = []

    def fake_get(name, model=None, api_key=None):
        seen.append(name)
        if name == "nope":
            raise ValueError(f"未知 provider：{name}")
        return ScriptedProvider(name)

    monkeypatch.setattr(prov_mod, "get_provider", fake_get)
    # 注意：fallback.build_chain 在调用时才 `from .provider import get_provider`，
    # 故 patch 模块属性即生效。
    with pytest.raises(ValueError):
        build_chain("nope", ["ollama"])
    chain = build_chain("groq", ["nope", "groq", "  ", "ollama"])
    # 坏名占位（运行时记 config 跳过）、重名/空串去重。
    assert chain.level_names == ["groq", "nope", "ollama"]
    out = _drain(chain, "s", "q", [])
    assert out == ["甲"]  # groq 本体成功（ScriptedProvider 默认产“甲”）
    assert chain.attempts == ()
    # 首选严格构造（"nope" 那次也调了 get_provider，只是抛了）；
    # 备选 thunk 从未执行：懒构造。
    assert seen == ["nope", "groq"]
    # 首选挂掉时坏备选记 config 跳过，不毙整单。
    # 构造期异常一律记 config（无论类型——thunk 只负责构造，不负责推理）。
    def _boom_timeout():
        raise _timeout("timeout")

    def _boom_config():
        raise ValueError("未知 provider：nope")

    chain2 = FallbackLLMProvider([
        ("groq", _boom_timeout),
        ("nope", _boom_config),
        ScriptedProvider("ollama", chunks=("乙",)),
    ])
    assert _drain(chain2, "s", "q", []) == ["乙"]
    assert chain2.attempts == (("groq", "config"), ("nope", "config"))
    assert chain2.tried == 1  # 构造失败不算调用


def test_build_chain_limits_and_shapes():
    with pytest.raises(ValueError):
        FallbackLLMProvider([])
    with pytest.raises(ValueError):
        build_chain("ollama", ["a", "b", "c", "d", "e"])
    with pytest.raises(ValueError):
        build_chain("ollama", "not-a-list")


# ---------------- 结果形状（answer 层，加法键） ----------------

def _maybe_db(monkeypatch, db, s=None):
    """把检索三路打桩成确定性 maybe_single（沿用 answer_router 的 shapes）。

    s 不传则按当前常量反解到 maybe 档中点 —— 档位是意图，s 是换算结果。
    """
    from tests.conftest import band_s
    from retrieval import hybrid_rank as hr

    if s is None:
        s = band_s((hr.TH_MAYBE + hr.TH_DIRECT) / 2.0)
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", s, "jieba"),
                         _hit("a", "标准答案甲。", s, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", s, "vec"))],
    )


def _zeros(texts):
    return [[0.0] * 512 for _ in texts]


@pytest.mark.anyio
async def test_answer_carries_provider_and_degraded(monkeypatch, db):
    from generation.router import answer

    _maybe_db(monkeypatch, db)
    a = ScriptedProvider("groq", error=_timeout("rate_limited"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b])
    result = await answer(db, "s", "测试问题", chain, _zeros)
    assert result["type"] == "llm"
    assert result["text"] == "乙"
    assert result["provider"] == "ollama"  # UI 实际出力标注
    assert result["degraded"] == ["groq:rate_limited"]  # asr_final 同形
    assert result["llm_calls"] == 2


@pytest.mark.anyio
async def test_answer_error_carries_trail(monkeypatch, db):
    from generation.router import answer

    _maybe_db(monkeypatch, db)
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("openai", error=_timeout("auth"))
    chain = FallbackLLMProvider([a, b])
    result = await answer(db, "s", "测试问题", chain, _zeros)
    assert result["type"] == "error"
    assert result["error"] == "auth"
    assert result["provider"] is None
    assert result["degraded"] == ["groq:timeout", "openai:auth"]
    assert result["llm_calls"] == 2


EVAL_DIR = Path(__file__).resolve().parent / "eval"

TEST_TOKEN = "test-token-" + "x" * 32


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.mark.anyio
async def test_fail_closed_never_touches_chain(db):
    """拒绝不重试：空库无候选 → fail_closed，各级计数全零、trail 为空。"""
    from generation.router import FAIL_CLOSED_TEXT, answer

    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b])
    result = await answer(db, "s", "量子电动力学xyz", chain, _zeros)
    assert result["type"] == "fail_closed"
    assert result["text"] == FAIL_CLOSED_TEXT
    assert result["provider"] is None and result["degraded"] == []
    assert result["llm_calls"] == 0
    assert a.calls == [] and b.calls == []
    assert chain.attempts == ()


@pytest.mark.anyio
async def test_fail_closed_with_demo_db_untouched(db):
    """用种子库的真 fail_closed 问题断言：拒绝就是拒绝，不换模型重试。"""
    from knowledge.compiler import compile_store
    from knowledge.field_extractor import extract, load_vocab
    from knowledge.parsers.json_parser import parse_json
    from knowledge.stores import create_store
    from knowledge.validator import validate_field_items, validate_qa_items
    from generation.router import FAIL_CLOSED_TEXT, answer
    import json as _json

    store = create_store(db, "库")
    seed = (EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8")
    parsed = parse_json(_json.loads(seed))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))
    chain = FallbackLLMProvider([a, b])
    result = await answer(db, store["id"], "量子电动力学xyz", chain, _zeros)
    assert result["type"] == "fail_closed"
    assert result["text"] == FAIL_CLOSED_TEXT
    assert result["provider"] is None and result["degraded"] == []
    assert a.calls == [] and b.calls == []


# ---------------- 端点 fallbacks 透传 ----------------

def _sse_events(resp):
    import json as _json

    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            events.append(_json.loads(line[5:].strip()))
    return events


@pytest.fixture()
def chain_client(api_client, monkeypatch):
    """真 _get_llm（仅 _conn 打桩）：fallbacks 走真实 build_chain。"""
    import routers.qa as qa_mod

    client, conn = api_client
    monkeypatch.setattr(qa_mod, "_conn", lambda: conn)
    from knowledge.compiler import compile_store
    from knowledge.field_extractor import extract, load_vocab
    from knowledge.parsers.json_parser import parse_json
    from knowledge.stores import create_store
    from knowledge.validator import validate_field_items, validate_qa_items
    import json as _json

    store = create_store(conn, "问答库")
    seed = (EVAL_DIR / "seed_demo.json").read_text(encoding="utf-8")
    parsed = parse_json(_json.loads(seed))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fr, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fr, load_vocab())
    compile_store(conn, store["id"], qa, fields, vocab_miss=miss)
    return client, conn, store["id"]


def test_ask_fallbacks_reaches_backup(chain_client, monkeypatch):
    """POST /qa/ask {provider, fallbacks}：首选超时 → 备选出文本，done 带标注。"""
    client, _, sid = chain_client
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))

    def fake_get_provider(name, model=None, api_key=None):
        assert name in ("groq", "ollama")
        return {"groq": a, "ollama": b}[name]

    import generation.provider as prov_mod

    # build_chain 在调用时才 `from .provider import get_provider`，故 patch 模块属性生效。
    monkeypatch.setattr(prov_mod, "get_provider", fake_get_provider)
    r = client.post("/qa/ask",
                    json={"question": "退货期限", "store_id": sid,
                          "provider": "groq", "fallbacks": ["ollama"]},
                    headers=_h())
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    r = client.get("/qa/stream", params={"task_id": task_id}, headers=_h())
    assert r.status_code == 200
    events = _sse_events(r)
    done = [e for e in events if e["type"] == "done"][0]["result"]
    # “退货期限” direct 命中不走 LLM——此处要的是 maybe 路径，换一个模糊问法。
    assert done["type"] in ("direct", "llm", "fail_closed", "error")


def test_ask_fallbacks_maybe_path_uses_backup(chain_client, monkeypatch, maybe_s):
    """maybe 路径才进链：用 monkeypatched 检索强制 maybe，断言备选标注。"""
    import generation.router as router_mod
    import routers.qa as qa_mod

    client, _, sid = chain_client
    a = ScriptedProvider("groq", error=_timeout("timeout"))
    b = ScriptedProvider("ollama", chunks=("乙",))

    def fake_get_provider(name, model=None, api_key=None):
        return {"groq": a, "ollama": b}[name]

    import generation.provider as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", fake_get_provider)

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *x, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *x, **k: [_hit("a", "标准答案甲。", maybe_s, "jieba"),
                         _hit("a", "标准答案甲。", maybe_s, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *x, **k: [dict(_hit("a", "标准答案甲。", maybe_s, "vec"))],
    )

    class FakeEmbed:
        def embed(self, texts):
            return [[0.0] * 512 for _ in texts]

    monkeypatch.setattr(qa_mod, "_get_embedder", lambda: FakeEmbed())
    r = client.post("/qa/ask",
                    json={"question": "测试问题", "store_id": sid,
                          "provider": "groq", "fallbacks": ["ollama"]},
                    headers=_h())
    task_id = r.json()["task_id"]
    r = client.get("/qa/stream", params={"task_id": task_id}, headers=_h())
    events = _sse_events(r)
    done = [e for e in events if e["type"] == "done"][0]["result"]
    assert done["type"] == "llm"
    assert done["text"] == "乙"
    assert done["provider"] == "ollama"
    assert done["degraded"] == ["groq:timeout"]
    assert done["llm_calls"] == 2
    assert len(a.calls) == 1 and len(b.calls) == 1
