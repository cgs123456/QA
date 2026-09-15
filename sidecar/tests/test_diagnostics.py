"""音频诊断端点单测（task19）：鉴权/空态/建连后计数快照/内容无关。

模块级 TestClient 直连 app（同 test_audio_protocol.py，不经过需 libsimple
 vendor 的 `api_client` fixture——vendor 二进制本机缺失时也能跑）。
用真 WS 建连驱动 `routers/audio.py`，不断言转写文本（R14：响应只含计数与限额）。
"""

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from fastapi.testclient import TestClient

import routers.audio as audio_mod  # noqa: E402
from app import app  # noqa: E402
from asr.provider import StubASRProvider  # noqa: E402
from core.auth import init_token  # noqa: E402

TEST_TOKEN = "test-token-" + "x" * 32
SECRET_TEXT = "绝密转写内容甲乙丙"

init_token(TEST_TOKEN)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    audio_mod.set_asr_provider(StubASRProvider(text=SECRET_TEXT))
    audio_mod._LAST_COUNTERS = None
    yield
    audio_mod.set_asr_provider(None)
    audio_mod._LAST_COUNTERS = None


def _h(token=TEST_TOKEN):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _audio(seq, ts, fill=0.0):
    return struct.pack("<BHI", 0x00, seq, ts) + struct.pack("<480f", *([fill] * 480))


def _event(seq, ts, obj):
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


def test_diagnostics_requires_auth():
    assert client.get("/diagnostics/audio").status_code == 401
    assert client.get("/diagnostics/audio",
                      headers=_h("wrong")).status_code == 401


def test_diagnostics_empty_before_any_connection():
    r = client.get("/diagnostics/audio", headers=_h())
    assert r.status_code == 200
    body = r.json()
    # 从未建连是合法状态（null），不是 404。
    assert body["last_connection"] is None
    assert body["limits"]["max_pending"] == audio_mod.MAX_PENDING
    assert body["limits"]["max_segment_frames"] == audio_mod.MAX_SEGMENT_FRAMES
    # capture 段报 sidecar 真正看得见的事实（不再是占位 "pending"）：
    # 从未建连 → idle + 空 active_paths。
    assert body["capture"]["status"] == "idle"
    assert body["capture"]["active_paths"] == []


def test_diagnostics_capture_reflects_a_live_connection():
    """在连时 capture 段必须从 idle 变 connected 并列出 path，断开后变 disconnected。

    这是"UI 说在采、sidecar 什么都没收到"这类排查唯一需要的那个事实。
    """
    with client.websocket_connect("/audio/stream", headers=_h()) as ws:
        ws.send_bytes(_event(0, 0, {"event": "segment_start",
                                    "ts_ms": 0, "path": "loopback"}))
        ws.receive_json()
        live = client.get("/diagnostics/audio", headers=_h()).json()["capture"]
        assert live["status"] == "connected"
        assert live["active_paths"] == ["loopback"]
    after = client.get("/diagnostics/audio", headers=_h()).json()["capture"]
    assert after["status"] == "disconnected"
    assert after["active_paths"] == []


def test_diagnostics_reflects_last_connection():
    with client.websocket_connect("/audio/stream", headers=_h()) as ws:
        ws.send_bytes(_event(0, 0, {"event": "segment_start",
                                    "ts_ms": 0, "path": "loopback"}))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.1))
        # 跳号一帧：seq_gap 计数应为 1（诊断可见性）。
        ws.send_bytes(_audio(5, 60, 0.1))
        ws.send_bytes(_event(6, 90, {"event": "segment_end",
                                     "ts_ms": 90, "path": "loopback"}))
        ws.receive_json()
    body = client.get("/diagnostics/audio", headers=_h()).json()
    last = body["last_connection"]
    assert last["seq_gap"] == 1
    assert last["malformed"] == 0
    # 内容无关：诊断响应全文不得出现转写文本（R14）。
    assert SECRET_TEXT not in json.dumps(body)
