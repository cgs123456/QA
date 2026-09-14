"""task15 单测：ASR Provider / 降级链 / 下行扩充 / registry 条目。

覆盖：
- 降级链逐级切换、终级 manual_input、attempts 摘要；
- 超时按段时长派生（>3× 段时长，非固定值）；
- 降级事件内容无关（R14）；
- 下行 asr_final 带 provider / degraded，全灭带 manual_input_required；
- faster-whisper provider 权重未就位时快速失败且不静默联网；
- registry 新条目字段完整。
"""

import asyncio
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

import routers.audio as audio_mod
from app import app
from asr.cloud_rest import CloudRestProvider, pcm_f32_to_wav_bytes
from asr.fallback import (
    DEGRADE_TIMEOUT_FACTOR,
    MIN_TIMEOUT_S,
    FallbackASRProvider,
    build_default_chain,
)
from asr.faster_whisper_provider import FasterWhisperProvider
from asr.provider import (
    ERR_ALL_FAILED,
    ERR_MANUAL_INPUT,
    ERR_PROVIDER_ERROR,
    ERR_PROVIDER_TIMEOUT,
    ERR_UNAVAILABLE,
    ASRError,
    StubASRProvider,
    segment_seconds,
)
from core.auth import init_token

TEST_TOKEN = "test-token-" + "x" * 32
SECRET_TEXT = "绝密转写内容甲乙丙"

init_token(TEST_TOKEN)
client = TestClient(app)

# 1 秒 16kHz float32 单声道 = 64000 字节
ONE_SECOND_BYTES = 16000 * 4


def _headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _audio(seq, ts, fill=0.0):
    return struct.pack("<BHI", 0x00, seq, ts) + struct.pack("<480f", *([fill] * 480))


def _event(seq, ts, obj):
    return struct.pack("<BHI", 0x01, seq, ts) + json.dumps(obj).encode("utf-8")


def _start(seq, ts, path="loopback"):
    return _event(seq, ts, {"event": "segment_start", "path": path, "ts_ms": ts})


def _end(seq, ts, path="loopback"):
    return _event(seq, ts, {"event": "segment_end", "path": path, "ts_ms": ts})


# ---------------------------------------------------------------- 降级链


def test_chain_falls_through_to_the_next_level_and_reports_it():
    """第 0 级失败 → 第 1 级接手；结果必须标明实际出力的是谁。"""
    primary = StubASRProvider(fail_with=ASRError("unavailable", "no weights"), name="local-main")
    backup = StubASRProvider(text="备份结果", name="local-backup")
    events = []
    chain = FallbackASRProvider([primary, backup], on_degrade=events.append)

    res = asyncio.run(chain.transcribe_with_detail(b"\x00" * ONE_SECOND_BYTES, 16000, "mic"))

    assert res.text == "备份结果"
    assert res.provider == "local-backup"
    assert res.attempts == (("local-main", "unavailable"),)
    assert res.degrade_summary() == "local-main:unavailable"
    # 主级确实被调用过一次，备份也调用过一次
    assert len(primary.calls) == 1 and len(backup.calls) == 1
    # path 透传到 provider（诊断用，不参与转写）
    assert backup.paths == ["mic"]
    # 降级事件恰好一条，且指向失败的那一级
    assert len(events) == 1
    assert events[0]["level"] == 0 and events[0]["provider"] == "local-main"
    assert events[0]["error"] == "unavailable"


def test_chain_uses_the_first_level_when_it_works():
    """主级正常时不得触碰后续级（降级是异常路径，不是常态）。"""
    primary = StubASRProvider(text="主级结果", name="local-main")
    backup = StubASRProvider(text="备份结果", name="local-backup")
    chain = FallbackASRProvider([primary, backup])

    res = asyncio.run(chain.transcribe_with_detail(b"\x00" * ONE_SECOND_BYTES))

    assert res.text == "主级结果"
    assert res.provider == "local-main"
    assert res.attempts == ()
    assert len(backup.calls) == 0


def test_chain_exhausted_raises_manual_input_with_attempts():
    """全灭 → manual_input_required（第 3 级「纯 VAD + 手动输入」的信号）。"""
    a = StubASRProvider(fail_with=ASRError(ERR_UNAVAILABLE, "x"), name="local-main")
    b = StubASRProvider(fail_with=ASRError("http", "500"), name="cloud-rest")
    chain = FallbackASRProvider([a, b], manual_input=True)

    with pytest.raises(ASRError) as ei:
        asyncio.run(chain.transcribe(b"\x00" * ONE_SECOND_BYTES))

    assert ei.value.kind == ERR_MANUAL_INPUT
    assert ei.value.attempts == (("local-main", ERR_UNAVAILABLE), ("cloud-rest", "http"))


