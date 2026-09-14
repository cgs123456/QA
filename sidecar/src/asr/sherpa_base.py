"""sherpa-onnx 离线识别器共用基类。

SenseVoice 与 Paraformer 的差异只有三处：**模型文件、构造参数、文本清洗**。
其余全部相同：进程内单例、懒加载、PCM 契约校验、CPU 卸载、错误映射、就绪探测。

把相同的那部分抽出来，是因为它是**契约**：段输入统一为单声道 float32 + 16kHz
（PRD §3.5「不同 Provider 的段输入格式在 Protocol 中统一为 float32+sr，
由各自 Provider 自行转换」）。这条契约若在两个文件里各写一遍，
迟早有一份先腐坏 —— 而腐坏的表现是「某个 provider 悄悄转出乱码」。

R14：本模块零打印；转写文本只作为返回值向上走，异常只带类型名。
"""

import asyncio
import threading
from pathlib import Path

from .provider import ERR_PROVIDER_ERROR, ERR_UNAVAILABLE, ASRError

# 进程内单例：key = (registry_key, num_threads, provider, model_dir)
_MODEL_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

SAMPLE_RATE = 16000


class SherpaOfflineProvider:
    """sherpa-onnx `OfflineRecognizer` 的通用外壳。子类只填三处差异。"""

    name = "sherpa-onnx"
    #: registry 里的模型键，子类必须覆盖。
    registry_key = ""

    def __init__(self, model_dir=None, num_threads: int = 1, provider: str = "cpu"):
        from models import registry

        self.num_threads = num_threads
        self.provider = provider
        self.model_dir = Path(model_dir) if model_dir else Path(
            registry.default_model_dir(self.registry_key)
        )
        self._model = None
        self.load_count = 0

    # ---- 就绪性（只查文件，不加载、不联网） ----

    def _spec(self) -> dict:
        from models import registry

        return registry.MODEL_REGISTRY[self.registry_key]

    def missing_files(self) -> list:
        return [n for n in self._spec()["files"] if not (self.model_dir / n).is_file()]

    def is_ready(self) -> bool:
        return not self.missing_files()

    def _model_path(self, filename: str) -> str:
        return str(self.model_dir / filename)

    # ---- 加载（进程内单例） ----

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        missing = self.missing_files()
        if missing:
            raise ASRError(
                ERR_UNAVAILABLE,
                f"{self.name} 权重未就位（缺 {len(missing)} 个文件），先经 /model/download 获取",
            )
        key = (self.registry_key, self.num_threads, self.provider, str(self.model_dir))
        with _CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is not None:
                self._model = cached
                return cached
            model = self._build()
            _MODEL_CACHE[key] = model
            self._model = model
            self.load_count += 1
            return model

    def _build(self):
        """构造识别器。子类实现；导入失败必须映射为 `unavailable`。"""
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError as e:
            raise ASRError(ERR_UNAVAILABLE, f"sherpa-onnx 未安装（{type(e).__name__}）") from e
        try:
            return self._make()
        except ASRError:
            raise
        except Exception as e:
            # 只带类型名：权重路径可能含用户名，不进异常文本。
            raise ASRError(ERR_UNAVAILABLE, f"模型加载失败（{type(e).__name__}）") from e

    def _make(self):
        raise NotImplementedError

    def warmup(self) -> None:
        self._ensure_model()

    # ---- 转写 ----

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = SAMPLE_RATE,
                         path: str | None = None) -> str:
        # CPU 密集 → 线程卸载；事件循环在转写期间仍可收帧（R13 不阻塞）。
        return await asyncio.to_thread(self._transcribe_sync, pcm_f32, sample_rate)

    def _transcribe_sync(self, pcm_f32: bytes, sample_rate: int) -> str:
        import numpy as np

        if sample_rate != SAMPLE_RATE:
            raise ASRError(ERR_PROVIDER_ERROR, f"仅支持 16kHz（收到 {sample_rate}）")
        if len(pcm_f32) % 4 != 0:
            raise ASRError(ERR_PROVIDER_ERROR, "PCM 长度非 4 字节对齐（非 float32 LE）")
        audio = np.frombuffer(pcm_f32, dtype="<f4")
        if audio.size == 0:
            return ""
        model = self._ensure_model()
        try:
            stream = model.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            self._decode(model, stream)
            return self.clean_result(stream.result)
        except ASRError:
            raise
        except Exception as e:
            raise ASRError(ERR_PROVIDER_ERROR, f"转写失败（{type(e).__name__}）") from e

    @staticmethod
    def _decode(model, stream) -> None:
        """兼容两个版本的解码入口（新版为 `decode_streams([...])`）。"""
        decode_streams = getattr(model, "decode_streams", None)
        if decode_streams is not None:
            decode_streams([stream])
        else:
            model.decode_stream(stream)

    def clean_result(self, result) -> str:
        """识别结果 → 可上屏文本。

        子类若需要读 `result` 的结构化字段（如 SenseVoice 的 `lang`/`emotion`/`event`），
        覆盖本方法；只改文本的覆盖 `clean` 即可。
        """
        return self.clean(getattr(result, "text", "") or "")

    def clean(self, text: str) -> str:
        """子类覆盖：把识别器原始输出洗成可上屏文本。"""
        return (text or "").strip()

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self.registry_key,
            "num_threads": self.num_threads,
            "ready": self.is_ready(),
        }


__all__ = ["SAMPLE_RATE", "SherpaOfflineProvider"]
