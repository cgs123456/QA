"""ASR 降级链（task15 第 3 项）：本地主 → 同类本地备选 → 云端 REST → 纯 VAD + 手动输入。

## 链路

| 级 | 语义 | 进入条件 |
|---|---|---|
| 0 | 本地主（faster-whisper base） | 恒在 |
| 1 | 同类本地备选（如另一档 faster-whisper 权重） | 调用方配了才在 |
| 2 | 云端 REST（OpenAI 兼容 `/audio/transcriptions`） | **有 API Key** 才在 |
| 3 | 纯 VAD + 手动输入 | 恒在（终级兜底） |

第 3 级不是 provider：全部 provider 失败时抛 `ASRError(manual_input_required)`，
前端据此切到手动输入（VAD 已给出"这段有人在说"的事实，缺的只是文本）。
若 `manual_input=False`，则抛 `all_providers_failed`（契约里两种 kind 都在）。

**OpenAI Realtime WS 已裁定为 Phase 2**，本轮不做（理由与替代方案见 docs/PROGRESS.md）。

## 超时：按段时长派生，不用固定值

固定超时会在大段上必然误判降级：15s 的段在 CPU int8 上转写可能就要 10s+，
若设 10s 固定超时，段越长越会"稳定失败"并降级到云端——把一个**容量问题**
伪装成**故障**。故超时取 `max(MIN_TIMEOUT_S, factor × 段时长)`（factor 默认 3.0），
即"转写耗时超过段本身时长的 3 倍才算异常"。

代价（记录在案）：单次调用的最坏耗时 = 级数 × 该超时；链越长，最坏时延越大。
故链只保留必要层级，且每级独立计时（前一级的失败不消耗后一级的预算）。

## 降级事件（R14）

每降一级记一条**内容无关**事件：级号 + provider 名 + 错误 kind（+ path 标签）。
不含音频、不含文本、不含权重路径。经 `on_degrade` 回调外发（生产接日志/诊断），
默认落到本模块 logger 的 WARNING。
"""

import asyncio
import logging
from dataclasses import dataclass

from .provider import (
    ERR_ALL_FAILED,
    ERR_MANUAL_INPUT,
    ERR_PROVIDER_ERROR,
    ERR_PROVIDER_TIMEOUT,
    ASRError,
    segment_seconds,
)

_log = logging.getLogger(__name__)

# 超时按段时长派生（见模块 docstring）。两者均为**初值**，W4 用样本集调。
DEGRADE_TIMEOUT_FACTOR = 3.0
MIN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ASRResult:
    """一次转写的结果载体：文本 + 实际出力的 provider + 失败过的级。"""

    text: str
    provider: str
    attempts: tuple = ()  # ((provider_name, error_kind), ...) 按尝试顺序

    def degrade_summary(self) -> str:
        """内容无关的降级摘要，可直接进下行/日志。"""
        return ";".join(f"{n}:{k}" for n, k in self.attempts)


