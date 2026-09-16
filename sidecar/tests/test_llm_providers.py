"""F6.1 全量：claude / gemini / groq Provider 单测。

- 三家 SSE 格式不同，解析器各自独立（本文件按家分别构造 Mock 流断言）。
- 错误映射：401/403→auth、429→rate_limited（Groq 单列供 P8）、其余→http；
  超时/连接沿用 _map_httpx_error；key 永不进异常文本（R8）。
- 四档判定（direct/maybe/fail_closed/error）对每个新 provider 参数化验证，
  Mock 下 llm_calls 计数与 task-7 既有语义一致。
- 模式对齐 task-7 test_providers.py：httpx.MockTransport + 真流迭代。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from generation.claude_provider import ClaudeProvider
from generation.gemini_provider import GeminiProvider
from generation.groq_provider import GroqProvider
from generation.provider import (
    ProviderError,
    describe_llm_providers,
    get_provider,
    list_llm_providers,
)

TEST_TOKEN = "test-token-" + "x" * 32


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ---------- registry / catalog ----------

def test_catalog_lists_six_in_order():
    assert list_llm_providers() == ["ollama", "openai", "claude", "gemini", "groq", "custom"]


def test_catalog_entries_content_irrelevant():
    entries = describe_llm_providers()
    assert len(entries) == 6
    for e in entries:
        assert "api_key" not in str(e).lower()
        assert e["name"] in ("ollama", "openai", "claude", "gemini", "groq", "custom")
    by_name = {e["name"]: e for e in entries}
    assert by_name["claude"]["stream"] == "SSE event: content_block_delta"
    assert "alt=sse" in by_name["gemini"]["stream"]
    assert "OpenAI" in by_name["groq"]["stream"]
    assert by_name["groq"]["note"].startswith("免费档限流严格")


def test_get_provider_routing_new():
    assert isinstance(get_provider("claude", api_key="k"), ClaudeProvider)
    assert get_provider("claude", api_key="k").model == "claude-3-5-sonnet-20240620"
    assert isinstance(get_provider("gemini", api_key="k"), GeminiProvider)
    assert get_provider("gemini", api_key="k").model == "gemini-1.5-flash"
    assert isinstance(get_provider("groq", api_key="k"), GroqProvider)
    assert get_provider("groq", api_key="k").model == "llama-3.1-70b-versatile"
    # 大小写/空白归一
    assert isinstance(get_provider(" Claude ", api_key="k"), ClaudeProvider)
    with pytest.raises(ValueError):
        get_provider("claude")
    with pytest.raises(ValueError):
        get_provider("gemini")
    with pytest.raises(ValueError):
        get_provider("groq")
    with pytest.raises(ValueError):
        ClaudeProvider(api_key="")
    with pytest.raises(ValueError):
        GeminiProvider(api_key="")
    with pytest.raises(ValueError):
        GroqProvider(api_key="")


def test_factory_uses_pushed_keys_new(api_client):
    """三新 provider 密钥复用 /settings/llm-secret（与 openai 同通道）。"""
    from core.secrets import clear_llm_secret

    client, _ = api_client
    try:
        for name in ("claude", "gemini", "groq"):
            r = client.post("/settings/llm-secret",
                            json={"provider": name, "api_key": f"sk-{name}-123"},
                            headers=_h())
            assert r.status_code == 200
            assert r.json() == {"stored": name}
            assert f"sk-{name}-123" not in r.text
            assert get_provider(name).api_key == f"sk-{name}-123"
            # 显式传入优先于内存
            assert get_provider(name, api_key="sk-explicit").api_key == "sk-explicit"
    finally:
        for name in ("claude", "gemini", "groq"):
            clear_llm_secret(name)


def test_llm_providers_endpoint(api_client):
    client, _ = api_client
    # 无 token → 401（R2：鉴权先于业务）
    assert client.get("/llm/providers").status_code == 401
    r = client.get("/llm/providers", headers=_h())
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["providers"]]
    assert names == ["ollama", "openai", "claude", "gemini", "groq", "custom"]
    assert "sk-" not in r.text


# ---------- claude ----------

@pytest.mark.anyio
async def test_claude_sse_parse():
    body = (b"event: message_start\n"
            b'data: {"type":"message_start"}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" there"}}\n\n'
            b"event: message_stop\n"
            b'data: {"type":"message_stop"}\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "k"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, content=body)

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    chunks = [c async for c in p.generate("sys", "q", ["ctx"])]
    assert chunks == ["Hi", " there"]


@pytest.mark.anyio
async def test_claude_ignores_other_events():
    """ping / message_delta / 空行 / 裸 data 行全部忽略，只取 delta 文本。"""
    body = (b"event: ping\n"
            b'data: {"type":"ping"}\n\n'
            b"\n"
            b'data: {"orphan":true}\n\n'
            b"event: message_delta\n"
            b'data: {"type":"message_delta"}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"A"}}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":""}}\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    assert [c async for c in p.generate("s", "q", [])] == ["A"]


@pytest.mark.anyio
async def test_claude_401_maps_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"nope")

    p = ClaudeProvider(api_key="SECRET-K", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "auth"
    assert "SECRET-K" not in str(exc.value)


@pytest.mark.anyio
async def test_claude_429_maps_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow")

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "rate_limited"


@pytest.mark.anyio
async def test_claude_500_maps_http():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "http"


@pytest.mark.anyio
async def test_claude_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: content_block_delta\n"
                                           b"data: not-json\n\n")

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "protocol"


@pytest.mark.anyio
async def test_claude_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("t")

    p = ClaudeProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "timeout"


# ---------- gemini ----------

@pytest.mark.anyio
async def test_gemini_sse_parse():
    body = (b'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n\n'
            b'data: {"candidates":[{"content":{"parts":[{"text":" world"}]}}]}\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        assert ":streamGenerateContent" in request.url.path
        assert "alt=sse" in str(request.url)
        assert request.headers["x-goog-api-key"] == "k"
        # key 不进 URL（R8：用头不用 ?key=）
        assert "k" not in str(request.url)
        return httpx.Response(200, content=body)

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    assert [c async for c in p.generate("sys", "q", ["ctx"])] == ["Hello", " world"]


@pytest.mark.anyio
async def test_gemini_skips_empty_and_multipart():
    """空 candidates（promptFeedback）跳过；多 part 拼接。"""
    body = (b'data: {"promptFeedback":{}}\n\n'
            b'data: {"candidates":[{"content":{"parts":[{"text":"A"},{"text":"B"}]}}]}\n\n'
            b'data: {"candidates":[]}\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    assert [c async for c in p.generate("s", "q", [])] == ["AB"]


@pytest.mark.anyio
async def test_gemini_401_maps_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"bad key")

    p = GeminiProvider(api_key="SECRET-K", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "auth"
    assert "SECRET-K" not in str(exc.value)


@pytest.mark.anyio
async def test_gemini_400_maps_auth():
    """Google 无效 key 常回 400（非 401），同视为 auth。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"API key not valid")

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "auth"


