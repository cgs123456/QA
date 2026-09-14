"""ASR Provider 目录 + 运行时切换（F6.2）。

task17 DoD 要求「切换无需重启应用」。做法分三层：

1. **目录无状态**：`PROVIDER_SPECS` 是静态表（name → 显示名 / 类别 / registry 模型键）。
2. **选择有状态**：`ASRSwitchboard` 持有当前选择，`select()` 只替换一个实例引用。
   构造 provider **不加载权重**（权重在第一次转写时懒加载），所以切换是微秒级的 ——
   这正是「不重启就能换」在实现上唯一需要保证的事。
3. **读取时机**：`routers/audio.py` 每段转写时读一次 `current_provider()`，
   因此切换对**下一段**生效；正在转写的那一段仍用它开始时拿到的引用
   （引用替换是原子的，不会撕裂）。

**默认仍是 faster-whisper**：task15 的既有行为不动。哪个 provider 更合适
由 `docs/benchmark.md` 的同样本横向对比决定，不靠印象改默认值。
"""

from dataclasses import dataclass

from .provider import ERR_UNAVAILABLE, ASRError

PROVIDER_FASTER_WHISPER = "faster-whisper"
PROVIDER_SENSEVOICE = "sensevoice"
PROVIDER_PARAFORMER = "paraformer"
PROVIDER_CLOUD_REST = "cloud-rest"

#: 云端 ASR 的 key 复用 LLM 侧 `openai` 槽位（REST 接口 OpenAI 兼容）。
CLOUD_SECRET_SLOT = "openai"

#: 本地备选偏好顺序。**确定性**是刻意的：同一输入必须得到同一备选，
#: 否则「降级到哪一级」会变成不可复现的行为。
BACKUP_PREFERENCE = (PROVIDER_FASTER_WHISPER, PROVIDER_SENSEVOICE, PROVIDER_PARAFORMER)

DEFAULT_PROVIDER = PROVIDER_FASTER_WHISPER

KIND_LOCAL = "local"
KIND_CLOUD = "cloud"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display: str
    kind: str
    model: str | None  # registry 模型键；云端为 None
    note: str = ""


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    PROVIDER_FASTER_WHISPER: ProviderSpec(
        name=PROVIDER_FASTER_WHISPER,
        display="faster-whisper base（int8，多语种）",
        kind=KIND_LOCAL,
        model="faster-whisper-base",
        note="task15 默认；实测中文倾向输出繁体",
    ),
    PROVIDER_SENSEVOICE: ProviderSpec(
        name=PROVIDER_SENSEVOICE,
        display="SenseVoiceSmall（int8，中英日韩粤）",
        kind=KIND_LOCAL,
        model="sense-voice",
        note="多语种；输出带情感/事件标签，已清洗",
    ),
    PROVIDER_PARAFORMER: ProviderSpec(
        name=PROVIDER_PARAFORMER,
        display="Paraformer-zh（int8，中文）",
        kind=KIND_LOCAL,
        model="paraformer-zh",
        note="中文专用；非中文场景不适用",
    ),
    PROVIDER_CLOUD_REST: ProviderSpec(
        name=PROVIDER_CLOUD_REST,
        display="云端 REST（OpenAI 兼容）",
        kind=KIND_CLOUD,
        model=None,
        note="需要 API Key；本地优先定位下的最后手段",
    ),
}


def provider_names() -> list:
    return list(PROVIDER_SPECS)


def spec(name: str) -> ProviderSpec:
    try:
        return PROVIDER_SPECS[name]
    except KeyError:
        raise ValueError(f"未知 ASR provider：{name}") from None


def _cloud_key(cloud_api_key=None):
    if cloud_api_key:
        return cloud_api_key
    from core.secrets import get_llm_secret

    return get_llm_secret(CLOUD_SECRET_SLOT)


def model_ready(name: str, cloud_api_key=None) -> bool:
    """该 provider 现在能否真的跑起来（只查文件/内存 key，不加载、不联网）。"""
    sp = PROVIDER_SPECS.get(name)
    if sp is None:
        return False
    if sp.kind == KIND_CLOUD:
        return bool(_cloud_key(cloud_api_key))
    from pathlib import Path

    from models import registry

    entry = registry.MODEL_REGISTRY.get(sp.model)
    if entry is None:
        return False
    base = Path(registry.default_model_dir(sp.model))
    return all((base / filename).is_file() for filename in entry["files"])


