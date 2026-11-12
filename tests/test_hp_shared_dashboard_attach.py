"""Fixed Dashboard-only attachment; no real host, secret or systemd operations."""

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_retains_kernel_contract_and_uses_read_only_live_upstream(tmp_path):
    old = load("hp_quota_launcher")
    launcher = load("hp_shared_dashboard_launcher")
    assert launcher.ARGS == old.ARGS
    arguments = launcher.application_args(tmp_path, tmp_path / "broker-read")
    assert arguments[arguments.index("--upstream") + 1] == "http://127.0.0.1:18587"
    assert "--reviewed-config" not in arguments and "--activity-upstream" not in arguments
    assert (
        arguments[arguments.index("--dashboard-password-file") + 1]
        == "/etc/api-quota-dashboard/browser-auth.json"
    )
    assert arguments[-2:] == ["--port", "18586"]


def test_launcher_accepts_synthetic_systemd_acl_mode_and_copies_owner_private(
    tmp_path, monkeypatch
):
    launcher = load("hp_shared_dashboard_launcher")
    monkeypatch.setattr(launcher, "ROOT_OWNER", os.geteuid())
    credentials = tmp_path / "credentials" / "api-quota-dashboard.service"
    credentials.mkdir(parents=True, mode=0o750)
    credentials.parent.chmod(0o750)
    token = credentials / "broker-read"
    token.write_text("fixture-scoped-read-plan-token-over-thirty-two")
    token.chmod(0o440)
    monkeypatch.setattr(launcher, "BROKER_CREDENTIAL", token)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    result = launcher.private_broker_token(private)
    assert result.read_bytes() == token.read_bytes()
    assert result.stat().st_mode & 0o777 == 0o600
    result.unlink()
    token.chmod(0o444)
    with pytest.raises(ValueError):
        launcher.private_broker_token(private)


@pytest.fixture
def host(tmp_path, monkeypatch):
    gate = load("hp_shared_dashboard_attach")
    launcher = (ROOT / "deploy/hp_shared_dashboard_launcher.py").read_bytes()
    old_unit = b"[Unit]\nDescription=fixture\n[Service]\nUser=fixture\nExecStart=/usr/bin/python3 -I -B /usr/local/libexec/momonong-quota-launcher.py dashboard-serve fixture\nNoNewPrivileges=yes\nProtectSystem=strict\nIPAddressDeny=any\nIPAddressAllow=localhost\n[Install]\nWantedBy=multi-user.target\n"
    paths = {}
    for name in ("LAUNCHER", "UNIT", "POLICY", "HELPER", "CREDENTIAL"):
        path = tmp_path / name.lower()
        paths[name] = path
        monkeypatch.setattr(gate, name, path)
    monkeypatch.setattr(gate, "OWNER", os.geteuid())
    monkeypatch.setattr(gate, "STATE", tmp_path / "operation")
    monkeypatch.setattr(gate, "OLD_LAUNCHER", gate.digest(b"old launcher\n"))
    monkeypatch.setattr(gate, "OLD_UNIT", gate.digest(old_unit))
    paths["LAUNCHER"].write_bytes(b"old launcher\n")
    paths["UNIT"].write_bytes(old_unit)
    paths["HELPER"].write_bytes(b"fixed old helper\n")
    policy = {
        "schema": "hp-quota-maintenance-policy-v1",
        "launcher_sha256": gate.OLD_LAUNCHER,
        "unit_sha256": gate.OLD_UNIT,
        "helper_sha256": gate.digest(paths["HELPER"].read_bytes()),
        "service_user": "fixture",
        "service_group": "fixture",
    }
    paths["POLICY"].write_bytes(gate.canonical(policy))
    for name in ("LAUNCHER", "UNIT", "HELPER"):
        paths[name].chmod(0o644)
    paths["POLICY"].chmod(0o600)
    calls = []
    monkeypatch.setattr(gate, "restart_dashboard", lambda: calls.append("restart"))
    monkeypatch.setattr(gate, "verify_dashboard", lambda: calls.append("verify"))
    monkeypatch.setattr(gate, "verify_broker", lambda token: calls.append("broker-read"))
    monkeypatch.setattr(
        gate, "encrypt_token", lambda token: b"fixture-encrypted-systemd-credential"
    )
    return gate, launcher, paths, policy, calls


