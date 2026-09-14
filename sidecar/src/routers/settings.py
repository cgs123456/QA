"""模型密钥通道（全部鉴权，由 app.py 挂载时统一加 verify_token）。

落地裁定（PRD 未细化端点）：Rust 在握手后经本端点推送当前所选 provider 的
key；sidecar 仅内存持有（core/secrets.py），不落盘、不打日志、不回显。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.secrets import set_llm_secret

router = APIRouter()


class LLMSecretBody(BaseModel):
    provider: str
    api_key: str


@router.post("/settings/llm-secret")
def push_llm_secret(body: LLMSecretBody):
    try:
        name = set_llm_secret(body.provider, body.api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"stored": name}
