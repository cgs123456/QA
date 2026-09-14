"""sherpa-onnx Provider 单测（task17）：无权重、无 sherpa_onnx、无 numpy 可跑。

手法：向 `sys.modules` 注入最小替身（fake `sherpa_onnx` + fake `numpy`），
只模拟"解码器接受波形并返回 result"的形状，不模拟识别本身。
识别正确性由 `docs/benchmark.md` 的同样本横向对比负责，不由单测负责。

覆盖：
- registry 键一致（provider.REGISTRY_KEY == registry 常量，避免字面量手滑）；
- registry 条目契约（每文件有 size/sha256/urls —— downloader 拒收无 SHA 条目）；
- 权重缺失 → `unavailable`（快速失败，不静默联网）；
- 空段 → `""`（不碰权重）；
- PCM 契约（非 16k / 非 4 字节对齐 → `provider_error`）；
- SenseVoice 标签清洗双路径（result 结构字段 / 内联文本防御层）；
- `decode_streams` 新版入口与 `decode_stream` 旧版入口都兼容；
- 进程内单例（同 key 二次构造不重复加载）。
"""

import asyncio
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asr.provider import ERR_PROVIDER_ERROR, ERR_UNAVAILABLE, ASRError  # noqa: E402
from models import registry  # noqa: E402


# ---- 替身 ----

class _FakeArray:
    def __init__(self, size):
        self.size = size


