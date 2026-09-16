"""设置通道：模型密钥 + LLM/ASR Provider 目录（全部鉴权，由 app.py 挂载时统一加 verify_token）。

**密钥通道**：Rust 在握手后经本端点推送当前所选 provider 的 key；
sidecar 仅内存持有（core/secrets.py），不落盘、不打日志、不回显。

**ASR Provider 切换（task17 / F6.2）**：`GET /asr/providers` 取目录快照，
`PUT /asr/provider` 切换。切换操作的是 `asr.runtime` 的进程级切换板，
故**无需重启**即对下一段音频生效（详见 routers/audio.py 的 task17 记录）。

**低延迟模式（task18）**：`GET /asr/latency` 取能力快照
`{enabled, cuda, note}`，`PUT /asr/latency {enabled}` 开关。
开启要求 CUDA 就绪，否则 409 + `{"error": "unavailable"}`；关闭永远允许。
开关只影响**下一段**（audio.py 在 segment_start 快照），关闭时段处理
与 task15 逐字一致（回归单测锁定）。

**音频诊断（task19）**：`GET /diagnostics/audio` 返回最近一次 WS 连接的
内容无关计数（`routers/audio.py::_LAST_COUNTERS` 的只读快照）+ 限额常量。
从未建连 → `last_connection: null`（不是 404——“无数据”是合法状态）。
`capture` 段报 sidecar **真正看得见**的那部分采集状态（当前在连的 path，
见 `_capture_status`）；设备名/原生格式/自检结论属 Rust 采集侧，本端点
看不见也不编造，前端从 Tauri 命令 `get_capture_state` 拿那些。
落地裁定（PRD 未细化端点）：
- 切换用 PUT 而非 POST —— 与既有 `PUT /stores/current` 同形（单选切换语义幂等）。
- 未知 provider 名 → 400（请求体有误，不是资源缺失）。
- 构造失败（如切到云端但未填 key）→ 409 + `{"error": kind}`：
  选择本身合法，是**前置条件**不满足；此时切换板保持原选择不变
  （`ASRSwitchboard.select` 先构造成功才替换），故 409 之后状态仍自洽。
- 响应体是内容无关的：无 key、无路径、无权重字节（R14）。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from asr.provider import ASRError
from asr.runtime import describe_latency, get_switchboard, set_low_latency
from core.secrets import set_llm_secret

router = APIRouter()


class LLMSecretBody(BaseModel):
    provider: str
    api_key: str


class SetASRProviderBody(BaseModel):
    name: str


class SetLatencyBody(BaseModel):
    enabled: bool


@router.post("/settings/llm-secret")
def push_llm_secret(body: LLMSecretBody):
    try:
        name = set_llm_secret(body.provider, body.api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"stored": name}


@router.get("/llm/providers")
def list_llm_providers():
    """LLM provider 目录快照（F6.1 全量：设置页 provider 下拉渲染用）。

    内容无关：仅名字/显示名/是否需 key/默认模型/base_url/流格式/备注，
    无 key、无密钥槽值。密钥复用 POST /settings/llm-secret
   （secret_slot 即 provider 名，ollama 除外）。
    """
    from generation.provider import describe_llm_providers

    return {"providers": describe_llm_providers()}


@router.get("/asr/providers")
def list_asr_providers():
    """ASR provider 目录 + 当前选择（设置页初始渲染用）。"""
    return get_switchboard().describe()


@router.put("/asr/provider")
def set_asr_provider(body: SetASRProviderBody):
    """切换 ASR provider，立即生效（无需重启）。"""
    try:
        return get_switchboard().select(body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ASRError as e:
        # kind 内容无关，可直接下行；message 是本地写死的提示语（无用户数据）。
        raise HTTPException(status_code=409, detail={"error": e.kind, "message": str(e)})


@router.get("/asr/latency")
def get_latency():
    """低延迟模式能力快照（设置页开关渲染用）。"""
    return describe_latency()


@router.put("/asr/latency")
def set_latency(body: SetLatencyBody):
    """开/关低延迟模式。无 CUDA 时开启 → 409（`unavailable`）；关闭永远允许。"""
    try:
        return set_low_latency(body.enabled)
    except ASRError as e:
        raise HTTPException(status_code=409, detail={"error": e.kind, "message": str(e)})


def _capture_status() -> dict:
    """`capture` 段：sidecar **真正看得见**的那部分采集状态。

    设备名/原生格式/自检结论属于 Rust 采集侧，本端点看不见也不编造（前端从
    Tauri 命令 `get_capture_state` 拿那些）。这里只报"线上有没有人在推音频"——
    这正是排查"UI 说在采、sidecar 什么都没收到"时唯一需要的那个事实。
    """
    from routers import audio as audio_mod

    active = audio_mod.active_paths()
    if active:
        return {
            "status": "connected",
            "active_paths": active,
            "note": "采集服务已连上 /audio/stream",
        }
    if audio_mod.last_counters() is None:
        return {
            "status": "idle",
            "active_paths": [],
            "note": "尚未收到任何音频连接",
        }
    return {
        "status": "disconnected",
        "active_paths": [],
        "note": "最近一次音频连接已断开",
    }


@router.get("/diagnostics/degrade")
def degrade_diagnostics():
    """三链降级计数（P8 可用性收尾，R14 内容无关）。

    `{chains: {asr|llm|embedding: {total, by_kind, by_provider}}}`——
    只有链名/provider 名/错误种类/计数，无文本、无向量、无 key。
    ASR 由进程单例切换板写入；LLM 由问答降级链写入；embedding 由重建失败
    与问答时向量故障写入。sidecar 重启即清零（内存语义）。
    """
    from diagnostics.degrade import snapshot

    return {"chains": snapshot()}


@router.get("/diagnostics/audio")
def audio_diagnostics():
    """最近一次音频 WS 连接的内容无关计数 + 限额（诊断面板轮询用，R14）。

    单向依赖 settings → audio（audio 不反向依赖 settings，无环）。
    """
    from routers import audio as audio_mod

    return {
        "last_connection": audio_mod.last_counters(),
        "limits": {
            "max_pending": audio_mod.MAX_PENDING,
            "max_segment_frames": audio_mod.MAX_SEGMENT_FRAMES,
            "send_timeout_s": audio_mod.SEND_TIMEOUT_S,
        },
        "capture": _capture_status(),
    }