def test_chain_exhausted_without_manual_input_reports_all_failed():
    a = StubASRProvider(fail_with=ASRError("http", "500"), name="only")
    chain = FallbackASRProvider([a], manual_input=False)

    with pytest.raises(ASRError) as ei:
        asyncio.run(chain.transcribe(b"\x00" * ONE_SECOND_BYTES))

    assert ei.value.kind == ERR_ALL_FAILED
    assert ei.value.attempts == (("only", "http"),)


def test_unexpected_exception_degrades_instead_of_propagating():
    """非 ASRError 的意外异常也要降级，且只把类型名带进事件（不带 message）。"""
    class Boom:
        name = "boom"

        async def transcribe(self, pcm, sample_rate=16000, path=None):
            raise RuntimeError(f"内部细节 {SECRET_TEXT}")

    events = []
    good = StubASRProvider(text="ok", name="good")
    chain = FallbackASRProvider([Boom(), good], on_degrade=events.append)

    res = asyncio.run(chain.transcribe_with_detail(b"\x00" * ONE_SECOND_BYTES))

    assert res.provider == "good"
    assert res.attempts == (("boom", ERR_PROVIDER_ERROR),)
    assert SECRET_TEXT not in json.dumps(events, ensure_ascii=False)


def test_empty_chain_is_an_error_not_a_silent_empty_string():
    with pytest.raises(ASRError) as ei:
        asyncio.run(FallbackASRProvider([]).transcribe(b"\x00" * ONE_SECOND_BYTES))
    assert ei.value.kind == ERR_ALL_FAILED


# ---------------------------------------------------------------- 超时


def test_timeout_is_derived_from_segment_length_not_a_fixed_value():
    """坑位守卫：长段的超时预算必须随段时长增长。

    固定超时会把「15s 的段在 CPU int8 上要转 10s+」这一**容量事实**
    误判成**故障**并稳定降级。
    """
    chain = FallbackASRProvider([StubASRProvider()])
    fifteen_s = b"\x00" * (15 * ONE_SECOND_BYTES)

    assert segment_seconds(fifteen_s) == pytest.approx(15.0)
    assert chain.timeout_for(fifteen_s) == pytest.approx(15.0 * DEGRADE_TIMEOUT_FACTOR)
    # 明确断言：15s 段拿到的预算远大于任何"固定 10s"式取值
    assert chain.timeout_for(fifteen_s) > 30.0
    # 短段吃下限，不会因为按比例算而变得小到必然超时
    one_s = b"\x00" * ONE_SECOND_BYTES
    assert chain.timeout_for(one_s) == pytest.approx(MIN_TIMEOUT_S)
    assert chain.timeout_for(b"") == pytest.approx(MIN_TIMEOUT_S)


def test_a_provider_that_exceeds_its_budget_is_degraded():
    """超时确实触发降级（用缩小的时间尺度，不真等 45s）。"""
    slow = StubASRProvider(text="太慢", delay_s=0.5, name="slow")
    fast = StubASRProvider(text="快", name="fast")
    events = []
    chain = FallbackASRProvider([slow, fast], on_degrade=events.append,
                                timeout_factor=0.00001, min_timeout_s=0.05)

    res = asyncio.run(chain.transcribe_with_detail(b"\x00" * ONE_SECOND_BYTES))

    assert res.provider == "fast"
    assert res.attempts == (("slow", ERR_PROVIDER_TIMEOUT),)
    assert events[0]["error"] == ERR_PROVIDER_TIMEOUT
    assert events[0]["timeout_s"] == pytest.approx(0.05)


# ---------------------------------------------------------------- R14 内容无关


def test_degrade_events_carry_no_content(caplog):
    """降级事件只允许出现 级号/provider/kind/path/超时——不得含音频或文本。"""
    import logging

    seen = []
    failing = StubASRProvider(fail_with=ASRError("http", f"含密钥 {SECRET_TEXT}"),
                              name="cloud-rest")
    chain = FallbackASRProvider([failing, StubASRProvider(text=SECRET_TEXT, name="ok")],
                                on_degrade=seen.append)
    with caplog.at_level(logging.WARNING, logger="asr.fallback"):
        asyncio.run(chain.transcribe(b"\x11" * ONE_SECOND_BYTES, 16000, "loopback"))

    assert seen and set(seen[0]) == {"level", "provider", "error", "path", "timeout_s"}
    blob = json.dumps(seen, ensure_ascii=False) + caplog.text
    assert SECRET_TEXT not in blob
    # 音频字节也不能出现在事件里
    assert "0x11" not in blob and "\\x11" not in blob


