"""Minimal FastAPI app (skeleton): only GET /health.

Auth/DB/business endpoints are explicitly out of scope for this task.
"""

from fastapi import FastAPI

VERSION = "0.1.0"

app = FastAPI(title="InterviewCopilot sidecar (skeleton)")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}