@pytest.mark.anyio
async def test_gemini_429_maps_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow")

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "rate_limited"


@pytest.mark.anyio
async def test_gemini_500_maps_http():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "http"


@pytest.mark.anyio
async def test_gemini_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: not-json\n\n")

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "protocol"


@pytest.mark.anyio
async def test_gemini_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("t")

    p = GeminiProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "timeout"


# ---------- groq ----------

@pytest.mark.anyio
async def test_groq_sse_parse():
    body = (b'data: {"choices":[{"delta":{"content":"Yo"}}]}\n\n'
            b'data: {"choices":[{"delta":{}}]}\n\ndata: [DONE]\n')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        assert request.url.path == "/openai/v1/chat/completions"
        assert "groq" in str(request.url)
        return httpx.Response(200, content=body)

    # base_url 指向 groq（OpenAI 兼容）：默认即 groq 域。
    p = GroqProvider(api_key="k", client=_mock_client(handler))
    assert "groq.com" in p.base_url
    assert [c async for c in p.generate("sys", "q", ["ctx"])] == ["Yo"]


@pytest.mark.anyio
async def test_groq_401_maps_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"bad")

    p = GroqProvider(api_key="SECRET-K", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "auth"
    assert "SECRET-K" not in str(exc.value)


@pytest.mark.anyio
async def test_groq_429_maps_rate_limited_single_kind():
    """Groq 免费档限流严格：429 必须单列 rate_limited（供 P8 降级链用）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limit")

    p = GroqProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "rate_limited"
    assert exc.value.kind != "http"


@pytest.mark.anyio
async def test_groq_500_maps_http():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    p = GroqProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "http"


@pytest.mark.anyio
async def test_groq_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: not-json\n\n")

    p = GroqProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "protocol"


@pytest.mark.anyio
async def test_groq_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("t")

    p = GroqProvider(api_key="k", client=_mock_client(handler))
    with pytest.raises(ProviderError) as exc:
        [c async for c in p.generate("s", "q", [])]
    assert exc.value.kind == "timeout"


@pytest.mark.anyio
async def test_new_providers_connection_mapping():
    """连接失败统一 kind=connection（与 task-7 既有映射一致）。"""
    from generation.provider import _map_httpx_error

    assert _map_httpx_error(httpx.ConnectError("c"), "x").kind == "connection"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("c")

    for cls in (ClaudeProvider, GeminiProvider, GroqProvider):
        p = cls(api_key="k", client=_mock_client(handler))
        with pytest.raises(ProviderError) as exc:
            [c async for c in p.generate("s", "q", [])]
        assert exc.value.kind == "connection"


# ---------- 四档判定 × 三 provider（Mock 下） ----------

def _make_provider(name: str, handler):
    client = _mock_client(handler)
    if name == "claude":
        return ClaudeProvider(api_key="k", client=client), client
    if name == "gemini":
        return GeminiProvider(api_key="k", client=client), client
    assert name == "groq"
    return GroqProvider(api_key="k", client=client), client


def _ok_handler(name: str):
    """各家 happy-path Mock 流（各出一块“甲”）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if name == "claude":
            body = (b"event: content_block_delta\n"
                    b'data: {"delta":{"text":"\\u7532"}}\n\n')
            return httpx.Response(200, content=body)
        if name == "gemini":
            body = (b'data: {"candidates":[{"content":{"parts":[{"text":"\\u7532"}]}}]}\n\n')
            return httpx.Response(200, content=body)
        body = (b'data: {"choices":[{"delta":{"content":"\\u7532"}}]}\n\n'
                b"data: [DONE]\n")
        return httpx.Response(200, content=body)
    return handler


def _err_handler(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"e")
    return handler


def _zeros(texts):
    return [[0.0] * 512 for _ in texts]


@pytest.fixture()
def demo_db2(db):
    from knowledge.compiler import compile_store
    from knowledge.field_extractor import extract, load_vocab
    from knowledge.parsers.json_parser import parse_json
    from knowledge.stores import create_store
    from knowledge.validator import validate_field_items, validate_qa_items

    store = create_store(db, "答案库")
    seed = (Path(__file__).resolve().parent / "eval" / "seed_demo.json").read_text(encoding="utf-8")
    import json as _json

    parsed = parse_json(_json.loads(seed))
    qa, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    fields, miss = extract(fields_raw, load_vocab())
    compile_store(db, store["id"], qa, fields, vocab_miss=miss)
    return db, store["id"]


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["claude", "gemini", "groq"])
async def test_four_direct_no_llm(name, demo_db2):
    """direct 档：零 LLM 调用（MockTransport 零命中）。"""
    from generation.router import answer

    db, sid = demo_db2
    hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, content=b"unused")

    llm, _ = _make_provider(name, handler)
    result = await answer(db, sid, "退货期限", llm, _zeros)
    assert result["type"] == "direct"
    assert result["llm_calls"] == 0
    assert hits == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["claude", "gemini", "groq"])
