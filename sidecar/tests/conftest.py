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


def band_s(target: float, routes=("jieba", "simple", "vec"), denom=None) -> float:
    """各路统一取 s 时，使融合分正好等于 `target` 所需的 s（可 >1：该档不可达）。

    路由类单测断言的是**档位**（maybe / direct），不是某个魔数 s；而 s→档位的
    换算随权重与阈值标定而变 —— P6 把 w 从 (3,1,1,1) 改成 (1.5,3,1,4)、Σw 6→9.5
    之后，原本稳落 maybe 档的 s=0.9 直接进了 direct，一批测试集体转红，
    而它们的**本意从未变过**。写死 s 等于让每次标定都得回来改测试。

    routes：在场的路（决定分子）；denom：分母，默认 W_SUM（四路全活）。
    """
    from retrieval import hybrid_rank as hr

    w = {"field": hr.W_FIELD, "jieba": hr.W_JIEBA,
         "simple": hr.W_SIMPLE, "vec": hr.W_VEC}
    return target * (denom or hr.W_SUM) / sum(w[r] for r in routes)


@pytest.fixture()
def maybe_s():
    """落在 maybe 档中点的统一 s（field 路空、jieba/simple/vec 三路在场）。"""
    from retrieval import hybrid_rank as hr

    return band_s((hr.TH_MAYBE + hr.TH_DIRECT) / 2.0)
