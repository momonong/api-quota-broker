"""Offline root-gate scope/CAS checks; never invoke privileged installation."""

import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[1] / "deploy/hp_encrypted_bootstrap.py"
    spec = importlib.util.spec_from_file_location("encrypted_gate", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_bundle_is_exact_and_rejects_mutated_or_linked_files(tmp_path):
    gate = module()
    lock = {}
    for name in gate.FIXED_FILES:
        raw = ("synthetic:" + name).encode()
        (tmp_path / name).write_bytes(raw)
        lock[name] = gate.sha(raw)
    encoded = json.dumps(lock).encode()
    (tmp_path / "bundle.json").write_bytes(encoded)
    assert set(gate.bundle_files(tmp_path, gate.sha(encoded))) == gate.FIXED_FILES
    changed = tmp_path / "package.json"
    changed.write_bytes(b"changed")
    with pytest.raises(ValueError, match="bundle_file_drift"):
        gate.bundle_files(tmp_path, gate.sha(encoded))
    changed.unlink()
    changed.symlink_to(tmp_path / "worker.crt")
    with pytest.raises(ValueError, match="bundle_file_metadata"):
        gate.bundle_files(tmp_path, gate.sha(encoded))


def test_caddy_patch_only_adds_two_scoped_routes_and_refuses_drift(monkeypatch):
    gate = module()
    existing = b"unrelated\n\t        @quota_project_get {\nother-site\n"
    monkeypatch.setattr(gate, "EXPECTED_CADDY", gate.sha(existing))
    snippet = (Path(__file__).parents[1] / "deploy/hp_quota_encrypted_api.caddy").read_bytes()
    candidate = gate.caddy_candidate(existing, snippet)
    addition = b"".join(b"\t        " + line for line in snippet.splitlines(keepends=True))
    assert candidate.replace(addition, b"", 1) == existing
    assert b"path /quota/api/v1/crypto-config\n" in candidate
    assert b"path /quota/api/v1/encrypted\n" in candidate
    with pytest.raises(ValueError):
        gate.caddy_candidate(existing + b"drift", snippet)


def test_runtime_unit_keeps_original_credential_and_loopback_boundaries(monkeypatch):
    gate = module()
    spec = importlib.util.spec_from_file_location(
        "base_gate", Path(__file__).parents[1] / "deploy/hp_shared_bootstrap.py"
    )
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    raw = base.UNIT_TEXT.encode()
    monkeypatch.setattr(gate, "EXPECTED_UNIT", gate.sha(raw))
    unit = gate.encrypted_unit(raw)
    assert b"LoadCredentialEncrypted=runtime.json:" in unit
    assert b"LoadCredentialEncrypted=worker.token:" in unit
    assert b"After=network-online.target quota-private-publish.service" in unit
    assert b"SocketBindDeny=any" in unit
    assert b"SocketBindAllow=ipv4:tcp:18587" in unit
    assert b"SocketBindAllow=ipv4:tcp:18588" in unit
    assert b"quota-private-root" not in unit
    for action, key in (("delegate", b"root.pem"), ("publish", b"signer.pem")):
        maintenance = gate.crypto_unit(action)
        assert b"User=quota-shared-broker" in maintenance
        assert b"IPAddressDeny=any" in maintenance
        assert b"LoadCredentialEncrypted=" + key in maintenance
    assert b"root.pem" not in gate.crypto_unit("publish")
    assert b"storage.key" in gate.crypto_unit("publish")


def test_failed_setup_never_disables_preexisting_tunnel_or_timers(tmp_path, monkeypatch):
    gate = module()
    home = tmp_path / "existing-tunnel"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh/authorized_keys"
    key.write_bytes(b"existing-key")
    config = tmp_path / "existing.conf"
    config.write_bytes(b"existing-policy")
    monkeypatch.setattr(gate, "TUNNEL_HOME", home)
    monkeypatch.setattr(gate, "TUNNEL_CONFIG", config)
    calls = []
    monkeypatch.setattr(gate, "command", lambda argv, **kw: calls.append(argv))
    gate.cleanup_created({}, None)
    assert calls == []
    assert key.read_bytes() == b"existing-key"
    assert config.read_bytes() == b"existing-policy"


def test_busy_publish_not_started_restores_files_without_restart(tmp_path, monkeypatch):
    gate = module()
    paths = {"launcher": "LAUNCHER", "helper": "ENTRY", "policy": "POLICY", "unit": "UNIT"}
    originals = {}
    for key, name in paths.items():
        path = tmp_path / key
        path.write_bytes(b"new")
        monkeypatch.setattr(gate, name, path)
        originals[key] = ("old-" + key).encode()
    events = []

    def maintenance(action, **kwargs):
        events.append(action)
        assert action == "status"
        return {"status": "not_started"}

    monkeypatch.setattr(gate, "maintenance", maintenance)
    monkeypatch.setattr(gate, "command", lambda argv, **kw: events.append(argv))
    monkeypatch.setattr(gate, "atomic", lambda path, raw, **kw: path.write_bytes(raw))
    gate.restore_runtime(
        originals,
        operation="fixed-operation",
        publish_attempted=True,
        runtime_changed=True,
        caddy_changed=False,
        service_gid=1000,
    )
    assert events == ["status", ["/usr/bin/systemctl", "daemon-reload"]]
    assert all((tmp_path / name).read_bytes() == raw for name, raw in originals.items())
