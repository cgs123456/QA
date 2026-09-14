"""R2 全覆盖：除 GET /health 外，一切端点无 token → 401（与 body 合法性无关，
鉴权先于参数校验执行）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)

NO_AUTH_CASES = [
    ("GET", "/sidecar/info", None),
    ("POST", "/knowledge/compile", {}),
    ("GET", "/knowledge/list", None),
    ("GET", "/knowledge/search?q=x", None),
    ("POST", "/store", {}),
    ("GET", "/store/abc", None),
    ("DELETE", "/store/abc", None),
    ("PUT", "/stores/current", {}),
    ("POST", "/settings/llm-secret", {}),
    ("POST", "/qa/ask", {}),
    ("GET", "/qa/stream?task_id=x", None),
    ("POST", "/model/download", {}),
    ("GET", "/model/download/x", None),
]


def test_health_needs_no_token():
    assert client.get("/health").status_code == 200


def test_everything_else_requires_token():
    for method, path, body in NO_AUTH_CASES:
        resp = client.request(method, path, json=body)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
