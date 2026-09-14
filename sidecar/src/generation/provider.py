"""LLM Provider 抽象（PRD §3.7/F6.1）：流式 generate + embed。

- generate() 返回异步生成器（逐 chunk 流式）；调用方按需迭代。
- 超时 30s：httpx Timeout → ProviderError(kind="timeout")，走 error 路径。
- 错误映射（kind）：timeout / connection / http / protocol；错误信息只含
  URL 与状态码，永不含 API Key（R8：key 只进 Authorization 头，异常不回显头）。
"""

from typing import AsyncIterator, Protocol

import httpx

import json as _json

from core.prompt_guard import SYSTEM_PROMPT, build_prompt  # noqa: F401
# 提示词唯一定义见 core/prompt_guard.py（PRD §3.9）；此处重导出供旧引用。

TIMEOUT_S = 30.0

class ProviderError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind



class LLMProvider(Protocol):
    name: str

    def generate(
        self, system: str, question: str, contexts: list
    ) -> AsyncIterator[str]:
        """流式生成答案正文（逐 chunk）。"""
        ...

    async def embed(self, texts: list) -> list:
        """文本 → 向量（list of list[float]）。"""
        ...


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S))


def _map_httpx_error(e: Exception, where: str) -> ProviderError:
    if isinstance(e, httpx.TimeoutException):
        return ProviderError("timeout", f"{where} 超时（{TIMEOUT_S}s）")
    if isinstance(e, httpx.ConnectError):
        return ProviderError("connection", f"{where} 连接失败：{e}")
    if isinstance(e, httpx.HTTPStatusError):
        return ProviderError("http", f"{where} HTTP {e.response.status_code}")
    return ProviderError("protocol", f"{where} 异常：{e}")


def get_provider(name: str, model: str | None = None, api_key: str | None = None):
    """按名取 provider。key 缺省时读内存密钥库（settings 推送）。"""
    from core.secrets import get_llm_secret

    key = (name or "").strip().lower()
    if key == "ollama":
        return OllamaProvider(model=model or OllamaProvider.DEFAULT_MODEL)
    if key == "openai":
        resolved = api_key or get_llm_secret("openai")
        if not resolved:
            raise ValueError("openai 缺少 API Key（先经 settings 推送或构造传入）")
        return OpenAIProvider(api_key=resolved, model=model or OpenAIProvider.DEFAULT_MODEL)
    if key == "custom":
        return CustomProvider(
            base_url=(model or "").strip() or CustomProvider.DEFAULT_BASE_URL,
            api_key=api_key or get_llm_secret("custom"),
            model=None,
        )
    raise ValueError(f"未知 provider：{name}")


class OllamaProvider:
    """本地 Ollama（默认 qwen2.5:7b，零网络依赖可离线）。"""

    name = "ollama"
    DEFAULT_MODEL = "qwen2.5:7b"
    DEFAULT_BASE_URL = "http://127.0.0.1:11434"

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 client: httpx.AsyncClient | None = None):
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._client = client

    async def _post_stream(self, path: str, payload: dict):
        client = self._client or _client()
        try:
            async with client.stream("POST", self.base_url + path, json=payload) as resp:
                if resp.status_code != 200:
                    raise ProviderError("http", f"ollama {path} HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line:
                        yield line
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, f"ollama {path}")
        finally:
            if self._client is None:
                await client.aclose()

    async def generate(self, system: str, question: str, contexts: list):
        prompt = build_prompt(question, contexts or [])
        async for line in self._post_stream(
            "/api/generate", {"model": self.model, "prompt": prompt, "stream": True}
        ):
            try:
                obj = _json.loads(line)
            except ValueError as e:
                raise ProviderError("protocol", f"ollama 流解析失败：{e}")
            chunk = obj.get("response", "")
            if chunk:
                yield chunk
            if obj.get("done"):
                break

    async def embed(self, texts: list) -> list:
        client = self._client or _client()
        try:
            resp = await client.post(
                self.base_url + "/api/embed",
                json={"model": self.model, "input": list(texts)},
            )
            if resp.status_code != 200:
                raise ProviderError("http", f"ollama /api/embed HTTP {resp.status_code}")
            return resp.json()["embeddings"]
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "ollama /api/embed")
        finally:
            if self._client is None:
                await client.aclose()


class OpenAIProvider:
    """OpenAI（流式 chat completions）。"""

    name = "openai"
    DEFAULT_MODEL = "gpt-4o-mini"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_EMBED_MODEL = "text-embedding-3-small"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None,
                 embed_model: str | None = None,
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("openai 需要 API Key")
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.embed_model = embed_model or self.DEFAULT_EMBED_MODEL
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
                    raise ProviderError(
                        "http", f"openai chat HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = _json.loads(data)
                        chunk = obj["choices"][0]["delta"].get("content", "")
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        raise ProviderError("protocol", f"openai 流解析失败：{e}")
                    if chunk:
                        yield chunk
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "openai chat")
        finally:
            if self._client is None:
                await client.aclose()

    async def embed(self, texts: list) -> list:
        client = self._client or _client()
        try:
            resp = await client.post(
                self.base_url + "/embeddings",
                json={"model": self.embed_model, "input": list(texts)},
                headers=self._headers(),
            )
            if resp.status_code != 200:
                raise ProviderError("http", f"openai embeddings HTTP {resp.status_code}")
            return [d["embedding"] for d in resp.json()["data"]]
        except ProviderError:
            raise
        except Exception as e:
            raise _map_httpx_error(e, "openai embeddings")
        finally:
            if self._client is None:
                await client.aclose()


class CustomProvider(OpenAIProvider):
    """自定义 OpenAI 兼容端点（base_url 占位实现，鉴权同 Bearer）。"""

    name = "custom"
    DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, client=None):
        super().__init__(
            api_key=api_key or "custom-no-key",
            model=model or "default",
            base_url=base_url or self.DEFAULT_BASE_URL,
            client=client,
        )