def test_attach_preserves_all_unit_restrictions_and_helper_pin(host):
    gate, launcher, paths, policy, calls = host
    original = paths["UNIT"].read_bytes()
    request = {
        "launcher_base64": base64.b64encode(launcher).decode(),
        "read_token": "fixture-read-plan-only-token-thirty-two-characters",
    }
    candidate = gate.prepare(request, gate.digest(launcher), apply=True)
    assert not gate.STATE.exists()
    assert candidate["unit"].replace(gate.CREDENTIAL_LINE.encode() + b"\n", b"") == original
    result = gate.apply(candidate, request["read_token"])
    assert result["status"] == "complete"
    new_policy = json.loads(paths["POLICY"].read_bytes())
    assert new_policy["helper_sha256"] == policy["helper_sha256"]
    assert new_policy["launcher_sha256"] == gate.digest(launcher)
    assert new_policy["unit_sha256"] == gate.digest(paths["UNIT"].read_bytes())
    assert paths["CREDENTIAL"].read_bytes() == b"fixture-encrypted-systemd-credential"
    assert gate.status()["status"] == "complete"
    with pytest.raises(ValueError):
        gate.apply(candidate, request["read_token"])
    assert calls == ["broker-read", "restart", "verify"]


def test_failed_start_restores_three_files_and_keeps_original_receipt(host, monkeypatch):
    gate, launcher, paths, _policy, calls = host
    original = {name: paths[name].read_bytes() for name in ("LAUNCHER", "UNIT", "POLICY")}
    request = {
        "launcher_base64": base64.b64encode(launcher).decode(),
        "read_token": "fixture-read-plan-only-token-thirty-two-characters",
    }
    candidate = gate.prepare(request, gate.digest(launcher), apply=True)
    checks = iter([False, True])

    def verify():
        if not next(checks):
            raise gate.Refused("fixture_start_failed")

    monkeypatch.setattr(gate, "verify_dashboard", verify)
    result = gate.apply(candidate, request["read_token"])
    assert result["status"] == "rolled_back"
    assert all(paths[name].read_bytes() == raw for name, raw in original.items())
    assert paths["CREDENTIAL"].exists()
    assert gate.status()["status"] == "rolled_back"
    assert (gate.STATE / "intent.json").exists()
    assert calls == ["broker-read", "restart", "restart"]


def test_unknown_or_foreign_change_never_overwritten_by_rollback(host, monkeypatch):
    gate, launcher, paths, _policy, _calls = host
    request = {
        "launcher_base64": base64.b64encode(launcher).decode(),
        "read_token": "fixture-read-plan-only-token-thirty-two-characters",
    }
    candidate = gate.prepare(request, gate.digest(launcher), apply=True)

    def crash():
        paths["UNIT"].write_bytes(b"foreign unit\n")
        raise KeyboardInterrupt()

    monkeypatch.setattr(gate, "restart_dashboard", crash)
    with pytest.raises(KeyboardInterrupt):
        gate.apply(candidate, request["read_token"])
    assert gate.status()["status"] == "restarting"
    with pytest.raises(ValueError):
        gate.rollback()
    assert paths["UNIT"].read_bytes() == b"foreign unit\n"


def test_preclaim_drift_or_invalid_payload_has_no_effect(host):
    gate, launcher, paths, _policy, _calls = host
    request = {"launcher_base64": base64.b64encode(launcher).decode()}
    assert gate.prepare(request, gate.digest(launcher))["launcher"] == launcher
    for altered in ({**request, "command": "anything"}, {**request, "read_token": "short"}):
        with pytest.raises(ValueError):
            gate.prepare(altered, gate.digest(launcher), apply=True)
    paths["LAUNCHER"].write_bytes(b"drift\n")
    with pytest.raises(ValueError):
        gate.prepare(request, gate.digest(launcher))
    assert not gate.STATE.exists()


