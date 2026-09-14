"""ASR Provider 抽象（Phase 1b）：整段转写接口。

- 输入：单声道 float32 LE PCM（16kHz，WS 上行已拼好的语音段），不做 VAD（R11）。
- `path` 为来源路标签（`loopback`/`mic`），只用于诊断与降级事件，不参与转写。
- 真实 Provider 见 `faster_whisper_provider.py`（本地）与 `cloud_rest.py`（云端）；
  `fallback.py` 把多级 provider 串成降级链。
- R14：实现体不得打印/记录转写文本（单测以 capsys 断言）。
"""

from typing import Protocol

# 下行 `asr_error` 的 `error` 取值（与 docs/api-contract.md 一致）。
ERR_NO_PROVIDER = "no_provider"
ERR_PROVIDER_TIMEOUT = "provider_timeout"
ERR_PROVIDER_ERROR = "provider_error"
ERR_ALL_FAILED = "all_providers_failed"
ERR_UNAVAILABLE = "unavailable"
ERR_MANUAL_INPUT = "manual_input_required"


class ASRProvider(Protocol):
    name: str

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        """整段转写，返回文本。失败抛 ASRError（由调用方转为 asr_error 下行）。"""
        ...


class ASRError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        # 降级链逐级失败时由 fallback 填充：((provider_name, error_kind), ...)。
        # 内容无关，可直接进下行 `asr_error`。
        self.attempts: tuple = ()


def segment_seconds(pcm_f32: bytes, sample_rate: int = 16000) -> float:
    """段时长（秒）。**由样本计数派生，不用挂钟**（与 R10 的 ts_ms 同源纪律）。"""
    return len(pcm_f32) / (4 * sample_rate) if sample_rate else 0.0


class StubASRProvider:
    """测试替身：固定文本 + 调用记录 + 可选故障注入。"""

    name = "stub"

    def __init__(self, text: str = "测试转写", fail_with: Exception | None = None,
                 delay_s: float = 0.0, name: str | None = None):
        self.text = text
        self.fail_with = fail_with
        self.delay_s = delay_s
        if name:
            self.name = name
        self.calls: list = []
        # 与 `calls` 平行记录来源路（既有断言按 (size, rate) 二元组解包，
        # 故 path 单列，避免无谓改动既有用例）。
        self.paths: list = []

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        import asyncio as _asyncio

        self.calls.append((len(pcm_f32), sample_rate))
        self.paths.append(path)
        if self.delay_s:
            await _asyncio.sleep(self.delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        return self.text