async def test_four_maybe_llm(name, db, monkeypatch, maybe_s):
    """maybe 档：LLM 被调用一次，流文本为“甲”。"""
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", maybe_s, "jieba"),
                         _hit("a", "标准答案甲。", maybe_s, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", maybe_s, "vec"))],
    )
    llm, _ = _make_provider(name, _ok_handler(name))
    result = await router_mod.answer(db, "s", "测试问题", llm, _zeros)
    assert result["type"] == "llm"
    assert result["text"] == "甲"
    assert result["llm_calls"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["claude", "gemini", "groq"])
async def test_four_fail_closed_no_llm(name, demo_db2):
    """fail_closed 档：零 LLM 调用。"""
    from generation.router import FAIL_CLOSED_TEXT, answer

    db, sid = demo_db2
    hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, content=b"unused")

    llm, _ = _make_provider(name, handler)
    result = await answer(db, sid, "量子电动力学xyz", llm, _zeros)
    assert result["type"] == "fail_closed"
    assert result["text"] == FAIL_CLOSED_TEXT
    assert result["llm_calls"] == 0
    assert hits == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["claude", "gemini", "groq"])
async def test_four_error_path(name, db, monkeypatch, maybe_s):
    """error 档：LLM 429 → rate_limited，走 error 路径（llm_calls=1）。"""
    import generation.router as router_mod

    def _hit(qid, text, s, route):
        return {"qa_id": qid, "standard_question": "Q", "official_answer": text,
                "category": "E", "route": route, "s": s}

    monkeypatch.setattr(router_mod, "field_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        router_mod, "fts5_search",
        lambda *a, **k: [_hit("a", "标准答案甲。", maybe_s, "jieba"),
                         _hit("a", "标准答案甲。", maybe_s, "simple")],
    )
    monkeypatch.setattr(
        router_mod, "vector_search",
        lambda *a, **k: [dict(_hit("a", "标准答案甲。", maybe_s, "vec"))],
    )
    llm, _ = _make_provider(name, _err_handler(429))
    result = await router_mod.answer(db, "s", "测试问题", llm, _zeros)
    assert result["type"] == "error"
    assert result["error"] == "rate_limited"
    assert result["llm_calls"] == 1
