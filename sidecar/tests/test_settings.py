"""密钥通道单测：鉴权/不回显/内存持有（R8：key 不出现在任何响应与日志）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 与 conftest/test_auth 取值一致（同进程互不覆盖）。
TEST_TOKEN = "test-token-" + "x" * 32

from core.secrets import clear_llm_secret, get_llm_secret  # noqa: E402


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def test_push_requires_auth(api_client):
    client, _ = api_client
    r = client.post("/settings/llm-secret",
                    json={"provider": "openai", "api_key": "sk-x"})
    assert r.status_code == 401


def test_push_stores_memory_only(api_client):
    client, _ = api_client
    try:
        r = client.post("/settings/llm-secret",
                        json={"provider": "openai", "api_key": "sk-test-123"},
                        headers=_h())
        assert r.status_code == 200
        assert r.json() == {"stored": "openai"}
        assert "sk-test-123" not in r.text  # 响应不回显
        assert get_llm_secret("openai") == "sk-test-123"  # 内存持有
        assert get_llm_secret("OpenAI") == "sk-test-123"  # 归一化
    finally:
        clear_llm_secret("openai")
    assert get_llm_secret("openai") is None


def test_push_rejects_empty(api_client):
    client, _ = api_client
    assert client.post("/settings/llm-secret",
                       json={"provider": "openai", "api_key": "  "},
                       headers=_h()).status_code == 400
    assert client.post("/settings/llm-secret",
                       json={"provider": "  ", "api_key": "sk-x"},
                       headers=_h()).status_code == 400


def test_factory_uses_pushed_key(api_client):
    from generation.provider import get_provider

    client, _ = api_client
    try:
        client.post("/settings/llm-secret",
                    json={"provider": "openai", "api_key": "sk-from-store"},
                    headers=_h())
        assert get_provider("openai").api_key == "sk-from-store"
        assert get_provider("openai", api_key="sk-explicit").api_key == "sk-explicit"
    finally:
        clear_llm_secret("openai")
