"""Credential provisioning with a fake CLI only; never accesses real Doppler."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "shared_credentials", Path(__file__).parents[1] / "scripts/quota_shared_credentials.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_existing_secrets_are_never_overwritten(monkeypatch):
    tool = module()
    monkeypatch.setattr(tool, "names", lambda: set(tool.NAMES))
    monkeypatch.setattr(tool, "doppler", lambda *_a, **_k: pytest.fail("must not write existing"))
    assert tool.provision()["created_names"] == []


def test_values_go_to_stdin_only_and_cli_errors_are_redacted(monkeypatch):
    tool = module()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=1, stdout=b"fixture-sensitive-value", stderr=b"fixture-sensitive-value"
        )

    monkeypatch.setattr(tool.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        tool.doppler(["set", tool.NAMES[1]], input_data=b"fixture-sensitive-value")
    assert "fixture-sensitive-value" not in str(exc.value)
    assert all("fixture-sensitive-value" not in arg for arg in calls[0][0])
    assert calls[0][1]["input"] == b"fixture-sensitive-value"


def test_failed_write_does_not_retry_or_generate_another_identity(monkeypatch):
    tool = module()
    calls = []
    monkeypatch.setattr(tool, "names", lambda: set())

    def stop(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(tool, "doppler", stop)
    with pytest.raises(TimeoutError):
        tool.provision()
    assert calls == [1]


def test_instance_material_and_project_policy_are_separate(monkeypatch):
    tool = module()
    keys = json.loads(tool.new_value(tool.NAMES[0]))
    assert keys["instance_id"] == "hp-shared-20261111" and "provider_keys" not in keys
    monkeypatch.setattr(tool, "get", lambda name: "fixture-" + name + "-token")
    policy = tool.access_policy()
    assert [p["project_id"] for p in policy["principals"]] == ["owner", "knowledge-hub", None]
    assert policy["principals"][2]["scopes"] == ["read", "plan"]
    assert "fixture-" not in json.dumps(policy)
