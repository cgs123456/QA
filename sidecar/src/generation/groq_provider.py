"""Groq Provider（OpenAI 兼容 chat/completions 流式）。

- 端点：POST {base_url}/chat/completions
  （默认 https://api.groq.com/openai/v1，即 base_url 指向 groq）。
- 鉴权：`Authorization: Bearer`（R8：key 只进头）。
- 流式：OpenAI 同形 `data: {"choices":[{"delta":{"content":"..."}}]}`
  … `data: [DONE]`。解析器与 OpenAIProvider **各自独立实现**
  （不共用状态机：三家 SSE 格式不同，任务要求不强行抽象）。
- 错误映射：401/403 → auth；429 → rate_limited **单列**
  （Groq 免费档限流严格，供 P8 降级链区分限流与普通 http 错误）；
  其余非 200 → http。
"""

import json as _json

import httpx

from .provider import ProviderError, TIMEOUT_S, _client, _map_httpx_error


def _status_error(where: str, status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth", f"{where} 鉴权失败 HTTP {status}")
    if status == 429:
        return ProviderError("rate_limited", f"{where} 限流 HTTP 429")
    return ProviderError("http", f"{where} HTTP {status}")


class GroqProvider:
    """Groq（OpenAI 兼容，免费档限流 kind 单列）。"""

    name = "groq"
    DEFAULT_MODEL = "llama-3.1-70b-versatile"
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None,
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("groq 需要 API Key")
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._client = client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def generate(self, system: str, question: str, contexts: list):
        context_text = "\n\n".join(contexts or [])
        user = f"{context_text}\n\n{question}" if context_text else question
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
        }
        client = self._client or _client()
        try:
            async with client.stream(
                "POST", self.base_url + "/chat/completions",
                json=payload, headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    raise _status_error("groq chat", resp.status_code)
                # 独立 SSE 解析（与 OpenAIProvider 同形但不共用函数）。
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = _json.loads(data)
                        chunk = obj["choices"][0]["delta"].get("content", "")
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        raise ProviderError("protocol", f"groq 流解析失败：{e}")
                    if chunk:
                        yield chunk
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "groq chat")
        finally:
            if self._client is None:
                await client.aclose()

    async def embed(self, texts: list) -> list:
        raise ProviderError("protocol", "groq 不支持 embed（走本地向量）")
