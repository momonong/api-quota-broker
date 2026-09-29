import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[1] / "scripts/set_nvidia_doppler_secret.py"
    spec = importlib.util.spec_from_file_location("doppler_admin", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


def test_cli_admin_uses_explicit_scope_and_stdin(module, monkeypatch):
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/doppler")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.set_secret("approved-project", "dev", "private fixture key")
    assert len(calls) == 2
    assert all(
        "--project" in c[0] and "--config" in c[0] and "--no-read-env" in c[0] for c in calls
    )
    assert all("private fixture key" not in " ".join(c[0]) for c in calls)
    assert calls[1][1]["input"] == b"private fixture key"
    assert calls[1][0][-3:] == ["secrets", "set", "NVIDIA_API_KEY"]


def test_cli_admin_fails_before_write_if_preflight_fails(module, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/doppler")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="preflight"):
        module.set_secret("approved-project", "dev", "private fixture key")
    assert len(calls) == 1
