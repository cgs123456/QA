"""ASR Provider 抽象（Phase 1b）：整段转写接口。

- 输入：单声道 float32 LE PCM（16kHz，WS 上行已拼好的语音段），不做 VAD（R11）。
- 真实 Provider（faster-whisper 等）为 1b-2；此处仅抽象 + 测试替身。
- R14：实现体不得打印/记录转写文本（单测以 capsys 断言）。
"""

from typing import Protocol


class ASRProvider(Protocol):
    name: str

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000) -> str:
        """整段转写，返回文本。失败抛 ASRError（由调用方转为 asr_error 下行）。"""
        ...


class ASRError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


class StubASRProvider:
    """测试替身：固定文本 + 调用记录 + 可选故障注入。"""

    name = "stub"

    def __init__(self, text: str = "测试转写", fail_with: Exception | None = None,
                 delay_s: float = 0.0):
        self.text = text
        self.fail_with = fail_with
        self.delay_s = delay_s
        self.calls: list = []

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000) -> str:
        import asyncio as _asyncio

        self.calls.append((len(pcm_f32), sample_rate))
        if self.delay_s:
            await _asyncio.sleep(self.delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        return self.text
