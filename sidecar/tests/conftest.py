"""共享 fixtures：tmp DB（含迁移）与鉴权 HTTP 客户端（tmp DB）。

TEST_TOKEN 与 test_auth.py 取值一致，保证多模块同进程跑时互不覆盖。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token-" + "x" * 32


@pytest.fixture()
def db(tmp_path):
    from database.connection import connect
    from database.schema import run_migrations

    conn = connect(tmp_path / "t.db")
    run_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from core.auth import init_token
    from database.connection import connect
    from database.schema import run_migrations

    init_token(TEST_TOKEN)
    conn = connect(tmp_path / "api.db")
    run_migrations(conn)

    import routers.knowledge as rk
    import routers.store as rs
    from app import app

    monkeypatch.setattr(rk, "_conn", lambda: conn)
    monkeypatch.setattr(rs, "_conn", lambda: conn)
    try:
        with TestClient(app) as client:
            yield client, conn
    finally:
        conn.close()


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