def build_provider(name: str, *, cloud_api_key=None, **opts):
    """构造**裸** provider（不套降级链）。权重懒加载，故构造很便宜。"""
    sp = spec(name)
    if name == PROVIDER_FASTER_WHISPER:
        from .faster_whisper_provider import FasterWhisperProvider

        return FasterWhisperProvider(**opts)
    if name == PROVIDER_SENSEVOICE:
        from .sensevoice_provider import SenseVoiceProvider

        return SenseVoiceProvider(**opts)
    if name == PROVIDER_PARAFORMER:
        from .paraformer_provider import ParaformerProvider

        return ParaformerProvider(**opts)
    if name == PROVIDER_CLOUD_REST:
        key = _cloud_key(cloud_api_key)
        if not key:
            raise ASRError(ERR_UNAVAILABLE, "云端 ASR 需要 API Key（设置页填写后可用）")
        from .cloud_rest import CloudRestProvider

        return CloudRestProvider(api_key=key, **opts)
    raise ValueError(f"未知 ASR provider：{sp.name}")  # pragma: no cover


def pick_local_backup(selected: str, *, ready=None, cloud_api_key=None) -> str | None:
    """同类本地备选：按固定偏好顺序取第一个「不是当前选中且权重就绪」的本地 provider。

    权重未就绪的不入选 —— 加一个注定失败的降级级毫无意义，只会让日志更吵。
    """
    is_ready = ready or (lambda n: model_ready(n, cloud_api_key))
    for name in BACKUP_PREFERENCE:
        if name == selected:
            continue
        if is_ready(name):
            return name
    return None


def catalog_entries(cloud_api_key=None) -> list:
    """给设置页的目录快照（内容无关：无 key、无路径、无权重字节）。"""
    return [
        {
            "name": sp.name,
            "display": sp.display,
            "kind": sp.kind,
            "model": sp.model,
            "note": sp.note,
            "ready": model_ready(sp.name, cloud_api_key),
        }
        for sp in PROVIDER_SPECS.values()
    ]


class ASRSwitchboard:
    """持有当前 ASR provider，支持运行时替换（F6.2）。"""

    def __init__(self, initial: str = DEFAULT_PROVIDER, *, with_chain: bool = True,
                 cloud_api_key=None, on_degrade=None, **provider_opts):
        spec(initial)  # 名字非法就地报错，而不是等到第一次转写
        self._selected = initial
        self._with_chain = with_chain
        self._cloud_api_key = cloud_api_key
        self._on_degrade = on_degrade
        self._provider_opts = provider_opts
        self._provider = None

    @property
    def selected(self) -> str:
        return self._selected

    def current(self):
        """当前 provider（含降级链，若启用）。首次调用时构造。"""
        if self._provider is None:
            self._provider = self._build(self._selected)
        return self._provider

    def select(self, name: str) -> dict:
        """切换 provider。构造失败（如云端缺 key）就地抛错，**不改动当前选择**。"""
        provider = self._build(name)
        # 先构造成功再替换：切换失败必须保持原状，不能留下半切状态。
        self._provider = provider
        self._selected = name
        return self.describe()

    def describe(self) -> dict:
        return {
            "selected": self._selected,
            "spec": {
                "name": PROVIDER_SPECS[self._selected].name,
                "display": PROVIDER_SPECS[self._selected].display,
                "kind": PROVIDER_SPECS[self._selected].kind,
                "model": PROVIDER_SPECS[self._selected].model,
            },
            "ready": model_ready(self._selected, self._cloud_api_key),
            "with_chain": self._with_chain,
            "available": catalog_entries(self._cloud_api_key),
        }

    def _build(self, name: str):
        primary = build_provider(name, cloud_api_key=self._cloud_api_key, **self._provider_opts)
        if not self._with_chain:
            return primary
        from .fallback import build_default_chain

        backup_name = pick_local_backup(name, cloud_api_key=self._cloud_api_key)
        backup = (build_provider(backup_name, cloud_api_key=self._cloud_api_key)
                  if backup_name else None)
        return build_default_chain(
            primary=primary,
            local_backup=backup,
            cloud_api_key=self._cloud_api_key,
            on_degrade=self._on_degrade,
        )


__all__ = [
    "ASRSwitchboard", "BACKUP_PREFERENCE", "CLOUD_SECRET_SLOT", "DEFAULT_PROVIDER",
    "KIND_CLOUD", "KIND_LOCAL", "PROVIDER_CLOUD_REST", "PROVIDER_FASTER_WHISPER",
    "PROVIDER_PARAFORMER", "PROVIDER_SENSEVOICE", "PROVIDER_SPECS", "ProviderSpec",
    "build_provider", "catalog_entries", "model_ready", "pick_local_backup",
    "provider_names", "spec",
]
