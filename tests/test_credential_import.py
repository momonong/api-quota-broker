import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[1] / "scripts/import_doppler_credential.py"
    spec = importlib.util.spec_from_file_location("credential_import", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


def test_import_uses_stdin_and_only_encrypted_file(module, tmp_path, monkeypatch):
    observed = []

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        Path(command[-1]).write_bytes(b"encrypted fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.import_credential(tmp_path, "private fixture token")
    assert result.read_bytes() == b"encrypted fixture"
    assert "private fixture token" not in " ".join(observed[0][0])
    assert observed[0][1]["input"] == b"private fixture token"
    assert not list(tmp_path.glob("*.tmp"))
    assert result.stat().st_mode & 0o777 == 0o600


def test_import_rejects_existing_credential(module, tmp_path, monkeypatch):
    target = tmp_path / "doppler_service_token.cred"
    target.write_bytes(b"old encrypted fixture")

    def fake_run(command, **_):
        Path(command[-1]).write_bytes(b"new encrypted fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(FileExistsError):
        module.import_credential(tmp_path, "new private token")
    assert target.read_bytes() == b"old encrypted fixture"


def test_check_host_refuses_without_tpm(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/systemd-creds")
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: True if str(self) == "/run/systemd/system" else real_is_dir(self),
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1)
    )
    with pytest.raises(RuntimeError, match="TPM2 is unavailable"):
        module.check_host(tmp_path)
