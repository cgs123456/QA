"""模型下载进度通道（全部鉴权，由 app.py 挂载时统一加 verify_token）。

落地裁定（PRD 只定义 POST 触发）：POST /model/download 返回 download_id，
GET /model/download/{id} 轮询进度。下载在后台线程跑（urllib 阻塞，不占事件循环），
进度回调写入内存记录；未知 id → 404。契约见 docs/api-contract.md。
"""

import asyncio
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.downloader import AllMirrorsFailed, ChecksumMismatch, DownloadError, ensure_model_files
from models.registry import MODEL_REGISTRY, default_model_dir

router = APIRouter()

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
# 终端态（done/error）保留时长：前端轮询到终态即停，留 5 分钟供复读。
JOB_TTL_S = 300.0


class DownloadBody(BaseModel):
    model: str | None = None


def _record(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="download 不存在或已过期")
    return {
        "download_id": job_id,
        "status": job["status"],
        "files": {
            name: {"downloaded": f["downloaded"], "total": f["total"],
                   "done": f["done"]}
            for name, f in job["files"].items()
        },
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


def _run_download(job_id: str, model_name: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "downloading"
        entry = MODEL_REGISTRY[model_name]
        dest_dir = str(default_model_dir(model_name))

    def _progress(filename, done, total):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            slot = job["files"].setdefault(
                filename, {"downloaded": 0, "total": None, "done": False})
            slot["downloaded"] = done
            slot["total"] = total

    try:
        report = ensure_model_files(entry, dest_dir, progress=_progress)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            for filename in report:
                job["files"].setdefault(
                    filename, {"downloaded": 0, "total": None, "done": False})["done"] = True
        _finish(job_id, "done")
    except (AllMirrorsFailed, ChecksumMismatch, DownloadError) as e:
        _finish(job_id, "error", f"{type(e).__name__}: {e}")
    except Exception as e:  # 磁盘满/权限等非下载错误：同样落终态，不让轮询死等。
        _finish(job_id, "error", f"{type(e).__name__}")


@router.post("/model/download")
async def start_download(body: DownloadBody):
    from models.registry import BGE_SMALL_ZH

    model_name = (body.model or BGE_SMALL_ZH).strip()
    if model_name not in MODEL_REGISTRY:
        raise HTTPException(status_code=400, detail=f"未知模型：{model_name}")
    job_id = secrets.token_urlsafe(12)
    _sweep_jobs()
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "files": {}, "error": None,
                         "completed_at": None}
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_download, job_id, model_name)
    return {"download_id": job_id}


@router.get("/model/download/{download_id}")
async def download_status(download_id: str):
    _sweep_jobs()
    return _record(download_id)
