"""三链降级事件统一计数（P8 可用性收尾，R14）。

ASR / LLM / embedding 各自的降级语义不同（段级转写 / 问答流 / 重建任务），
但观测形状统一为**纯计数**：链名 + provider 名 + 错误 kind —— 无文本、
无向量、无 key。诊断面板只读快照，不存明细。

写入点（均为“真实失败”才记，冷却跳过不记，见各模块注释）：
- ASR：`asr/runtime.py` 给进程单例切换板挂 `asr_on_degrade` 适配器；
- LLM：`routers/qa.py` 给降级链挂 `llm_on_degrade` 适配器；
- embedding：重建任务失败（`routers/embedding.py`）与问答时向量故障降级
  （`routers/qa.py` 的 embed 兜底）。

进程内存语义（与 secrets/ASR 切换板一致）：sidecar 重启即清零。
线程安全：`threading.Lock` 短临界区，内无 await，可在任意上下文调用。
"""

import threading

_CHAINS = ("asr", "llm", "embedding")

_lock = threading.Lock()
_counters: dict = {c: {"total": 0, "by_kind": {}, "by_provider": {}} for c in _CHAINS}


def record(chain: str, provider: str, kind: str) -> None:
    """记一次降级事件。参数只允许名字/种类字符串（调用方负责内容无关）。"""
    if chain not in _CHAINS:
        raise ValueError(f"未知降级链：{chain}")
    provider = (provider or "?").strip() or "?"
    kind = (kind or "?").strip() or "?"
    with _lock:
        slot = _counters[chain]
        slot["total"] += 1
        slot["by_kind"][kind] = slot["by_kind"].get(kind, 0) + 1
        slot["by_provider"][provider] = slot["by_provider"].get(provider, 0) + 1


def snapshot() -> dict:
    """诊断端点用的深拷贝快照（调用方改不动内部状态）。"""
    with _lock:
        return {
            c: {"total": v["total"],
                "by_kind": dict(v["by_kind"]),
                "by_provider": dict(v["by_provider"])}
            for c, v in _counters.items()
        }


def reset() -> None:
    """清零（测试用；生产路径计数只增不减）."""
    with _lock:
        for c in _CHAINS:
            _counters[c] = {"total": 0, "by_kind": {}, "by_provider": {}}


def asr_on_degrade(event: dict) -> None:
    """ASR `on_degrade` 回调适配器：`{provider, error, ...}` → 计数。"""
    record("asr", str(event.get("provider", "?")), str(event.get("error", "?")))


def llm_on_degrade(event: dict) -> None:
    """LLM `on_degrade` 回调适配器：`{provider, error, ...}` → 计数。"""
    record("llm", str(event.get("provider", "?")), str(event.get("error", "?")))


__all__ = ["asr_on_degrade", "llm_on_degrade", "record", "reset", "snapshot"]
