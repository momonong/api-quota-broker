"""Root metadata diagnostic must never read any file body."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[1] / "deploy/asus/diagnose_credentials.py"
spec = importlib.util.spec_from_file_location("credential_diagnostic_fixture", path)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def forbidden(*a, **k):
    pytest.fail("diagnostic tried to read a file body")


def test_nonroot_stops_before_directory_or_file_access(monkeypatch):
    monkeypatch.setattr(diagnostic.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(Path, "lstat", forbidden)
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.report()


def test_stage_count_metadata_only_never_opens_hashes_or_prints_body(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir(mode=0o750)
    config.chmod(0o750)
    (config / "credentials").mkdir(mode=0o700)
    stage = config / (".credential-init." + "0" * 32)
    stage.mkdir(mode=0o700)
    evidence = stage / "import-evidence.json"
    evidence.write_bytes(b"UNREADABLE_BODY_FIXTURE")
    evidence.chmod(0o600)
    cipher = stage / "queue_key.cred"
    cipher.write_bytes(b"UNREADABLE_CIPHER_FIXTURE")
    cipher.chmod(0o600)
    unknown = stage / "UNREPORTED_NAME_FIXTURE"
    unknown.touch()
    monkeypatch.setattr(diagnostic, "CONFIG", config)
    monkeypatch.setattr(diagnostic.os, "geteuid", lambda: 0)
    monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: "asus-ubuntu2604-server")
    monkeypatch.setattr(diagnostic.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    real_metadata, real_entries = diagnostic.metadata, diagnostic.entries

    def metadata(path):
        if str(path).startswith(str(config)):
            return {**real_metadata(path), "uid": 0}
        if path == Path("/var/lib/api-quota-broker"):
            return {"present": True, "kind": "directory", "uid": 995, "mode": 0o700}
        return {"present": True, "kind": "file", "uid": 0, "mode": 0o400, "nlink": 1, "size": 4112}

    monkeypatch.setattr(diagnostic, "metadata", metadata)
    monkeypatch.setattr(
        diagnostic,
        "entries",
        lambda p: [] if p == Path("/var/lib/api-quota-broker") else real_entries(p),
    )
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    result = diagnostic.report()
    assert result["host_key_metadata_ready"] and result["credentials_entries"] == 0
    assert result["stages"][0]["entry_count"] == 3
    assert result["stages"][0]["ciphertext_files"] == result["stages"][0]["unknown_entries"] == 1
    output = json.dumps(result)
    for sensitive in (
        "UNREADABLE_BODY_FIXTURE",
        "UNREADABLE_CIPHER_FIXTURE",
        "UNREPORTED_NAME_FIXTURE",
    ):
        assert sensitive not in output
    assert result["credential_reads"] == result["provider_calls"] == result["service_changes"] == 0


def test_directory_limit_stops_at_first_excess_entry(monkeypatch):
    yielded = []

    def children(self):
        for index in range(1000):
            yielded.append(index)
            yield self / str(index)

    monkeypatch.setattr(Path, "iterdir", children)
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.entries(Path("/fixture"))
    assert len(yielded) == 33


def test_symlink_metadata_is_rejected_without_reading_target(tmp_path, monkeypatch):
    target = tmp_path / "body"
    target.write_bytes(b"NEVER_READ_FIXTURE")
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.metadata(link)
