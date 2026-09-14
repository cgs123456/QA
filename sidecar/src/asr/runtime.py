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

**低延迟模式（task18）**：同模块另持一个进程级布尔开关。默认关闭 ——
关闭时 `routers/audio.py` 的段处理与 task15 **逐字一致**（回归单测锁定）。
开启需要 CUDA（`cuda_available()` 实测），无 GPU 的机器上 `set_low_latency(True)`
直接抛 `unavailable`，不给“开了却跑不动”的半吊子状态。
"""

from .catalog import DEFAULT_PROVIDER, ASRSwitchboard
from .provider import ERR_UNAVAILABLE, ASRError

_SWITCHBOARD = None

#: 低延迟模式（流式 partial）：默认关。段内中途改值只影响**下一段**
#:（audio.py 在 segment_start 时快照一次，与 provider 切换同语义）。
_LOW_LATENCY = False

#: CUDA 探测缓存：硬件配置进程内不变，探一次即可（省得每次 import torch）。
_CUDA_PROBE: bool | None = None


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


def cuda_available() -> bool:
    """本机是否有可用 CUDA（低延迟模式的开启前提）。

    探测顺序：onnxruntime（本项目硬依赖，import 便宜）→ torch（懒 import，
    首次约 1s）。任一报可用即算可用；全灭/未装即不可用。结果进程内缓存。
    只回答“能不能开”，不加载权重、不建会话。
    """
    global _CUDA_PROBE
    if _CUDA_PROBE is not None:
        return _CUDA_PROBE
    found = False
    try:
        import onnxruntime as _ort

        if "CUDAExecutionProvider" in _ort.get_available_providers():
            found = True
    except Exception:
        pass
    if not found:
        try:
            import torch as _torch

            if _torch.cuda.is_available():
                found = True
        except Exception:
            pass
    _CUDA_PROBE = found
    return found


def _reset_cuda_probe() -> None:
    """清探测缓存（测试用；生产路径硬件不变，不调它）。"""
    global _CUDA_PROBE
    _CUDA_PROBE = None


def is_low_latency() -> bool:
    """低延迟模式是否开启（audio.py 每段快照一次）。"""
    return _LOW_LATENCY


def set_low_latency(enabled: bool) -> dict:
    """开/关低延迟模式，返回 `describe_latency()` 快照。

    开启（True）要求 CUDA 就绪，否则抛 `ASRError(unavailable)` —— 调用方
    转为 409（settings 端点形状与 provider 切换一致）。关闭永远允许。
    """
    global _LOW_LATENCY
    if enabled and not cuda_available():
        raise ASRError(ERR_UNAVAILABLE, "低延迟模式需要 CUDA（本机无可用 GPU）")
    _LOW_LATENCY = bool(enabled)
    return describe_latency()


def describe_latency() -> dict:
    """能力快照（内容无关：只有布尔量，给设置页渲染开关用）。

    - `enabled`：当前是否开启；
    - `cuda`：本机 CUDA 是否可用（False 时前端应禁用开关并明示原因）；
    - `default_off_note`：固定提示语，防前端各写一套文案。
    """
    return {
        "enabled": _LOW_LATENCY,
        "cuda": cuda_available(),
        "note": "CPU 默认关闭：整段路径不受影响；开启需 CUDA，首片段目标 ~1.5s（GPU 实测挂起，见 benchmark.md）。",
    }


__all__ = ["cuda_available", "current_provider", "describe_latency",
           "get_switchboard", "is_low_latency", "set_low_latency",
           "set_switchboard"]
