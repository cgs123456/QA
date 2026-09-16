"""LLM 降级链（P8 可用性收尾）：首选→备选按序尝试。

## 语义

- `FallbackLLMProvider(levels)` 包装 N 个 provider（实例或 `(name, thunk)`
  懒构造对），`generate()` 按序尝试：某级在**首 chunk 之前**失败
  （超时/限流/401 等建连期错误——任务点名的三类全在此）→ 记录
  `(name, kind)` 后换下一级；某级成功则完整透传其流并结束。
- **首 chunk 之后**的失败不再换级：已下行的 chunk 收不回，混流比静默截断
  更糟——此时沿用既有 error 路径（`last_provider` 记下行到一半的那级）。
- 全灭时抛**最后一级**的 `ProviderError`（kind 即其 kind），并把完整
  `attempts` 挂到异常上（复用 ASR `err.attempts` 模式，调用方无需另设通道）。
- Fail-Closed **不参与降级**：拒答分支在 `answer_stream` 里先于 LLM 返回，
  链只活在 maybe 档里——拒绝就是拒绝，不换模型重试（单测锁定）。

## 防雪崩（坑位）：连续失败冷却

- 进程级断路器 `_CIRCUIT`：某 provider 连续失败达 `MAX_FAILURES`（默认 3，
  初值）→ 冷静 `COOLDOWN_S`（默认 60s，初值），期间该级被跳过并记
  `(name, "cooled")`（内容无关，进 degraded  trail，不进诊断计数——
  跳过是冷却的后果不是新的失败）。
- 成功即清零该级计数。链内**永不 sleep**：Groq 限流 kind 触发的是立即换级
  （快速切换），而不是退避等待——退避只属于单 provider 内部
  （如 embedding 的 `_post_batch`），链层不叠加等待。
- 时钟经模块属性 `_monotonic` 注入，单测可 monkeypatch（不 sleep、不 flaky）。

## 降级事件（R14）

每级真实失败记一条**内容无关**事件 `{level, provider, error}`：
无 prompt、无 context、无 key。经 `on_degrade` 回调外发（生产接
`diagnostics.degrade.llm_on_degrade`，默认落本模块 WARNING 日志）。
"""

import asyncio
import logging
import time

from .provider import ProviderError

_log = logging.getLogger(__name__)

#: 单次问答最多尝试的级数（含首选）：每级最坏 30s+，无界链即无界请求时长。
MAX_LEVELS = 5
#: 连续失败几次进冷却（初值，W4 用样本调）。
MAX_FAILURES = 3
#: 冷却时长秒（初值）。
COOLDOWN_S = 60.0

#: 断路器表：name → {"fails": int, "cooled_until": float}（monotonic 时钟）。
_CIRCUIT: dict = {}


def _monotonic() -> float:
    """时钟间接层（单测 monkeypatch 本属性注入假时钟）。"""
    return time.monotonic()


def reset_circuits() -> None:
    """清断路器表（测试用；生产路径只增不减、按过期自然恢复）。"""
    _CIRCUIT.clear()


def _cooled(name: str, now: float) -> bool:
    st = _CIRCUIT.get(name)
    return st is not None and now < st["cooled_until"]


def _note_failure(name: str, max_failures: int, cooldown_s: float) -> None:
    st = _CIRCUIT.setdefault(name, {"fails": 0, "cooled_until": 0.0})
    st["fails"] += 1
    if st["fails"] >= max_failures:
        st["cooled_until"] = _monotonic() + cooldown_s


def _note_success(name: str) -> None:
    st = _CIRCUIT.get(name)
    if st is not None:
        st["fails"] = 0


