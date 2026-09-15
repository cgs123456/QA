"""Claude Provider（Anthropic Messages API，SSE 流式）。

- 端点：POST {base_url}/v1/messages（默认 https://api.anthropic.com）。
- 鉴权：`x-api-key` + `anthropic-version: 2023-06-01`（R8：key 只进头，异常不回显）。
- 流式：`stream: true`，SSE 事件中仅 `event: content_block_delta` 携带正文
  `data: {"delta":{"type":"text_delta","text":"..."}}`；其余事件
  （message_start/ping/message_delta/…）全部忽略。
- 解析器独立状态机：逐行跟踪当前 `event:` 名，不与 OpenAI/Gemini 共用
  解析逻辑（三家 SSE 格式完全不同，强行抽象只会互相污染）。
- 错误映射：401/403 → auth；429 → rate_limited（供 P8 降级链区分限流）；
  其余非 200 → http；超时/连接沿用 `_map_httpx_error`。
"""

import json as _json

import httpx

from .provider import ProviderError, TIMEOUT_S, _client, _map_httpx_error

ANTHROPIC_VERSION = "2023-06-01"


def _status_error(where: str, status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth", f"{where} 鉴权失败 HTTP {status}")
    if status == 429:
        return ProviderError("rate_limited", f"{where} 限流 HTTP 429")
    return ProviderError("http", f"{where} HTTP {status}")


class ClaudeProvider:
    """Anthropic Claude（Messages API 流式）。"""

    name = "claude"
    DEFAULT_MODEL = "claude-3-5-sonnet-20240620"
    DEFAULT_BASE_URL = "https://api.anthropic.com"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None,
                 max_tokens: int = 1024,
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("claude 需要 API Key")
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.max_tokens = max_tokens
        self._client = client

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def generate(self, system: str, question: str, contexts: list):
        context_text = "\n\n".join(contexts or [])
        user = f"{context_text}\n\n{question}" if context_text else question
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
        }
        client = self._client or _client()
        try:
            async with client.stream(
                "POST", self.base_url + "/v1/messages",
                json=payload, headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    raise _status_error("claude messages", resp.status_code)
                # 独立 SSE 状态机：只认 content_block_delta，其余事件丢弃。
                event: str | None = None
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line:
                        event = None
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if event is not None and event != "content_block_delta":
                        continue
                    # 无 event 名的裸 data 行：保守忽略（不猜格式）。
                    if event is None:
                        continue
                    try:
                        obj = _json.loads(data)
                        chunk = obj.get("delta", {}).get("text", "")
                    except (ValueError, AttributeError) as e:
                        raise ProviderError("protocol", f"claude 流解析失败：{e}")
                    if chunk:
                        yield chunk
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "claude messages")
        finally:
            if self._client is None:
                await client.aclose()

    async def embed(self, texts: list) -> list:
        raise ProviderError("protocol", "claude 不支持 embed（走本地向量）")
