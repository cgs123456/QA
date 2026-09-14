"""模型下载通道单测：鉴权/进度轮询 + 断网续传 e2e（真实 95MB 字节）。"""

import hashlib
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TEST_TOKEN = "test-token-" + "x" * 32


def _h():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _wait_done(client, download_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/model/download/{download_id}", headers=_h())
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.2)
    raise TimeoutError("下载未在时限内完成")


def test_download_requires_auth(api_client):
    client, _ = api_client
    assert client.post("/model/download", json={}).status_code == 401


def test_download_unknown_model_400(api_client):
    client, _ = api_client
    r = client.post("/model/download", json={"model": "nope"}, headers=_h())
    assert r.status_code == 400


def test_download_status_unknown_404(api_client):
    client, _ = api_client
    assert client.get("/model/download/nope", headers=_h()).status_code == 404


def test_download_flow_skipped_done(api_client):
    """本地模型齐全 → 秒级 done（幂等跳过路径）。"""
    client, _ = api_client
    r = client.post("/model/download", json={}, headers=_h())
    assert r.status_code == 200
    download_id = r.json()["download_id"]
    body = _wait_done(client, download_id)
    assert body["status"] == "done", body
    assert set(body["files"]) == {
        "model.onnx", "model.onnx_data", "tokenizer.json", "tokenizer_config.json"}
    assert all(f["done"] for f in body["files"].values())


ABORT_AFTER = 2 * 1024 * 1024


class FlakyHandler(SimpleHTTPRequestHandler):
    """首个无 Range 请求传 2MB 后断开（模拟断网）；后续按 Range 续传。"""

    hits: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FlakyHandler.hits.append(self.headers.get("Range"))
        if len(FlakyHandler.hits) == 1:
            path = self.translate_path(self.path)
            size = os.path.getsize(path)
            body = open(path, "rb").read(ABORT_AFTER)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            return
        return super().do_GET()


@pytest.fixture()
def flaky_server(tmp_path):
    from models.registry import MODEL_REGISTRY, BGE_SMALL_ZH, default_model_dir

    real = default_model_dir(BGE_SMALL_ZH) / "model.onnx_data"
    if not real.is_file():
        pytest.skip("本地模型缺失，跳过断网续传 e2e")
    root = tmp_path / "srv"
    root.mkdir()
    data = real.read_bytes()
    (root / "model.onnx_data").write_bytes(data)
    FlakyHandler.hits = []

    class _H(FlakyHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/model.onnx_data", data
    finally:
        server.shutdown()


def test_resume_after_abort_e2e(flaky_server, tmp_path):
    """中途断网 → 同镜像重试续传 → SHA256 通过（真实 95MB 字节）。"""
    from models.downloader import ensure_model_files

    url, data = flaky_server
    entry = {"files": {"model.onnx_data": {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "urls": [url],
    }}}
    rep = ensure_model_files(entry, tmp_path / "m")
    assert rep["model.onnx_data"]["skipped"] is False
    assert (tmp_path / "m" / "model.onnx_data").read_bytes() == data
    assert len(FlakyHandler.hits) == 2
    assert FlakyHandler.hits[0] is None  # 首传无 Range
    assert FlakyHandler.hits[1] == f"bytes={ABORT_AFTER}-"  # 续传带 Range