# ---------------------------------------------------------------- 组装


def test_default_chain_omits_cloud_without_a_key():
    chain = build_default_chain(primary=StubASRProvider(name="local-main"))
    assert [p.name for p in chain.levels] == ["local-main"]


def test_default_chain_adds_cloud_only_when_a_key_exists():
    chain = build_default_chain(primary=StubASRProvider(name="local-main"),
                                cloud_api_key="sk-test")
    assert [p.name for p in chain.levels] == ["local-main", "cloud-rest"]
    assert chain.describe()["levels"] == ["local-main", "cloud-rest"]


def test_default_chain_keeps_same_kind_local_backup_in_the_middle():
    chain = build_default_chain(primary=StubASRProvider(name="local-main"),
                                local_backup=StubASRProvider(name="local-backup"),
                                cloud_api_key="sk-test")
    assert [p.name for p in chain.levels] == ["local-main", "local-backup", "cloud-rest"]


def test_default_chain_primary_is_lazy_and_survives_missing_weights():
    """primary 缺省构造不得加载权重，也不得因权重缺失而抛——降级留给第一次转写。"""
    chain = build_default_chain(primary=None, manual_input=True)
    assert chain.levels[0].name == "faster-whisper"


# ---------------------------------------------------------------- 云端 REST


def test_cloud_rest_requires_a_key():
    with pytest.raises(ValueError):
        CloudRestProvider(api_key="")


def test_pcm_to_wav_is_a_legal_16k_mono_pcm16_wav():
    import wave
    import io

    wav = pcm_f32_to_wav_bytes(struct.pack("<480f", *([0.5] * 480)), 16000)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == 480


def test_cloud_rest_maps_http_failure_without_leaking_the_key():
    class Resp:
        status_code = 500

    class FakeClient:
        async def post(self, *a, **kw):
            return Resp()

        async def aclose(self):
            pass

    p = CloudRestProvider(api_key="sk-super-secret", client=FakeClient())
    with pytest.raises(ASRError) as ei:
        asyncio.run(p.transcribe(b"\x00" * 100))
    assert ei.value.kind == ERR_PROVIDER_ERROR
    assert "sk-super-secret" not in str(ei.value)


# ---------------------------------------------------------------- faster-whisper provider


def test_local_provider_fails_fast_when_weights_are_absent(tmp_path):
    """权重未就位 → unavailable 快速失败，**不静默联网下载**。"""
    p = FasterWhisperProvider(model_dir=tmp_path / "empty")
    assert p.is_ready() is False
    assert len(p.missing_files()) == 4
    with pytest.raises(ASRError) as ei:
        asyncio.run(p.transcribe(b"\x00" * ONE_SECOND_BYTES))
    assert ei.value.kind == ERR_UNAVAILABLE
    assert p.load_count == 0  # 根本没尝试加载


def test_local_provider_reports_ready_when_all_registry_files_exist(tmp_path):
    from models import registry

    entry = registry.MODEL_REGISTRY[registry.FASTER_WHISPER_BASE]
    for name in entry["files"]:
        (tmp_path / name).write_bytes(b"x")
    p = FasterWhisperProvider(model_dir=tmp_path)
    assert p.is_ready() is True
    assert p.missing_files() == []
    d = p.describe()
    assert d["provider"] == "faster-whisper" and d["model"] == "base"
    assert d["compute_type"] == "int8" and d["device"] == "cpu"
    assert d["language"] == "auto"  # task15：语言自动


def test_local_provider_rejects_non_16k_and_unaligned_pcm(tmp_path):
    from models import registry

    entry = registry.MODEL_REGISTRY[registry.FASTER_WHISPER_BASE]
    for name in entry["files"]:
        (tmp_path / name).write_bytes(b"x")
    p = FasterWhisperProvider(model_dir=tmp_path)
    with pytest.raises(ASRError) as ei:
        p._transcribe_sync(b"\x00" * 8, 8000)
    assert "16kHz" in str(ei.value)
    with pytest.raises(ASRError) as ei2:
        p._transcribe_sync(b"\x00" * 7, 16000)
    assert ei2.value.kind == ERR_PROVIDER_ERROR


