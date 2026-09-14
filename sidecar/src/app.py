"""FastAPI app: GET /health (public) + authenticated routes.

Auth/DB/business endpoints beyond this skeleton are later tasks; every
non-health route mounts ``verify_token`` (R2).
"""

from fastapi import Depends, FastAPI

from core.auth import verify_token

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
