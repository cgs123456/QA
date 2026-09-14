import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from app import VERSION, app
from core.auth import init_token

TEST_TOKEN = "test-token-" + "x" * 32

init_token(TEST_TOKEN)

client = TestClient(app)


def test_health_needs_no_token():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": VERSION}


def test_info_without_token_is_401():
    resp = client.get("/sidecar/info")
    assert resp.status_code == 401
    assert "test-token" not in resp.text


def test_info_with_wrong_token_is_401():
    resp = client.get("/sidecar/info", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_info_with_malformed_header_is_401():
    resp = client.get("/sidecar/info", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


def test_info_with_token_is_200():
    resp = client.get(
        "/sidecar/info", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"version": VERSION, "capabilities": ["health", "qa"]}