class FallbackLLMProvider:
    """首选→备选 LLM 链。流式透传，无缓冲（chunk 即到即发）。"""

    name = "fallback"

    def __init__(self, levels: list, on_degrade=None,
                 max_failures: int = MAX_FAILURES,
                 cooldown_s: float = COOLDOWN_S):
        norm = []
        for lv in levels or []:
            if isinstance(lv, tuple):
                lv_name, thunk = lv
            else:
                lv_name, thunk = getattr(lv, "name", "?"), (lambda p=lv: p)
            norm.append((str(lv_name), thunk))
        if not norm:
            raise ValueError("LLM 降级链为空")
        if len(norm) > MAX_LEVELS:
            raise ValueError(f"LLM 降级链过长（{len(norm)} > {MAX_LEVELS}）")
        self._levels = norm
        self.on_degrade = on_degrade
        self.max_failures = max_failures
        self.cooldown_s = cooldown_s
        # 最近一次 generate() 的决策轨迹（answer_stream 读它们组装结果）：
        self.attempts: tuple = ()
        self.last_provider = None
        self.tried = 0

    @property
    def level_names(self) -> list:
        return [n for n, _ in self._levels]

    def _degrade(self, level: int, name: str, kind: str) -> None:
        event = {"level": level, "provider": name, "error": kind}
        # R14：级号/provider 名/错误种类——无 prompt、无 context、无 key。
        _log.warning("llm degrade %s", event)
        if self.on_degrade is not None:
            self.on_degrade(event)

    async def generate(self, system: str, question: str, contexts: list):
        """按序尝试各级；成功则透传其完整流，首 chunk 后失败即按既有语义上抛。"""
        self.attempts = ()
        self.last_provider = None
        self.tried = 0
        now = _monotonic()
        last_error = None
        for level, (name, thunk) in enumerate(self._levels):
            if _cooled(name, now):
                self.attempts = (*self.attempts, (name, "cooled"))
                continue
            try:
                provider = thunk()  # 备选懒构造：坏备选只记 config，不毙掉整单
            except Exception as e:  # noqa: BLE001 — 构造期异常种类名进事件
                self.attempts = (*self.attempts, (name, "config"))
                self._degrade(level, name, "config")
                last_error = e
                continue
            self.tried += 1
            try:
                first = True
                async for chunk in provider.generate(system, question, contexts):
                    if first:
                        first = False
                        self.last_provider = name
                    yield chunk
                self.last_provider = name
                _note_success(name)
                return
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except ProviderError as e:
                if not first:
                    self.last_provider = name
                    raise  # 首 chunk 已下行：混流不如走 error 路径
                self.attempts = (*self.attempts, (name, e.kind))
                _note_failure(name, self.max_failures, self.cooldown_s)
                self._degrade(level, name, e.kind)
                last_error = e
            except Exception as e:  # noqa: BLE001 — 未预期异常也降级，不穿透
                if not first:
                    self.last_provider = name
                    raise ProviderError("error", f"{name} 异常：{type(e).__name__}")
                self.attempts = (*self.attempts, (name, "error"))
                _note_failure(name, self.max_failures, self.cooldown_s)
                self._degrade(level, name, "error")
                last_error = e
        # 全灭：抛最后一级的错（kind 即其 kind），attempts 全 trail 挂异常上。
        if isinstance(last_error, ProviderError):
            last_error.attempts = tuple(self.attempts)
            raise last_error
        err = ProviderError("error", "LLM 降级链全灭")
        err.attempts = tuple(self.attempts)
        raise err


def build_chain(primary_name: str, fallbacks=(), *, on_degrade=None,
                max_failures: int = MAX_FAILURES,
                cooldown_s: float = COOLDOWN_S) -> FallbackLLMProvider:
    """组装问答链：首选严格构造（未知/缺 key 立即抛，保持今日行为），
    备选懒构造（坏备选运行时记 config 跳过，不毙整单）。

    重名备选静默去重（与首选/更早备选同名者无意义）；空串备选丢弃。
    """
    from .provider import get_provider

    primary = (primary_name or "").strip().lower() or "ollama"
    if not isinstance(fallbacks, (list, tuple)):
        raise ValueError("fallbacks 须为 provider 名列表")
    seen = {primary}
    # 严格：抛 ValueError 即今日之 error-config 路径（_run_task 转 error 结果）。
    levels = [get_provider(primary)]
    for raw in fallbacks:
        name = (raw or "").strip().lower() if isinstance(raw, str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        # 闭包捕获：默认参数绑定当前 name（循环变量陷阱防范）。
        levels.append((name, lambda n=name: get_provider(n)))
    return FallbackLLMProvider(levels, on_degrade=on_degrade,
                               max_failures=max_failures, cooldown_s=cooldown_s)


__all__ = ["COOLDOWN_S", "MAX_FAILURES", "MAX_LEVELS", "FallbackLLMProvider",
           "build_chain", "reset_circuits"]