def _install_fake_numpy(monkeypatch):
    fake = ModuleType("numpy")
    fake.frombuffer = lambda buf, dtype=None: _FakeArray(len(buf) // 4)
    monkeypatch.setitem(sys.modules, "numpy", fake)
    return fake


class _FakeStream:
    def __init__(self, result):
        self._result = result
        self.accepted = []

    def accept_waveform(self, sample_rate, audio):
        self.accepted.append((sample_rate, audio))

    @property
    def result(self):
        return self._result


class _BaseRecognizer:
    """解码器替身公共部分。结果经类属性注入。"""

    _next_result = SimpleNamespace(text="默认", lang="", emotion="", event="")

    def __init__(self, result):
        self._result = result
        self.decode_calls = []

    @classmethod
    def from_sense_voice(cls, **kwargs):
        return cls(cls._next_result)

    @classmethod
    def from_paraformer(cls, **kwargs):
        return cls(cls._next_result)

    def create_stream(self):
        return _FakeStream(self._result)


class _FakeRecognizer(_BaseRecognizer):
    """新版入口（`decode_streams([...])`）。"""

    def decode_streams(self, streams):
        self.decode_calls.append(("decode_streams", len(streams)))


class _OldRecognizer(_BaseRecognizer):
    """旧版入口：只有 `decode_stream(stream)`，无 `decode_streams`。"""

    def decode_stream(self, stream):
        self.decode_calls.append(("decode_stream", 1))


def _RecognizerWith(result, old=False):
    base = _OldRecognizer if old else _FakeRecognizer

    class _R(base):
        pass

    _R._next_result = result
    return _R


def _install_fake_sherpa(monkeypatch, recognizer_cls=_FakeRecognizer):
    fake = ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = recognizer_cls
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    _install_fake_numpy(monkeypatch)
    _install_fake_sherpa(monkeypatch)
    from asr import sherpa_base

    monkeypatch.setattr(sherpa_base, "_MODEL_CACHE", {})
    yield


def _pcm(seconds=1.0, fill=0.1):
    n = int(16000 * seconds)
    return struct.pack(f"<{n}f", *([fill] * n))


# ---- registry 契约 ----

def test_registry_keys_match_provider_constants():
    from asr.paraformer_provider import REGISTRY_KEY as PF_KEY
    from asr.sensevoice_provider import REGISTRY_KEY as SV_KEY

    assert SV_KEY == registry.SENSE_VOICE
    assert PF_KEY == registry.PARAFORMER_ZH


def test_registry_asr_entries_have_sha_urls_size():
    for key in (registry.SENSE_VOICE, registry.PARAFORMER_ZH):
        entry = registry.MODEL_REGISTRY[key]
        assert entry["files"], key
        for name, spec in entry["files"].items():
            assert spec.get("sha256"), f"{key}/{name} 无 SHA（downloader 会拒收）"
            assert spec.get("urls"), f"{key}/{name} 无镜像"
            assert spec.get("size", 0) > 0, f"{key}/{name} 无尺寸"


# ---- 就绪 / 失败语义 ----

def test_missing_weights_is_unavailable_not_crash(tmp_path):
    from asr.sensevoice_provider import SenseVoiceProvider

    p = SenseVoiceProvider(model_dir=str(tmp_path / "empty"))
    assert p.is_ready() is False
    assert len(p.missing_files()) == 2
    with pytest.raises(ASRError) as e:
        asyncio.run(p.transcribe(_pcm(0.5)))
    assert e.value.kind == ERR_UNAVAILABLE


def test_empty_segment_returns_empty_without_weights(tmp_path):
    from asr.paraformer_provider import ParaformerProvider

    p = ParaformerProvider(model_dir=str(tmp_path / "empty"))
    assert asyncio.run(p.transcribe(b"")) == ""


def test_pcm_contract_rejects_bad_rate_and_alignment(tmp_path):
    from asr.sensevoice_provider import SenseVoiceProvider

    p = SenseVoiceProvider(model_dir=str(tmp_path / "empty"))
    with pytest.raises(ASRError) as e:
        asyncio.run(p.transcribe(_pcm(0.1), sample_rate=8000))
    assert e.value.kind == ERR_PROVIDER_ERROR
    with pytest.raises(ASRError) as e:
        asyncio.run(p.transcribe(b"\x00\x01\x02"))
    assert e.value.kind == ERR_PROVIDER_ERROR


# ---- 转写 happy path（替身识别器） ----

def _ready_dir(tmp_path, key):
    d = tmp_path / "model"
    d.mkdir()
    for name in registry.MODEL_REGISTRY[key]["files"]:
        (d / name).write_bytes(b"fake")
    return str(d)


def test_sense_voice_cleans_inline_tags_defense_path(tmp_path, monkeypatch):
    from asr.sensevoice_provider import SenseVoiceProvider

    result = SimpleNamespace(text="<|zh|><|NEUTRAL|><|Speech|><|woitn|>开放时间早上九点")
    _install_fake_sherpa(monkeypatch, _RecognizerWith(result))
    p = SenseVoiceProvider(model_dir=_ready_dir(tmp_path, registry.SENSE_VOICE))
    assert asyncio.run(p.transcribe(_pcm(0.5))) == "开放时间早上九点"
    # 标签快照内容无关（无正文），可进诊断。
    assert p.last_tags.get("lang") == "zh"
    assert "开放时间" not in str(p.last_tags)


def test_sense_voice_tags_from_result_fields(tmp_path, monkeypatch):
    from asr.sensevoice_provider import SenseVoiceProvider

    result = SimpleNamespace(text="干净正文", lang="zh", emotion="NEUTRAL", event="Speech")
    _install_fake_sherpa(monkeypatch, _RecognizerWith(result))
    p = SenseVoiceProvider(model_dir=_ready_dir(tmp_path, registry.SENSE_VOICE))
    assert asyncio.run(p.transcribe(_pcm(0.5))) == "干净正文"
    assert p.last_tags == {"lang": "zh", "emotion": "NEUTRAL", "event": "Speech"}


def test_paraformer_cleans_specials(tmp_path, monkeypatch):
    from asr.paraformer_provider import ParaformerProvider

    result = SimpleNamespace(text="开 放<unk>时间")
    _install_fake_sherpa(monkeypatch, _RecognizerWith(result))
    p = ParaformerProvider(model_dir=_ready_dir(tmp_path, registry.PARAFORMER_ZH))
    assert asyncio.run(p.transcribe(_pcm(0.5))) == "开放时间"


def test_old_decode_stream_entry_compatible(tmp_path, monkeypatch):
    from asr.paraformer_provider import ParaformerProvider

    _install_fake_sherpa(monkeypatch, _RecognizerWith(SimpleNamespace(text="旧入口"), old=True))
    p = ParaformerProvider(model_dir=_ready_dir(tmp_path, registry.PARAFORMER_ZH))
    assert asyncio.run(p.transcribe(_pcm(0.5))) == "旧入口"


def test_singleton_cache_shared_across_instances(tmp_path, monkeypatch):
    from asr.sensevoice_provider import SenseVoiceProvider

    model_dir = _ready_dir(tmp_path, registry.SENSE_VOICE)
    a = SenseVoiceProvider(model_dir=model_dir)
    b = SenseVoiceProvider(model_dir=model_dir)
    asyncio.run(a.transcribe(_pcm(0.2)))
    asyncio.run(b.transcribe(_pcm(0.2)))
    assert a._model is b._model
    assert (a.load_count + b.load_count) == 1


# ---- 切换板（无降级链，纯引用切换） ----

def test_switchboard_select_without_chain_needs_no_weights(tmp_path):
    from asr.catalog import ASRSwitchboard

    sb = ASRSwitchboard("faster-whisper", with_chain=False,
                        model_dir=str(tmp_path / "fw"))
    assert sb.selected == "faster-whisper"
    # 本地 provider 切换只换引用、不加载权重 —— 缺权重也必须能切。
    sb2 = ASRSwitchboard("sensevoice", with_chain=False,
                         model_dir=str(tmp_path / "sv"))
    assert sb2.current().name == "sensevoice"
    desc = sb2.select("paraformer")
    assert sb2.selected == "paraformer"
    assert desc["spec"]["model"] == "paraformer-zh"
    assert sb2.current().name == "paraformer"