def test_broker_credential_scope_must_be_read_plan_without_project_or_execute(
    tmp_path, monkeypatch
):
    gate = load("hp_shared_dashboard_attach")
    token = "fixture-read-plan-only-token-thirty-two-characters"
    path = tmp_path / "access.json"
    monkeypatch.setattr(gate, "OWNER", os.geteuid())
    good = {
        "version": 1,
        "principals": [
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "project_id": None,
                "scopes": ["read", "plan"],
                "limits": {},
            }
        ],
    }
    path.write_bytes(gate.canonical(good))
    path.chmod(0o640)
    gate.check_read_policy(path, token)
    for changed in ({"scopes": ["read", "plan", "execute"]}, {"project_id": "project-a"}):
        principal = {**good["principals"][0], **changed}
        path.write_bytes(gate.canonical({"version": 1, "principals": [principal]}))
        with pytest.raises(ValueError, match="dashboard_read_plan_scope_required"):
            gate.check_read_policy(path, token)


def test_launcher_five_metadata_files_remain_manifest_bound(tmp_path, monkeypatch):
    launcher = load("hp_shared_dashboard_launcher")
    monkeypatch.setattr(launcher, "ROOT_OWNER", os.geteuid())
    release, private = tmp_path / "release", tmp_path / "private"
    release.mkdir(mode=0o750)
    metadata = release / "metadata"
    metadata.mkdir(mode=0o750)
    private.mkdir(mode=0o700)
    manifest = []
    for name in launcher.METADATA:
        path = metadata / name
        raw = json.dumps({"fixture": name}).encode()
        path.write_bytes(raw)
        path.chmod(0o640)
        manifest.append(
            {
                "path": "metadata/" + name,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    (release / "manifest.json").write_text(json.dumps(manifest))
    (release / "manifest.json").chmod(0o640)
    launcher.private_metadata(release, private)
    assert sorted(path.name for path in private.iterdir()) == sorted(launcher.METADATA)
    assert all((private / name).stat().st_mode & 0o777 == 0o600 for name in launcher.METADATA)
    for path in private.iterdir():
        path.unlink()
    (metadata / "accounts.json").write_text("{}")
    with pytest.raises(ValueError, match="metadata_digest"):
        launcher.private_metadata(release, private)
    assert list(private.iterdir()) == []


def test_unexpected_systemd_dropin_or_identity_blocks_attachment(host, monkeypatch):
    gate, _launcher, paths, policy, _calls = host
    good = f"User=fixture\nGroup=fixture\nFragmentPath={paths['UNIT']}\nDropInPaths=\n".encode()
    monkeypatch.setattr(gate, "command", lambda *_args, **_kwargs: good)
    gate.verify_host_contract(policy)
    monkeypatch.setattr(
        gate,
        "command",
        lambda *_args, **_kwargs: good.replace(
            b"DropInPaths=\n", b"DropInPaths=/etc/foreign.conf\n"
        ),
    )
    with pytest.raises(ValueError):
        gate.verify_host_contract(policy)
    assert not gate.STATE.exists()


def test_encryption_failure_precedes_claim_and_plaintext_is_not_persisted(host, monkeypatch):
    gate, launcher, paths, _policy, _calls = host
    token = "fixture-read-plan-only-token-thirty-two-characters"
    payload = {"launcher_base64": base64.b64encode(launcher).decode(), "read_token": token}
    candidate = gate.prepare(payload, gate.digest(launcher), apply=True)

    def fail(_token):
        raise gate.Refused("fixture encryption unavailable")

    monkeypatch.setattr(gate, "encrypt_token", fail)
    with pytest.raises(ValueError):
        gate.apply(candidate, token)
    assert not gate.STATE.exists() and not paths["CREDENTIAL"].exists()
    assert all(token.encode() not in path.read_bytes() for path in paths.values() if path.exists())


def test_anonymous_dashboard_health_uses_required_public_host_header(monkeypatch):
    import io
    import urllib.error
    from contextlib import nullcontext
    from types import SimpleNamespace

    gate = load("hp_shared_dashboard_attach")
    monkeypatch.setattr(
        gate,
        "command",
        lambda *_args, **_kwargs: b"ActiveState=active\nSubState=running\nMainPID=123\n",
    )
    calls = []

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Host") == "momonong.me"
            calls.append(request.full_url)
            if request.full_url.endswith("/api/snapshot"):
                raise urllib.error.HTTPError(
                    request.full_url, 401, "unauthorized", {}, io.BytesIO()
                )
            return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(gate.urllib.request, "build_opener", lambda *_args: Opener())
    gate.verify_dashboard()
    assert calls == ["http://127.0.0.1:18586/quota/", "http://127.0.0.1:18586/quota/api/snapshot"]
