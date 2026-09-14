"""LLM API Key 内存持有（R8）。

- 仅进程内存 dict：不落盘、不打日志、不随任何响应回显。
- Rust 在握手后经认证端点推送当前所选 provider 的 key（settings 路由）。
- sidecar/LLM 重启即清空（内存语义）；Linux 加密文件 fallback 为 Phase 2。
"""

_store: dict = {}


def _norm(provider: str) -> str:
    return (provider or "").strip().lower()


def set_llm_secret(provider: str, api_key: str) -> str:
    name = _norm(provider)
    if not name:
        raise ValueError("provider 必填")
    if not api_key or not str(api_key).strip():
        raise ValueError("api_key 必填")
    _store[name] = str(api_key)
    return name


def get_llm_secret(provider: str):
    return _store.get(_norm(provider))


def clear_llm_secret(provider: str) -> None:
    _store.pop(_norm(provider), None)
