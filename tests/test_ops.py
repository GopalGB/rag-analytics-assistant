"""Configuration validation and backup/restore."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_invalid_settings_fail_fast():
    with pytest.raises(ValidationError):
        Settings(qbo_mode="live")
    with pytest.raises(ValidationError):
        Settings(date_order="YMD")


def test_config_warnings(tmp_path):
    s = Settings(data_dir=str(tmp_path / "missing"), trust_loopback=False, app_api_key=None, allow_cloud_ai=True,
                 redact_pii=False, cloud_allowed_data="documents,accounting", report_as_of="15/07/2026",
                 llm_provider="skynet", qbo_mode="sandbox", qbo_client_id=None)
    text = " ".join(s.warnings())
    for fragment in ("does not exist", "APP_API_KEY", "REDACT_PII", "accounting", "REPORT_AS_OF", "skynet", "QBO_CLIENT_ID"):
        assert fragment in text


def test_backup_roundtrip(tmp_path):
    from app.audit import AuditLog

    storage, data = tmp_path / "storage", tmp_path / "data"
    (data / "docs").mkdir(parents=True)
    (data / "docs" / "a.md").write_text("hello")
    storage.mkdir()
    (storage / "approvals.json").write_text("[]")
    (storage / "cache").mkdir()
    (storage / "cache" / "big.json").write_text("rebuildable")
    AuditLog(storage / "audit.jsonl").record("x")
    env = {"STORAGE_DIR": str(storage), "DATA_DIR": str(data), "PATH": "/usr/bin:/bin"}
    run = lambda *a, e=env: subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), *a],  # noqa: E731
                                           capture_output=True, text=True, env=e, cwd=ROOT)
    out = run("create", "--out", str(tmp_path / "b"))
    assert out.returncode == 0, out.stderr
    archive = next((tmp_path / "b").glob("*.tar.gz"))
    assert run("verify", str(archive)).returncode == 0
    target = {**env, "STORAGE_DIR": str(tmp_path / "r"), "DATA_DIR": str(tmp_path / "rd")}
    res = run("restore", str(archive), e=target)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "rd" / "docs" / "a.md").read_text() == "hello"
    assert not (tmp_path / "r" / "cache").exists()  # caches are not backed up
    assert run("restore", str(archive), e=target).returncode != 0  # refuses to overwrite without --force
