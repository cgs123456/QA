"""faster-whisper 本地 Provider（base / int8 / CPU）：整段转写、语言自动。

落地要点（task15）：

- **整段转写**：本 provider 不做 VAD（R11：VAD 只在 Rust 侧），段由端点状态机切好，
  故 `vad_filter=False` —— 二次 VAD 会与本项目的端点判定打架。
- **语言自动**：`language=None` 交给模型自判（中英混说场景不预设）。
- **进程内单例**：首次加载实测 ~2–3s（int8 量化 + CTranslate2 初始化），
  **绝不每段重载**——`_MODEL_CACHE` 按 (模型, 设备, 量化, 目录) 缓存，
  同进程内所有实例共享同一份权重。这是本模块存在的首要理由。
- **本地优先**：权重只从 registry 目录加载（Task 10 下载器按 SHA256 校验落盘）。
  目录不完整即 `ASRError(unavailable)` 快速失败，**不静默联网**；
  显式传 `allow_download=True` 才允许 faster-whisper 自行下载。
- **不阻塞事件循环**：CPU 密集，`asyncio.to_thread` 卸载到线程。
- **R14**：本模块零打印；转写文本只作为返回值向上走，异常只带类型名。
"""

import asyncio
import threading
from pathlib import Path

from .provider import ERR_PROVIDER_ERROR, ERR_UNAVAILABLE, ASRError

DEFAULT_MODEL = "base"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"

# 进程内单例：key = (model, device, compute_type, model_dir)
_MODEL_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _registry_dir() -> Path:
    from models import registry

    return Path(registry.default_model_dir(registry.FASTER_WHISPER_BASE))


def _missing_files(model_dir: Path) -> list:
    from models import registry

    entry = registry.MODEL_REGISTRY[registry.FASTER_WHISPER_BASE]
    return [n for n in entry["files"] if not (model_dir / n).is_file()]


class FasterWhisperProvider:
    """本地 faster-whisper。整段进、文本出；失败抛 `ASRError`。"""

    name = "faster-whisper"

    def __init__(self, model: str = DEFAULT_MODEL, model_dir=None,
                 device: str = DEFAULT_DEVICE, compute_type: str = DEFAULT_COMPUTE_TYPE,
                 language: str | None = None, beam_size: int = 1,
                 allow_download: bool = False, cpu_threads: int = 0):
        self.model = model
        self.model_dir = Path(model_dir) if model_dir else _registry_dir()
        self.device = device
        self.compute_type = compute_type
        # None = 语言自动（task15 要求）。
        self.language = language
        self.beam_size = beam_size
        self.allow_download = allow_download
        self.cpu_threads = cpu_threads
        self._model = None
        self.load_count = 0

    # ---- 就绪性（不触发加载，供 /diagnostics 与降级链预检） ----

    def is_ready(self) -> bool:
        """权重是否已就位（只查文件，不加载、不联网）。"""
        return not _missing_files(self.model_dir)

    def missing_files(self) -> list:
        return _missing_files(self.model_dir)

    # ---- 加载 ----

    def _ensure_model(self):
        """返回进程内单例模型；首次调用才加载（2–3s），之后 O(1)。"""
        if self._model is not None:
            return self._model
        missing = self.missing_files()
        if missing and not self.allow_download:
            raise ASRError(
                ERR_UNAVAILABLE,
                f"faster-whisper 权重未就位（缺 {len(missing)} 个文件），"
                "先经 /model/download 获取",
            )
        key = (self.model, self.device, self.compute_type, str(self.model_dir))
        with _CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is not None:
                self._model = cached
                return cached
            model = self._load_uncached()
            _MODEL_CACHE[key] = model
            self._model = model
            self.load_count += 1
            return model

    def _load_uncached(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # 依赖未装：快速失败，不伪装成"识别为空"
            raise ASRError(ERR_UNAVAILABLE, f"faster-whisper 未安装（{type(e).__name__}）") from e
        source = str(self.model_dir) if self.is_ready() else self.model
        try:
            return WhisperModel(
                source,
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(self.model_dir.parent),
                cpu_threads=self.cpu_threads,
            )
        except Exception as e:
            # 只带类型名：权重路径/参数可能含用户名，不进异常文本。
            raise ASRError(ERR_UNAVAILABLE, f"模型加载失败（{type(e).__name__}）") from e

    def warmup(self) -> None:
        """显式预热（sidecar 启动后台预热用），失败抛 ASRError。"""
        self._ensure_model()

    # ---- 转写 ----

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        # CPU 密集 → 线程卸载；事件循环在转写期间仍可收帧（R13 不阻塞）。
        return await asyncio.to_thread(self._transcribe_sync, pcm_f32, sample_rate)

    def _transcribe_sync(self, pcm_f32: bytes, sample_rate: int) -> str:
        import numpy as np

        if sample_rate != 16000:
            raise ASRError(ERR_PROVIDER_ERROR, f"仅支持 16kHz（收到 {sample_rate}）")
        if len(pcm_f32) % 4 != 0:
            raise ASRError(ERR_PROVIDER_ERROR, "PCM 长度非 4 字节对齐（非 float32 LE）")
        audio = np.frombuffer(pcm_f32, dtype="<f4")
        if audio.size == 0:
            return ""
        model = self._ensure_model()
        try:
            segments, _info = model.transcribe(
                audio,
                language=self.language,          # None = 自动
                beam_size=self.beam_size,
                vad_filter=False,                # R11：VAD 已在 Rust 侧完成
                condition_on_previous_text=False,  # 整段独立，不吃上一段上下文
                temperature=0.0,
            )
            return "".join(s.text for s in segments).strip()
        except Exception as e:
            raise ASRError(ERR_PROVIDER_ERROR, f"转写失败（{type(e).__name__}）") from e

    def describe(self) -> dict:
        """内容无关自述（诊断/降级事件用）。"""
        return {
            "provider": self.name,
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language or "auto",
            "ready": self.is_ready(),
        }


__all__ = ["DEFAULT_COMPUTE_TYPE", "DEFAULT_DEVICE", "DEFAULT_MODEL",
           "FasterWhisperProvider"]
