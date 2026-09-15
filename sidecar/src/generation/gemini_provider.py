"""Gemini Provider（streamGenerateContent，alt=sse 流式）。

- 端点：POST {base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse
  （默认 https://generativelanguage.googleapis.com）。
- 鉴权：`x-goog-api-key` 头（不用 URL ?key=，免 key 进日志/异常 URL）。
- 流式：SSE `data:` 行 JSON
  `{"candidates":[{"content":{"parts":[{"text":"..." dill}]}}]}`；
  无 `[DONE]`（流结束即完），空 candidates（如仅 promptFeedback）跳过。
- 解析器独立：不跟踪 `event:`（Gemini 不发事件名），与 Claude/Groq
  状态机各自独立。
- 错误映射：400/401/403 → auth（无效 key 时 Google 常回 400，
  与 401 同视为鉴权失败）；429 → rate_limited；其余非 200 → http。
"""

import json as _json
import urllib.parse

import httpx

from .provider import ProviderError, TIMEOUT_S, _client, _map_httpx_error


def _status_error(where: str, status: int) -> ProviderError:
    if status in (400, 401, 403):
        return ProviderError("auth", f"{where} 鉴权失败 HTTP {status}")
    if status == 429:
        return ProviderError("rate_limited", f"{where} 限流 HTTP 429")
    return ProviderError("http", f"{where} HTTP {status}")


class GeminiProvider:
    """Google Gemini（streamGenerateContent SSE）。"""

    name = "gemini"
    DEFAULT_MODEL = "gemini-1.5-flash"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None,
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("gemini 需要 API Key")
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._client = client

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "content-type": "application/json"}

    def _url(self) -> str:
        model_path = urllib.parse.quote(self.model, safe=".-_")
        return (f"{self.base_url}/v1beta/models/{model_path}"
                ":streamGenerateContent?alt=sse")

    async def generate(self, system: str, question: str, contexts: list):
        context_text = "\n\n".join(contexts or [])
        user = f"{context_text}\n\n{question}" if context_text else question
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        client = self._client or _client()
        try:
            async with client.stream(
                "POST", self._url(), json=payload, headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    raise _status_error("gemini streamGenerateContent",
                                        resp.status_code)
                # 独立解析：逐 data 行取 candidates[0].content.parts[*].text。
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = _json.loads(data)
                    except ValueError as e:
                        raise ProviderError("protocol", f"gemini 流解析失败：{e}")
                    try:
                        cands = obj.get("candidates", [])
                        if not cands:
                            continue
                        parts = cands[0].get("content", {}).get("parts", [])
                        chunk = "".join(
                            p.get("text", "") for p in parts
                            if isinstance(p, dict)
                        )
                    except (AttributeError, TypeError) as e:
                        raise ProviderError("protocol", f"gemini 流解析失败：{e}")
                    if chunk:
                        yield chunk
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "gemini streamGenerateContent")
        finally:
            if self._client is None:
                await client.aclose()

    async def embed(self, texts: list) -> list:
        raise ProviderError("protocol", "gemini embed 走本地向量（本 provider 仅 generate）")
