"""双 embedding 切换（F5.3/F6.5，R16）：POST 校验可达 → 后台全量重建 → 原子切换。

流程（全部鉴权，由 app.py 挂载时统一加 verify_token）：

- `GET /embedding/provider` → 当前态 `{active, dim, table, rebuilding, available[]}`
  （内容无关 R14；`rebuilding` 非 null 时 UI 提示“重建中”，检索仍走旧表）。
- `POST /embedding/provider {provider}` →
  同名即 200 空操作；未知名 400；已有重建在途 409；
  目标不可达（本地缺权重 / 云端缺 key / live 探测失败）→ 409 + `{"error": kind}`，
  此时选择与旧表保持原状；可达则建重建任务（后台跑，立即回 `rebuilding` 快照）。
- `GET /embedding/rebuild/{id}` → downloader 式进度
  `{rebuild_id, status, from, to, total, done, tokens, error}`；
  未知/过期 id → 404。

一致性纪律（R6 短事务 + 原子翻转）：

- 重建**只写目标表**（per-batch 短事务 upsert，逐行过维度守卫），
  **从不删行、从不碰旧表**；`set_active()` 只在目标表全量完成后调一次。
  因此：完成前检索天然走旧表 + FTS（不断链）；中途失败（异常/SIGKILL）
  服务态保持旧的一致——旧表 untouched，`active` 未翻转。
  目标表的部分行维度必正确（守卫在事务外先验），下次切换重跑全量覆盖。
- 长任务走 task 后台模式（asyncio.create_task，/qa/ask 同基建），
  **禁止在请求生命周期内同步执行**（坑位）。
- sidecar 重启即回 local（内存语义，与 secrets/ASR 切换板一致）；
  重启前的未完成重建作废（目标表残留行维度正确，下次重跑覆盖）。
"""

import asyncio
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database.connection import get_connection, write_lock
from diagnostics.degrade import record as record_degrade
from retrieval.embedding import (
    BATCH_SIZE,
    PROVIDER_CLOUD,
    PROVIDER_LOCAL,
    TABLE_FOR_PROVIDER,
    EmbeddingError,
    build_embedding_provider,
    check_blob_dim,
    describe as describe_active,
    get_active,
    set_active,
)

router = APIRouter()

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
# 终端态（done/error）保留时长：前端轮询到终态即停，留 5 分钟供复读。
JOB_TTL_S = 300.0
#: 可达探测上限（云端单条探测；超时按不可达处理，不无限挂起切换请求）。
PROBE_TIMEOUT_S = 30.0


def _conn():
    """DB 连接（测试 monkeypatch 本函数指向 tmp 库）。"""
    return get_connection()


def _build_provider(name: str):
    """provider 构造（测试 monkeypatch 注入假 provider）。"""
    return build_embedding_provider(name)


def _record(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="rebuild 不存在或已过期")
    return {
        "rebuild_id": job_id,
        "status": job["status"],
        "from": job["from"],
        "to": job["to"],
        "total": job["total"],
        "done": job["done"],
        "tokens": job["tokens"],
        "error": job["error"],
    }


def _finish(job_id: str, status: str, error: str | None = None) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["status"] = status
        job["error"] = error
        job["completed_at"] = time.monotonic()


def _sweep_jobs() -> None:
    """清终端态超 TTL 的 record（_JOBS 只增不删即内存泄漏）。"""
    now = time.monotonic()
    with _JOBS_LOCK:
        dead = [jid for jid, job in _JOBS.items()
                if job["status"] in ("done", "error")
                and (now - (job.get("completed_at") or now)) > JOB_TTL_S]
        for jid in dead:
            _JOBS.pop(jid, None)


def _running_job_snapshot():
    """在途重建快照（有则返回 record，无则 None；供 provider 快照的 rebuilding 段）。"""
    with _JOBS_LOCK:
        for jid, job in _JOBS.items():
            if job["status"] in ("queued", "running"):
                return {
                    "rebuild_id": jid,
                    "status": job["status"],
                    "from": job["from"],
                    "to": job["to"],
                    "total": job["total"],
                    "done": job["done"],
                    "tokens": job["tokens"],
                    "error": job["error"],
                }
    return None


def _provider_snapshot() -> dict:
    """当前态 + 在途重建（设置页初始渲染 + 轮询二合一）。"""
    snap = describe_active()
    snap["rebuilding"] = _running_job_snapshot()
    return snap