# ---------------------------------------------------------------- registry 条目


def test_registry_entry_for_faster_whisper_is_fully_pinned():
    from models import registry

    entry = registry.MODEL_REGISTRY[registry.FASTER_WHISPER_BASE]
    files = entry["files"]
    assert set(files) == {"model.bin", "config.json", "tokenizer.json", "vocabulary.txt"}
    for name, spec in files.items():
        assert len(spec["sha256"]) == 64, f"{name} sha256 未 pin"
        assert int(spec["sha256"], 16) >= 0
        assert spec["size"] > 0
        # 镜像顺序：ModelScope → hf-mirror → HF（三个都在，不调序）
        assert len(spec["urls"]) == 3
        assert spec["urls"][0].startswith("https://modelscope.cn/")
        assert spec["urls"][1].startswith("https://hf-mirror.com/")
        assert spec["urls"][2].startswith("https://huggingface.co/")
    # 权威来源核对：model.bin 的 sha256 取自 HF LFS oid
    assert files["model.bin"]["sha256"] == (
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9")
    assert files["model.bin"]["size"] == 145217532


def test_registry_urls_are_well_formed_per_file():
    """每个文件的三个镜像 URL 都必须指向同名远端路径（拼错就静默 404）。"""
    from models import registry

    files = registry.MODEL_REGISTRY[registry.FASTER_WHISPER_BASE]["files"]
    for name, spec in files.items():
        for url in spec["urls"]:
            assert url.endswith("/" + name), f"{name} 的 URL 路径不匹配：{url}"
            assert spec["remote"] in url


# ---------------------------------------------------------------- 下行扩充


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    audio_mod.set_asr_provider(StubASRProvider(text=SECRET_TEXT))
    audio_mod._LAST_COUNTERS = None


def test_asr_final_carries_the_provider_that_actually_worked():
    backup = StubASRProvider(text="降级后文本", name="local-backup")
    chain = FallbackASRProvider(
        [StubASRProvider(fail_with=ASRError(ERR_UNAVAILABLE, "no weights"),
                         name="local-main"), backup])
    audio_mod.set_asr_provider(chain)

    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        assert ws.receive_json()["type"] == "asr_start"
        ws.send_bytes(_audio(1, 30, 0.4))
        ws.send_bytes(_end(2, 60))
        final = ws.receive_json()

    assert final["type"] == "asr_final"
    assert final["text"] == "降级后文本"
    assert final["provider"] == "local-backup"
    assert final["degraded"] == ["local-main:unavailable"]
    assert final["path"] == "loopback"
    assert final["duration_ms"] == 60


def test_asr_final_omits_degraded_when_no_degradation_happened():
    audio_mod.set_asr_provider(StubASRProvider(text="直接结果", name="local-main"))

    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.4))
        ws.send_bytes(_end(2, 60))
        final = ws.receive_json()

    assert final["provider"] == "local-main"
    assert "degraded" not in final


def test_chain_exhaustion_reaches_the_client_as_manual_input():
    """全灭 → 客户端收到 manual_input_required + 逐级摘要（前端据此切手动输入）。"""
    chain = FallbackASRProvider([
        StubASRProvider(fail_with=ASRError(ERR_UNAVAILABLE, "no weights"), name="local-main"),
        StubASRProvider(fail_with=ASRError("http", "502"), name="cloud-rest"),
    ], manual_input=True)
    audio_mod.set_asr_provider(chain)

    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.4))
        ws.send_bytes(_end(2, 60))
        err = ws.receive_json()

    assert err["type"] == "asr_error"
    assert err["error"] == ERR_MANUAL_INPUT
    assert err["degraded"] == ["local-main:unavailable", "cloud-rest:http"]
    assert err["path"] == "loopback"


def test_plain_provider_still_reports_its_name_downstream():
    """非降级链的普通 provider 也要带 provider 名（协议向后兼容）。"""
    audio_mod.set_asr_provider(StubASRProvider(text="单 provider", name="stub"))

    with client.websocket_connect("/audio/stream", headers=_headers()) as ws:
        ws.send_bytes(_start(0, 0))
        ws.receive_json()
        ws.send_bytes(_audio(1, 30, 0.4))
        ws.send_bytes(_end(2, 60))
        final = ws.receive_json()

    assert final["provider"] == "stub"
    assert "degraded" not in final
