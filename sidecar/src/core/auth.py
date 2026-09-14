"""Bearer token auth (R2, R8).

- Token is generated at sidecar startup (see main.py) and lives ONLY in
  process memory: never logged, never persisted (R8).
- Every route except GET /health mounts ``verify_token``; missing or wrong
  credentials fail closed with 401 (no token material in the response).
"""

import secrets

from fastapi import Header, HTTPException

_token: str | None = None


def init_token(provided: str | None = None) -> str:
    """Set the process token (generated if not provided). Returns it."""
    global _token
    _token = provided or secrets.token_urlsafe(32)
    return _token


def get_token() -> str | None:
    return _token


async def verify_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_token()
    if expected is None or authorization is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not secrets.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="unauthorized")