async def _run_rebuild(job_id: str) -> None:
    """后台重建 worker（只写目标表；成功才翻转 active）。

    每批：事务外推理 → 全批维度守卫 → 一个短事务 upsert。
    异常（含 key 中途被清、维度错配、传输失败）→ job 落 error，
    active 与旧表原样不动。CancelledError 同样落 error 后重抛，
    免得“running”僵尸挡住下一次切换。
    """
    import sqlite_vec

    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        to_provider = job["to"]
    table = TABLE_FOR_PROVIDER[to_provider]
    conn = _conn()
    try:
        provider = _build_provider(to_provider)
        rows = conn.execute("SELECT id, standard_question FROM qa_pairs").fetchall()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            job["total"] = len(rows)
        done = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            vecs = await provider.embed([q for _, q in batch])
            if len(vecs) != len(batch):
                raise EmbeddingError(
                    "protocol",
                    f"重建条目数不符（要 {len(batch)} 回 {len(vecs)}）")
            blobs = [sqlite_vec.serialize_float32([float(x) for x in v])
                     for v in vecs]
            for b in blobs:
                check_blob_dim(table, b)
            with write_lock, conn:
                for (qid, _), blob in zip(batch, blobs):
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table}(qa_id, embedding)"
                        " VALUES (?, ?)",
                        (qid, blob),
                    )
            done += len(batch)
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    return
                job["done"] = done
                job["tokens"] = int(getattr(provider, "tokens_used", 0) or 0)
        # 原子翻转：单次赋值，此后检索与入库同源走新表。
        set_active(to_provider)
        _finish(job_id, "done")
    except asyncio.CancelledError:
        _finish(job_id, "error", "CancelledError: 重建被取消")
        raise
    except Exception as e:  # noqa: BLE001 — 落终态用种类名，原文不进记录
        # 重建失败进统一诊断计数（P8 三链之一；active 未翻转，旧表一致）。
        record_degrade("embedding", to_provider,
                       getattr(e, "kind", type(e).__name__))
        _finish(job_id, "error", f"{type(e).__name__}: {e}")


class SwitchBody(BaseModel):
    provider: str


@router.get("/embedding/provider")
def get_embedding_provider():
    """当前态快照（设置页 EmbeddingConfig 初始渲染用）。"""
    _sweep_jobs()
    return _provider_snapshot()


@router.post("/embedding/provider")
async def switch_embedding_provider(body: SwitchBody):
    """切换 embedding provider：校验可达后起后台重建任务，立即返回。

    同名 → 200 空操作；未知名 → 400；在途重建 → 409；
    不可达 → 409（active 与旧表不动）。
    """
    target = (body.provider or "").strip().lower()
    if target not in (PROVIDER_LOCAL, PROVIDER_CLOUD):
        raise HTTPException(status_code=400, detail=f"未知 embedding provider：{body.provider}")
    _sweep_jobs()
    if _running_job_snapshot() is not None:
        raise HTTPException(status_code=409, detail={"error": "rebuild_in_progress",
                                                     "message": "已有重建任务在途"})
    if target == get_active():
        return {"active": target, "rebuilding": None}
    # 可达校验（构造 + live 探测；失败即 409，不建任务不动旧态）。
    try:
        provider = _build_provider(target)
        if target == PROVIDER_LOCAL and not provider.model_ready():
            raise EmbeddingError("unavailable", "本地 embedding 模型缺失")
        if target == PROVIDER_CLOUD:
            await asyncio.wait_for(provider.check_reachable(),
                                   timeout=PROBE_TIMEOUT_S)
    except EmbeddingError as e:
        raise HTTPException(status_code=409, detail={"error": e.kind, "message": str(e)})
    except asyncio.TimeoutError:
        raise HTTPException(status_code=409, detail={"error": "timeout",
                                                     "message": "cloud embeddings 可达探测超时"})
    job_id = secrets.token_urlsafe(12)
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "from": get_active(), "to": target,
                         "total": None, "done": 0, "tokens": 0, "error": None,
                         "completed_at": None, "task": None}
    task = asyncio.create_task(_run_rebuild(job_id))
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id]["task"] = task
    return {"active": get_active(), "rebuilding": _record(job_id)}


@router.get("/embedding/rebuild/{rebuild_id}")
def rebuild_status(rebuild_id: str):
    """重建进度轮询（downloader 同形：status/total/done/tokens/error）。"""
    _sweep_jobs()
    return _record(rebuild_id)
