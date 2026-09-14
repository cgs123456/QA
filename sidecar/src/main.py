"""Sidecar entrypoint: bind 127.0.0.1 ephemeral port, start uvicorn, print handshake.

Contract (R2, R8):
- stdout prints exactly one JSON handshake line with flush=True.
- token is generated (secrets.token_urlsafe(32)), kept in process memory
  only (never logged/persisted), and enforced on every non-health route.
- uvicorn must finish binding BEFORE the handshake is printed; bind
  failure exits non-zero without printing a handshake.
- DB/extension init (vendor 自检 → 建连 → 迁移） happens BEFORE uvicorn;
  any failure exits non-zero without printing a handshake.
"""

import http.client
import json
import os
import secrets
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as _app_module  # noqa: E402 (需写回 HEALTH_NONCE，不能 from-import 值拷贝）
from app import VERSION, app  # noqa: E402
from core.auth import init_token  # noqa: E402
from database.connection import (  # noqa: E402
    check_vendor_files,
    default_db_path,
    get_connection,
)
from database.schema import run_migrations  # noqa: E402

PROTOCOL_VERSION = "1.0"


def pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    except OSError as e:
        print(f"port bind failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        s.close()


def wait_for_health(port: int, nonce: str, timeout_s: float = 10.0) -> bool:
    """轮询 /health 直到本进程的 server 就绪（nonce 匹配防端口占位者）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 200:
                try:
                    data = json.loads(body)
                except ValueError:
                    data = {}
                if data.get("status") == "ok" and data.get("nonce") == nonce:
                    return True
        except OSError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        time.sleep(0.1)
    return False


def init_storage() -> None:
    """vendor 自检 → 建连（WAL+双扩展+词典）→ 迁移。失败即致命错误。"""
    missing = check_vendor_files()
    if missing:
        raise RuntimeError(
            "vendor 缺失，拒绝启动："
            + ", ".join(missing)
            + "（缺 idf 时扩展会 abort 整个进程，绝不带病启动）"
        )
    conn = get_connection()
    run_migrations(conn)


def main() -> None:
    port = pick_port()
    auth_token = secrets.token_urlsafe(32)
    init_token(auth_token)  # enforce on all non-health routes (same process)
    nonce = secrets.token_urlsafe(16)
    _app_module.HEALTH_NONCE = nonce

    try:
        init_storage()
    except RuntimeError as e:
        print(f"storage init failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not wait_for_health(port, nonce):
        print("uvicorn failed to bind/serve /health", file=sys.stderr, flush=True)
        server.should_exit = True
        sys.exit(1)

    handshake = {
        "protocol_version": PROTOCOL_VERSION,
        "port": port,
        "auth_token": auth_token,
        "capabilities": ["health", "qa"],
        "models_loaded": [],
    }
    print(json.dumps(handshake), flush=True)
    thread.join()


if __name__ == "__main__":
    main()
