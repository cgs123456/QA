"""FastAPI app: GET /health (public) + authenticated routes.

Auth/DB/business endpoints beyond this skeleton are later tasks; every
non-health route mounts ``verify_token`` (R2).
"""

from fastapi import Depends, FastAPI

from core.auth import verify_token
from routers.knowledge import router as knowledge_router
from routers.model import router as model_router
from routers.qa import router as qa_router
from routers.settings import router as settings_router
from routers.store import router as store_router

VERSION = "0.1.0"
CAPABILITIES = ["health", "qa"]

app = FastAPI(title="InterviewCopilot sidecar")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/sidecar/info", dependencies=[Depends(verify_token)])
def sidecar_info() -> dict:
    """Minimal authenticated route (auth closed-loop proof, task-2)."""
    return {"version": VERSION, "capabilities": CAPABILITIES}


app.include_router(knowledge_router, dependencies=[Depends(verify_token)])
app.include_router(store_router, dependencies=[Depends(verify_token)])
app.include_router(settings_router, dependencies=[Depends(verify_token)])
app.include_router(qa_router, dependencies=[Depends(verify_token)])
app.include_router(model_router, dependencies=[Depends(verify_token)])


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/sidecar/info", dependencies=[Depends(verify_token)])
def sidecar_info() -> dict:
    """Minimal authenticated route (auth closed-loop proof, task-2)."""
    return {"version": VERSION, "capabilities": CAPABILITIES}
