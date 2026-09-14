"""知识库管理 API（全部鉴权，由 app.py 挂载时统一加 verify_token）。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database.connection import get_connection
from knowledge import stores

router = APIRouter()


def _conn():
    """DB 连接（测试 monkeypatch 本函数指向 tmp 库）。"""
    return get_connection()


class CreateStoreBody(BaseModel):
    name: str
    template_id: str | None = None


class SetCurrentBody(BaseModel):
    store_id: str


@router.post("/store")
def create_store(body: CreateStoreBody):
    try:
        return stores.create_store(_conn(), body.name, body.template_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/store/{store_id}")
def store_detail(store_id: str):
    try:
        return stores.get_store(_conn(), store_id)
    except stores.StoreNotFound:
        raise HTTPException(status_code=404, detail="store not found")


@router.delete("/store/{store_id}")
def delete_store(store_id: str):
    try:
        stores.delete_store(_conn(), store_id)
    except stores.StoreNotFound:
        raise HTTPException(status_code=404, detail="store not found")
    return {"deleted": store_id}


@router.put("/stores/current")
def set_current(body: SetCurrentBody):
    try:
        return stores.set_current_store(_conn(), body.store_id)
    except stores.StoreNotFound:
        raise HTTPException(status_code=404, detail="store not found")
