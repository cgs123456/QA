"""connection 锚定单测：BASE 解析优先级 + vendor 自检。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from database import connection
from database.connection import check_vendor_files, default_db_path, resolve_base


def test_resolve_base_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEWCOPILOT_BASE", str(tmp_path))
    assert resolve_base() == tmp_path.resolve()
    assert connection.vendor_dir() == tmp_path.resolve() / "vendor"
    assert connection.dict_dir() == tmp_path.resolve() / "vendor" / "dict"


def test_resolve_base_dev_is_absolute():
    base = resolve_base()
    assert base.is_absolute()
    assert (base / "src" / "main.py").is_file()
    assert (base / "vendor" / "dict").is_dir()


def test_check_vendor_files_ok_in_dev():
    assert check_vendor_files() == []


def test_check_vendor_files_detects_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEWCOPILOT_BASE", str(tmp_path))
    missing = check_vendor_files()
    assert "libsimple.dll" in missing or "libsimple.so" in missing or "libsimple.dylib" in missing
    assert any(m.startswith("dict/") for m in missing)


def test_default_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEWCOPILOT_DB", str(tmp_path / "x.db"))
    assert default_db_path() == (tmp_path / "x.db").resolve()


def test_default_db_path_dev_under_base():
    assert default_db_path() == resolve_base() / "data" / "interviewcopilot.db"


def test_default_db_path_frozen_outside_bundle(tmp_path, monkeypatch):
    """打包态 DB 进用户数据目录，绝不进 bundle（曾污染体积基线 + 只读安装即崩）。"""
    import sys as _sys

    fake_meipass = tmp_path / "_internal"
    fake_meipass.mkdir()
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "_MEIPASS", str(fake_meipass), raising=False)
    monkeypatch.delenv("INTERVIEWCOPILOT_DB", raising=False)
    db = default_db_path()
    assert db.name == "interviewcopilot.db"
    assert fake_meipass not in db.parents
    assert "copilot" in db.parts[-3].lower()  # …/InterviewCopilot|interviewcopilot/data/…
