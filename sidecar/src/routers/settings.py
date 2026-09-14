"""设置通道：模型密钥 + ASR Provider 选择（全部鉴权，由 app.py 挂载时统一加 verify_token）。

**密钥通道**：Rust 在握手后经本端点推送当前所选 provider 的 key；
sidecar 仅内存持有（core/secrets.py），不落盘、不打日志、不回显。

**ASR Provider 切换（task17 / F6.2）**：`GET /asr/providers` 取目录快照，
`PUT /asr/provider` 切换。切换操作的是 `asr.runtime` 的进程级切换板，
故**无需重启**即对下一段音频生效（详见 routers/audio.py 的 task17 记录）。

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
from asr.runtime import get_switchboard
from core.secrets import set_llm_secret

router = APIRouter()


class LLMSecretBody(BaseModel):
    provider: str
    api_key: str


class SetASRProviderBody(BaseModel):
    name: str


@router.post("/settings/llm-secret")
def push_llm_secret(body: LLMSecretBody):
    try:
        name = set_llm_secret(body.provider, body.api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"stored": name}


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
