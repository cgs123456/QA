"""模型下载：ModelScope → hf-mirror → HF 顺序、断点续传、SHA256、进度回调。

- 续传：.part 续写（Range），服务器不支持 Range（回 200）则从头写；
  416 视为已完整（后由 SHA256 裁决）。
- 校验：SHA256 全文件增量计算； mismatch 删 .part 并抛 ChecksumMismatch，
  换下一个镜像重试；全灭抛 AllMirrorsFailed。
- 已存在且 SHA 通过 → 跳过（幂等）。
- 进度回调 progress(downloaded: int, total: int | None)，纯计数（R8 无关，
  但同样不记任何业务内容）。
"""

import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 1024 * 1024


class DownloadError(Exception):
    pass


class ChecksumMismatch(DownloadError):
    pass


class AllMirrorsFailed(DownloadError):
    pass


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    return _sha256_of(path).lower() == expected.lower()


def _fetch_one(url: str, part: Path, progress=None, timeout: int = 60) -> None:
    existing = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "InterviewCopilot-sidecar"})
    if existing:
        req.add_header("Range", f"bytes={existing}-")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416 and existing:
            if progress:
                progress(existing, existing)
            return
        raise DownloadError(f"HTTP {e.code}：{url}") from e
    except OSError as e:
        raise DownloadError(f"连接失败：{url}（{e}）") from e

    status = resp.status
    if status == 206 and existing:
        mode, base, total = "ab", existing, None
        length = resp.headers.get("Content-Length")
        total = existing + int(length) if length is not None else None
    elif status == 200:
        mode, base, total = "wb", 0, None
        length = resp.headers.get("Content-Length")
        total = int(length) if length is not None else None
        if part.exists():
            part.unlink()
    else:
        raise DownloadError(f"HTTP {status}：{url}")

    downloaded = base
    if progress:
        progress(downloaded, total)
    with open(part, mode) as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress:
                progress(downloaded, total)


def download_file(urls: list, dest, expected_sha256: str, progress=None) -> Path:
    """按序尝试镜像，成功返回 dest；全灭抛 AllMirrorsFailed。"""
    dest = Path(dest)
    if not urls:
        raise AllMirrorsFailed("无可用镜像")
    if dest.is_file() and verify_sha256(dest, expected_sha256):
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_error: Exception | None = None
    for url in urls:
        try:
            _fetch_one(url, part, progress)
            if verify_sha256(part, expected_sha256):
                os.replace(part, dest)
                return dest
            last_error = ChecksumMismatch(f"SHA256 不一致：{url}")
        except ChecksumMismatch as e:
            last_error = e
        except DownloadError as e:
            last_error = e
        if part.exists():
            part.unlink()
    raise AllMirrorsFailed(f"全部 {len(urls)} 个镜像失败：{last_error}")


def ensure_model_files(entry: dict, dest_dir, progress=None) -> dict:
    """registry 条目全文件就位。返回 {filename: {"path", "skipped"}}。

    progress 回调签名为 progress(filename, downloaded, total)（多文件区分）。
    """
    dest_dir = Path(dest_dir)
    report = {}
    for filename, spec in entry["files"].items():
        if not spec.get("sha256"):
            raise DownloadError(f"{filename} 无 SHA256（registry 未 pin），拒绝下载")
        dest = dest_dir / filename
        skipped = dest.is_file() and verify_sha256(dest, spec["sha256"])
        if not skipped:

            def _cb(done, total):
                if progress:
                    progress(filename, done, total)

            download_file(spec["urls"], dest, spec["sha256"], progress=_cb)
            if spec.get("size") and dest.stat().st_size != spec["size"]:
                raise ChecksumMismatch(
                    f"{filename} 尺寸不符：{dest.stat().st_size} != {spec['size']}"
                )
        report[filename] = {"path": str(dest), "skipped": skipped}
    return report
