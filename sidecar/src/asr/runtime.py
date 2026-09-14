"""ASR provider 的进程级运行时状态（F6.2「选择有状态」那一层的落地点）。

`catalog.py` 提供目录与工厂，本身无状态；本模块持有**唯一**的
`ASRSwitchboard` 实例，供两处共享：

- `routers/audio.py` —— 每段转写前读一次 `current_provider()`（切换对下一段生效）；
- `routers/settings.py` —— 设置页调用 `select()` 切换。

单独成模块是为了避免设置路由反向依赖音频路由：两边都只依赖本模块，
不产生 `routers → routers` 的耦合。

**懒构造**：首次 `get_switchboard()` 才建实例。构造 provider 不加载权重
（权重在首次转写时懒加载），故进程启动不受影响，切换也是微秒级。

**默认仍是 `DEFAULT_PROVIDER`（faster-whisper）**：task15 行为不变。
哪个 provider 更合适由 `docs/benchmark.md` 的同样本横向对比决定，不靠印象改默认值。
"""

from .catalog import DEFAULT_PROVIDER, ASRSwitchboard

_SWITCHBOARD = None


def get_switchboard() -> ASRSwitchboard:
    """进程级实例（懒构造，带默认降级链）。"""
    global _SWITCHBOARD
    if _SWITCHBOARD is None:
        _SWITCHBOARD = ASRSwitchboard(DEFAULT_PROVIDER)
    return _SWITCHBOARD


def set_switchboard(sb) -> None:
    """替换进程级实例（测试用）。传 `None` 即回到「尚未构造」状态。"""
    global _SWITCHBOARD
    _SWITCHBOARD = sb


def current_provider():
    """当前生效的 provider（含降级链）。

    构造失败（如切到云端却没 key）会抛 `ASRError` —— 调用方须捕获并转为
    内容无关的 `asr_error`，**不得**让它穿透成连接级异常。
    """
    return get_switchboard().current()


__all__ = ["current_provider", "get_switchboard", "set_switchboard"]
