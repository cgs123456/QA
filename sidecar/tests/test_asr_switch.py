"""ASR Provider 切换（task17 / F6.2）：目录、切换端点、每段生效、覆盖优先级。

本文件**不需要权重**：切换板用替身（只换引用），provider 用 `StubASRProvider`。
真实权重的正确性由 `docs/benchmark.md` 的同样本对比负责，不由单测负责。
"""

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

import routers.audio as audio_mod
from app import app
from asr import runtime as asr_runtime
from asr.provider import ERR_UNAVAILABLE, ASRError, StubASRProvider

TEST_TOKEN = "test-token-" + "x" * 32

client = TestClient(app)


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


class _FakeSwitchboard:
    """替身切换板：不构造真 provider，只模拟「换引用」这一动作。"""

    def __init__(self, provider, selected="fake-a"):
        self._provider = provider
        self.selected = selected
        self.selects = []

    def current(self):
        return self._provider

    def swap(self, provider, name=None):
        """模拟一次切换：换掉引用（等价于 `select` 成功后的状态）。"""
        self._provider = provider
        if name:
            self.selected = name

    def select(self, name):
        self.selects.append(name)
        self.selected = name
        return {"selected": name}

    def describe(self):
        return {"selected": self.selected}


@pytest.fixture(autouse=True)
def _clean_slate():
    """清掉 audio 的测试覆盖 + 还原进程级切换板（本模块自管状态）。"""
    audio_mod.set_asr_provider(None)
    asr_runtime.set_switchboard(None)
    audio_mod._LAST_COUNTERS = None
    yield
    audio_mod.set_asr_provider(None)
    asr_runtime.set_switchboard(None)
    audio_mod._LAST_COUNTERS = None


# ---- 帧构造（与 test_audio_protocol 同构，勿改线格式） ----


def _audio(seq, ts, fill=0.0):
    return struct.pack("<BHI", 0x00, seq, ts) + struct.pack("<480f", *([fill] * 480))


def _event(seq, ts, obj):
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


def _start(seq, ts, path="loopback"):
    return _event(seq, ts, {"event": "segment_start", "ts_ms": ts, "path": path})


def _end(seq, ts, path="loopback"):
    return _event(seq, ts, {"event": "segment_end", "ts_ms": ts, "path": path})


def _one_segment(ws, base_seq, ts, fill=0.1):
    """跑完一个完整段，返回 asr_final/asr_error 的下行 JSON。"""
    ws.send_bytes(_start(base_seq, ts))
    assert ws.receive_json()["type"] == "asr_start"
    ws.send_bytes(_audio(base_seq + 1, ts + 30, fill))
    ws.send_bytes(_end(base_seq + 2, ts + 60))
    return ws.receive_json()


# ---- 端点 ----


def test_catalog_requires_auth():
    assert client.get("/asr/providers").status_code == 401
    assert client.put("/asr/provider", json={"name": "sensevoice"}).status_code == 401


def test_catalog_lists_all_providers(api_client):
    client_, _ = api_client
    body = client_.get("/asr/providers", headers=_h()).json()
    names = [e["name"] for e in body["available"]]
    assert names == ["faster-whisper", "sensevoice", "paraformer", "cloud-rest"]
    # 默认不动 task15 行为。
    assert body["selected"] == "faster-whisper"
    assert body["with_chain"] is True
    for entry in body["available"]:
        assert isinstance(entry["ready"], bool)
        assert entry["kind"] in ("local", "cloud")
    # 内容无关：不出现密钥/路径（R14）。
    assert "api_key" not in client_.get("/asr/providers", headers=_h()).text


def test_switch_unknown_name_is_400(api_client):
    client_, _ = api_client
    r = client_.put("/asr/provider", json={"name": "nope"}, headers=_h())
    assert r.status_code == 400


def test_switch_cloud_without_key_is_409_and_keeps_selection(api_client):
    from core.secrets import clear_llm_secret

    client_, _ = api_client
    clear_llm_secret("openai")
    r = client_.put("/asr/provider", json={"name": "cloud-rest"}, headers=_h())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == ERR_UNAVAILABLE
    # 切换失败必须保持原状，不能留下半切状态。
    assert client_.get("/asr/providers", headers=_h()).json()["selected"] == "faster-whisper"


def test_switch_local_provider_ok_without_weights(api_client):
    """本地 provider 切换只换引用、不加载权重 —— 权重缺失也必须能切。"""
    client_, _ = api_client
    r = client_.put("/asr/provider", json={"name": "sensevoice"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["selected"] == "sensevoice"
    assert r.json()["spec"]["model"] == "sense-voice"
    # 再切回，证明可反复切换（非单向）。
    assert client_.put("/asr/provider", json={"name": "paraformer"},
                       headers=_h()).json()["selected"] == "paraformer"


# ---- 每段生效（DoD：切换无需重启） ----


def test_switch_applies_to_next_segment_on_same_connection():
    """同一 WS 连接内：切换后**下一段**用新 provider，在途连接不重建。"""
    sb = _FakeSwitchboard(StubASRProvider(text="第一段", name="p1"), selected="p1")
    asr_runtime.set_switchboard(sb)

    with client.websocket_connect("/audio/stream", headers=_h()) as ws:
        first = _one_segment(ws, 0, 1000)
        assert first["type"] == "asr_final"
        assert first["text"] == "第一段"

        # 切换（等价于设置页点了另一个 provider）——连接保持不重建。
        sb.swap(StubASRProvider(text="第二段", name="p2"), name="p2")

        second = _one_segment(ws, 3, 2000)
        assert second["type"] == "asr_final"
        assert second["text"] == "第二段"
        assert second["segment_id"] == "seg_2"


def test_override_beats_switchboard():
    """`set_asr_provider` 的测试覆盖优先于切换板（既有用例不受 task17 影响）。"""
    asr_runtime.set_switchboard(_FakeSwitchboard(StubASRProvider(text="来自切换板")))
    audio_mod.set_asr_provider(StubASRProvider(text="来自覆盖"))

    with client.websocket_connect("/audio/stream", headers=_h()) as ws:
        assert _one_segment(ws, 0, 0)["text"] == "来自覆盖"


def test_switchboard_build_error_becomes_asr_error_not_crash():
    """切换板构造失败（如云端缺 key）→ 本段 asr_error，连接不崩。"""

    class _Boom:
        def current(self):
            raise ASRError(ERR_UNAVAILABLE, "需要 API Key")

    asr_runtime.set_switchboard(_Boom())

    with client.websocket_connect("/audio/stream", headers=_h()) as ws:
        ws.send_bytes(_start(0, 0))
        assert ws.receive_json()["type"] == "asr_start"
        ws.send_bytes(_end(1, 60))
        payload = ws.receive_json()
        assert payload["type"] == "asr_error"
        assert payload["error"] == ERR_UNAVAILABLE
