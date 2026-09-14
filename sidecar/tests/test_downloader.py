"""downloader 单测：镜像顺序/断点续传/SHA 校验（本地 HTTP 真服务）。"""

import hashlib
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models.downloader import (
    AllMirrorsFailed,
    ChecksumMismatch,
    download_file,
    ensure_model_files,
)

PAYLOAD = os.urandom(3 * 1024 * 1024)
GOOD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def httpd(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "good.bin").write_bytes(PAYLOAD)
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def test_fallback_order_and_progress(httpd, tmp_path):
    calls = []
    dest = tmp_path / "dl.bin"
    out = download_file(
        [f"{httpd}/missing.bin", f"{httpd}/good.bin"],
        dest,
        GOOD_SHA,
        progress=lambda done, total: calls.append((done, total)),
    )
    assert out == dest
    assert dest.read_bytes() == PAYLOAD
    assert calls and calls[-1][0] == len(PAYLOAD)


def test_resume_from_part(httpd, tmp_path):
    dest = tmp_path / "r.bin"
    part = tmp_path / "r.bin.part"
    part.write_bytes(PAYLOAD[: 1024 * 1024])  # 首 1MB 已有
    download_file([f"{httpd}/good.bin"], dest, GOOD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert not part.exists()


def test_checksum_mismatch_leaves_nothing(httpd, tmp_path):
    dest = tmp_path / "bad.bin"
    with pytest.raises(AllMirrorsFailed):
        download_file([f"{httpd}/good.bin"], dest, "0" * 64)
    assert not dest.exists()
    assert not tmp_path.joinpath("bad.bin.part").exists()


def test_all_mirrors_failed(tmp_path):
    with pytest.raises(AllMirrorsFailed):
        download_file(["http://127.0.0.1:1/nope.bin"], tmp_path / "x.bin", GOOD_SHA)


def test_ensure_model_files_idempotent(httpd, tmp_path):
    entry = {
        "files": {
            "good.bin": {
                "sha256": GOOD_SHA,
                "size": len(PAYLOAD),
                "urls": [f"{httpd}/good.bin"],
            }
        }
    }
    seen = []
    rep = ensure_model_files(entry, tmp_path / "m", progress=lambda *a: seen.append(a))
    assert rep["good.bin"]["skipped"] is False
    rep2 = ensure_model_files(entry, tmp_path / "m")
    assert rep2["good.bin"]["skipped"] is True
    assert seen and seen[0][0] == "good.bin"
