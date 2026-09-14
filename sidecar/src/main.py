"""Sidecar entrypoint: bind 127.0.0.1 ephemeral port, start uvicorn, print handshake.

Contract (R2, R8):
- stdout prints exactly one JSON handshake line with flush=True.
- token is generated (secrets.token_urlsafe(32)), kept in process memory
  only (never logged/persisted), and enforced on every non-health route.
- uvicorn must finish binding BEFORE the handshake is printed; bind
  failure exits non-zero without printing a handshake.
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

from app import VERSION, app  # noqa: E402
from core.auth import init_token  # noqa: E402

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


def wait_for_health(port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status == 200 and b'"status":"ok"' in body.replace(b" ", b""):
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def main() -> None:
    port = pick_port()
    auth_token = secrets.token_urlsafe(32)
    init_token(auth_token)  # enforce on all non-health routes (same process)

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not wait_for_health(port):
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
