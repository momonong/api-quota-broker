import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[1] / "scripts/verify_doppler_executor_read.py"
    spec = importlib.util.spec_from_file_location("doppler_probe", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


def test_metadata_only_names_and_exact_scope(module, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(["NVIDIA_API_KEY"]).encode())

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.checked_metadata("/bin/doppler")
    command = calls[0][0]
    assert "--only-names" in command and "--json" in command
    assert command[command.index("--project") + 1] == "api-provider-nvidia"
    assert command[command.index("--config") + 1] == "dev"
    assert calls[0][1]["capture_output"] is True


def test_short_read_only_token_stays_in_memory_and_reports_boolean(module, monkeypatch):
    fixture_token = "dp.st.dev." + "a" * 40
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=fixture_token.encode())

    def fake_resolver(token, project, config):
        assert token == fixture_token
        assert (project, config) == ("api-provider-nvidia", "dev")
        return lambda name: "fixture secret" if name == "NVIDIA_API_KEY" else ""

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "doppler_resolver_from_token", fake_resolver)
    assert module.create_and_read("/bin/doppler") is True
    command = calls[0][0]
    assert command[command.index("--access") + 1] == "read"
    assert command[command.index("--max-age") + 1] == "5m"
    assert fixture_token not in " ".join(command)
    assert calls[0][1]["capture_output"] is True


def test_failed_creation_never_attempts_read(module, monkeypatch):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b""),
    )
    monkeypatch.setattr(
        module, "doppler_resolver_from_token", lambda *_: pytest.fail("unexpected read")
    )
    with pytest.raises(RuntimeError, match="creation failed"):
        module.create_and_read("/bin/doppler")
