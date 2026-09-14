"""SSE 两段式问答（PRD §3.4）：POST /qa/ask → GET /qa/stream。

- ask：校验后创建 task（后台执行检索 + 路由），返回 task_id。
- stream：SSE 事件流 retrieval → generation* → done；未知/过期 task_id → 404。
- task 在 SSE 读毕或 TTL（60s）后清理；客户端断开（GeneratorExit）取消
  后台任务并清理，不留残留。
- provider/embed 均经模块级间接函数，便于测试注入与后续配置化。
"""

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database.connection import get_connection
from generation.provider import ProviderError, get_provider
from generation.router import answer_stream
from knowledge import stores

router = APIRouter()

TASK_TTL_S = 60.0


def _conn():
    """DB 连接（测试 monkeypatch 本函数指向 tmp 库）。"""
    return get_connection()


def _get_llm(provider_name: str | None):
    """LLM provider（测试 monkeypatch 注入假 provider）。"""
    return get_provider((provider_name or "ollama").strip().lower())


_embedder = None


def _get_embedder():
    """本地 embedding 单例（测试 monkeypatch 注入）。缺模型即 loudly 失败，
    由后台任务转为 error 事件（不断 worker）。"""
    global _embedder
    if _embedder is None:
        from models.registry import BGE_SMALL_ZH, default_model_dir
        from retrieval.embedder import Embedder

        model_dir = default_model_dir(BGE_SMALL_ZH)
        if not (model_dir / "model.onnx").is_file():
            raise RuntimeError("本地 embedding 模型缺失")
        _embedder = Embedder(model_dir)
    return _embedder


class AskBody(BaseModel):
    question: str
    store_id: str | None = None
    provider: str | None = None


class _Task:
    __slots__ = ("question", "store_id", "provider", "created",
                 "queue", "bg", "finished")

    def __init__(self, question, store_id, provider):
        self.question = question
        self.store_id = store_id
        self.provider = provider
        self.created = time.monotonic()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.bg: asyncio.Task | None = None
        self.finished = False


TASKS: dict = {}
TASKS_LOCK = asyncio.Lock()


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _expired(task: _Task) -> bool:
    return (time.monotonic() - task.created) > TASK_TTL_S


async def _cancel_task(task_id: str) -> None:
    """幂等取消：停后台任务 + 删记录（断开/过期/读毕共用）。"""
    async with TASKS_LOCK:
        task = TASKS.pop(task_id, None)
    if task is not None and task.bg is not None and not task.bg.done():
        task.bg.cancel()


async def _sweep_tasks() -> None:
    """清过期 task（TTL 后清理的实在约束——只靠 GET 触发会漏掉永不来读的 ask）。
    入口（ask/stream）处调用，上限定为 60s 窗口内的 task 量级。"""
    now = time.monotonic()
    async with TASKS_LOCK:
        expired = [tid for tid, t in TASKS.items() if (now - t.created) > TASK_TTL_S]
    for tid in expired:
        await _cancel_task(tid)


async def _run_task(task_id: str) -> None:
    async with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        return
    try:
        try:
            llm = _get_llm(task.provider)
        except (ValueError, ProviderError) as e:
            await task.queue.put({"type": "done", "result": {
                "type": "error", "text": "答案生成失败，请稍后重试。",
                "error": getattr(e, "kind", "config"),
                "sources": [], "llm_calls": 0}})
            return
        # embedder 延迟到 answer_stream 内按需构造：缺模型时降级 vec=[] + warnings，
        # 不再此处整体失败（保证 retrieval 事件恒先到达）。

        decision = sources = warnings = None

        def _embed_fn(texts):
            # 延迟到真正需要时构造 embedder；缺模型时抛错，
            # 由 answer_stream 降级为 vec=[] + warnings（不断全链路）。
            return _get_embedder().embed(texts)

        async for event in answer_stream(
            _conn(), task.store_id, task.question, llm, _embed_fn
        ):
            kind = event["type"]
            if kind == "decision":
                decision = event
                warnings = event.get("warnings", [])
            elif kind == "sources":
                sources = event["sources"]
                await task.queue.put({
                    "type": "retrieval",
                    "action": decision["action"],
                    "top1_score": decision["top1_score"],
                    "warnings": warnings,
                    "store_id": task.store_id,
                    "sources": sources,
                })
            elif kind == "chunk":
                await task.queue.put(
                    {"type": "generation", "chunk": event["text"]})
            elif kind == "done":
                if sources is None:
                    # direct / fail_closed：无 sources 事件，sources 取 result。
                    result = event["result"]
                    await task.queue.put({
                        "type": "retrieval",
                        "action": result["type"],
                        "top1_score": decision["top1_score"] if decision else 0.0,
                        "warnings": warnings or [],
                        "store_id": task.store_id,
                        "sources": result.get("sources", []),
                    })
                await task.queue.put(
                    {"type": "done", "result": event["result"]})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # 后台绝不崩 worker：转 error 事件。
        # 堆栈打到 stderr（Rust 侧落盘，降级页可查）；客户端只见种类名。
        import traceback as _traceback

        _traceback.print_exc()
        await task.queue.put({"type": "done", "result": {
            "type": "error", "text": "答案生成失败，请稍后重试。",
            "error": f"internal:{type(e).__name__}",
            "sources": [], "llm_calls": 0}})
    finally:
        task.finished = True


@router.post("/qa/ask")
async def qa_ask(body: AskBody):
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 必填")
    conn = _conn()
    if body.store_id is not None:
        try:
            stores.get_store(conn, body.store_id)
        except stores.StoreNotFound:
            raise HTTPException(status_code=404, detail="store not found")
        store_id = body.store_id
    else:
        store_id = stores.get_current_store_id(conn)
        if store_id is None:
            raise HTTPException(status_code=400, detail="无当前知识库")
    task_id = secrets.token_urlsafe(16)
    task = _Task(question, store_id, body.provider)
    await _sweep_tasks()
    async with TASKS_LOCK:
        TASKS[task_id] = task
    task.bg = asyncio.create_task(_run_task(task_id))
    return {"task_id": task_id}


async def _event_gen(task_id: str, request: Request):
    async with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        return
    try:
        while True:
            # 主动轮询断开（纯靠 GeneratorExit 不可靠：空闲时对端关闭可能
            # 很久才送达；1s 切片只影响断开感知，不影响事件延迟）。
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(task.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if _expired(task):
                    break
                continue
            yield _sse(event)
            if event.get("type") == "done":
                break
    finally:
        # 正常读毕与客户端断开共用：取消后台 + 清理。
        await _cancel_task(task_id)


@router.get("/qa/stream")
async def qa_stream(task_id: str, request: Request):
    """request 用于 _event_gen 的 is_disconnected 轮询（真断开感知）。"""
    await _sweep_tasks()
    async with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task 不存在或已过期")
    if _expired(task):
        await _cancel_task(task_id)
        raise HTTPException(status_code=404, detail="task 不存在或已过期")
    return StreamingResponse(
        _event_gen(task_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁缓冲：chunk 即到即发
            "Connection": "keep-alive",
        },
    )