class FallbackASRProvider:
    """按序尝试各级 provider，全失败则转手动输入。"""

    name = "fallback"

    def __init__(self, levels: list, on_degrade=None,
                 timeout_factor: float = DEGRADE_TIMEOUT_FACTOR,
                 min_timeout_s: float = MIN_TIMEOUT_S,
                 manual_input: bool = True):
        self.levels = [p for p in levels if p is not None]
        self.on_degrade = on_degrade
        self.timeout_factor = timeout_factor
        self.min_timeout_s = min_timeout_s
        self.manual_input = manual_input

    # ---- 超时 ----

    def timeout_for(self, pcm_f32: bytes, sample_rate: int = 16000) -> float:
        """本段每级的超时预算（秒）。按样本计数派生，与挂钟无关。"""
        return max(self.min_timeout_s, self.timeout_factor * segment_seconds(pcm_f32, sample_rate))

    # ---- 转写 ----

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        """Protocol 兼容入口（只返回文本）。需要 provider 名请用 transcribe_with_detail。"""
        return (await self.transcribe_with_detail(pcm_f32, sample_rate, path)).text

    async def transcribe_with_detail(self, pcm_f32: bytes, sample_rate: int = 16000,
                                     path: str | None = None) -> ASRResult:
        """逐级尝试；返回 ASRResult。结果经返回值传递，**不落实例状态**——
        同一条链会被多段并发复用（sidecar 待转写 ≤8），共享可变状态必然串味。"""
        if not self.levels:
            raise ASRError(ERR_ALL_FAILED, "降级链为空")
        timeout = self.timeout_for(pcm_f32, sample_rate)
        attempts: list = []
        for level, provider in enumerate(self.levels):
            try:
                text = await asyncio.wait_for(
                    provider.transcribe(pcm_f32, sample_rate, path), timeout)
                return ASRResult(text=text, provider=getattr(provider, "name", "?"),
                                 attempts=tuple(attempts))
            except asyncio.TimeoutError:
                attempts.append((getattr(provider, "name", "?"), ERR_PROVIDER_TIMEOUT))
                self._degrade(level, provider, ERR_PROVIDER_TIMEOUT, path, timeout)
            except ASRError as e:
                attempts.append((getattr(provider, "name", "?"), e.kind))
                self._degrade(level, provider, e.kind, path, timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 未预期异常也要降级而不是穿透：种类名进事件，栈由调用方按需打印。
                attempts.append((getattr(provider, "name", "?"), ERR_PROVIDER_ERROR))
                self._degrade(level, provider, ERR_PROVIDER_ERROR, path, timeout, e)
        if self.manual_input:
            err = ASRError(ERR_MANUAL_INPUT,
                           f"降级链 {len(self.levels)} 级全失败，转手动输入")
        else:
            err = ASRError(ERR_ALL_FAILED, f"降级链 {len(self.levels)} 级全失败")
        # 把逐级失败摘要挂到异常上，调用方无需另设通道即可写进下行 asr_error。
        err.attempts = tuple(attempts)
        raise err

    # ---- 降级事件 ----

    def _degrade(self, level: int, provider, kind: str, path, timeout: float,
                 exc: Exception | None = None) -> None:
        event = {
            "level": level,
            "provider": getattr(provider, "name", "?"),
            "error": kind,
            "path": path,
            "timeout_s": round(timeout, 3),
        }
        # R14：只有级号/provider 名/错误种类/path 标签——无音频、无文本、无路径。
        _log.warning("asr degrade %s", event)
        if self.on_degrade is not None:
            self.on_degrade(event)
        if exc is not None:
            _log.debug("asr degrade cause=%s", type(exc).__name__)

    # ---- 诊断 ----

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "levels": [getattr(p, "name", "?") for p in self.levels],
            "manual_input": self.manual_input,
            "timeout_factor": self.timeout_factor,
            "min_timeout_s": self.min_timeout_s,
        }


def build_default_chain(*, primary=None, local_backup=None, cloud_api_key=None,
                        cloud_base_url=None, cloud_model=None, manual_input: bool = True,
                        on_degrade=None, timeout_factor: float = DEGRADE_TIMEOUT_FACTOR):
    """组装默认降级链：本地主 → （配了才有）同类本地备选 → （有 key 才有）云端 REST。

    `primary=None` 时惰性构造 `FasterWhisperProvider()`——构造本身不加载权重，
    权重缺失会在第一次转写时以 `unavailable` 降级，而不是让 sidecar 起不来。
    """
    from .cloud_rest import CloudRestProvider
    from .faster_whisper_provider import FasterWhisperProvider

    levels = [primary if primary is not None else FasterWhisperProvider()]
    if local_backup is not None:
        levels.append(local_backup)
    if cloud_api_key:
        levels.append(CloudRestProvider(api_key=cloud_api_key, base_url=cloud_base_url,
                                        model=cloud_model))
    return FallbackASRProvider(levels, on_degrade=on_degrade,
                               timeout_factor=timeout_factor, manual_input=manual_input)


__all__ = ["ASRResult", "DEGRADE_TIMEOUT_FACTOR", "FallbackASRProvider", "MIN_TIMEOUT_S",
           "build_default_chain"]
