"""Root bootstrap contract validation; temporary files and mocked host actions only."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hp_shared_bootstrap", Path(__file__).parents[1] / "deploy/hp_shared_bootstrap.py"
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def blob(raw):
    return {
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_base64": base64.b64encode(raw).decode(),
    }


def payload():
    launcher = b"# reviewed launcher fixture\n"
    return {
        "schema": "hp-quota-shared-bootstrap-v1",
        "operation_id": "a5d3103e-7253-45ac-a0d6-6fb06203d329",
        "release_id": "shared-20261111-v1",
        "instance_id": bootstrap.INSTANCE,
        "launcher": blob(launcher),
        "files": [
            dict(path=path, **blob(b"{}" if path.endswith(".json") else b"# fixture\n"))
            for path in bootstrap.PATHS
        ],
    }


def validate(value):
    return bootstrap.validate_payload(
        value, bootstrap.digest(bootstrap.canonical(value)), value["launcher"]["sha256"]
    )


def test_only_exact_paths_and_pinned_bytes_are_admitted():
    value = payload()
    proof = validate(value)
    assert set(proof["files"]) == set(bootstrap.PATHS)
    assert proof["launcher"] == b"# reviewed launcher fixture\n"
    assert "systemd/system" not in " ".join(bootstrap.PATHS)
    for path in ["../../etc/shadow", "app/quota_broker/extra.py", "config/provider-secret.json"]:
        changed = payload()
        changed["files"][0]["path"] = path
        with pytest.raises(bootstrap.Refused):
            validate(changed)


def test_reordered_missing_duplicate_and_tampered_payloads_stop_before_actions():
    for change in [
        lambda d: d["files"].reverse(),
        lambda d: d["files"].pop(),
        lambda d: d["files"].append(d["files"][0]),
        lambda d: d["files"][0].update(content_base64="eA=="),
        lambda d: d.update(command="arbitrary"),
    ]:
        value = payload()
        change(value)
        with pytest.raises(bootstrap.Refused):
            validate(value)
    value = payload()
    with pytest.raises(bootstrap.Refused):
        bootstrap.validate_payload(value, "0" * 64, value["launcher"]["sha256"])
    with pytest.raises(bootstrap.Refused):
        bootstrap.validate_payload(value, bootstrap.digest(bootstrap.canonical(value)), "0" * 64)


def test_credential_binding_matches_runtime_without_importing_application():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "runtime_fixture", Path(__file__).parents[1] / "deploy/hp_shared_runtime.py"
    )
    runtime = module_from_spec(spec)
    spec.loader.exec_module(runtime)
    credential = {
        "schema": 1,
        "instance_id": bootstrap.INSTANCE,
        "digest_key": base64.b64encode(b"d" * 32).decode(),
        "queue_key": base64.b64encode(b"q" * 32).decode(),
        "legacy_token": "L" * 48,
        "provider_keys": {
            "OPENAI_API_KEY": "fixture-openai",
            "OPENROUTER_API_KEY": "fixture-openrouter",
        },
    }
    token = "R" * 48
    access = {
        "version": 1,
        "principals": [
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "project_id": None,
                "scopes": ["read", "plan"],
                "limits": {
                    "max_input_bytes": 4096,
                    "max_output_tokens": 64,
                    "max_inflight": 1,
                    "max_deadline_seconds": 180,
                },
            }
        ],
    }
    result = bootstrap.validate_credentials(credential, token, access)
    assert result == runtime.credential_fingerprint(b"d" * 32, b"q" * 32, bootstrap.INSTANCE)
    access["principals"][0]["scopes"].append("execute")
    with pytest.raises(bootstrap.Refused):
        bootstrap.validate_credentials(credential, token, access)


def test_claim_is_durable_and_never_replayed(tmp_path):
    receipt = tmp_path / "receipt.json"
    record = {"status": "claimed", "operation_id": "same-operation"}
    bootstrap.write_exclusive(receipt, bootstrap.canonical(record))
    assert json.loads(receipt.read_text()) == record
    assert receipt.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        bootstrap.write_exclusive(receipt, b"replacement")
    assert json.loads(receipt.read_text()) == record


def test_unit_is_fixed_private_and_does_not_touch_other_services():
    unit = bootstrap.UNIT_TEXT
    assert "SocketBindAllow=ipv4:tcp:18587" in unit
    assert "SocketBindAllow=ipv4:tcp:18588" in unit
    assert "TimeoutStopSec=180" in unit
    assert "LoadCredentialEncrypted=runtime.json:" in unit
    assert "18993" not in unit and "api-quota-dashboard" not in unit
    assert "Restart=no" in unit


@pytest.mark.parametrize("fail_health", [False, True])
def test_apply_claim_precedes_mutation_secrets_are_encrypted_and_failure_keeps_ledger(
    tmp_path, monkeypatch, fail_health
):
    import os
    from types import SimpleNamespace

    value = payload()
    proof = validate(value)
    paths = {
        "BASE": tmp_path / "opt/shared",
        "STATE": tmp_path / "var/shared",
        "RECEIPT": tmp_path / "var/receipt.json",
        "LAUNCHER": tmp_path / "libexec/launcher.py",
        "UNIT": tmp_path / "units/shared.service",
        "CREDENTIAL": tmp_path / "creds/runtime",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(bootstrap, name, path)
    events, encrypted_input = [], []
    monkeypatch.setattr(bootstrap, "preflight", lambda: None)
    monkeypatch.setattr(bootstrap, "root_directory", lambda path: None)
    monkeypatch.setattr(
        bootstrap.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=os.geteuid() or 1000, pw_gid=os.getegid() or 1000),
    )

    def directory(path, uid, gid, mode):
        path.mkdir(mode=mode)

    monkeypatch.setattr(bootstrap, "directory", directory)

    def command(args, *, input_data=None, timeout=30):
        assert paths["RECEIPT"].exists()
        events.append(args)
        if "encrypt" in args:
            encrypted_input.append(input_data)
            return b"encrypted fixture, not plaintext"
        return b""

    monkeypatch.setattr(bootstrap, "command", command)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **kw: events.append(a[0]))

    def health(token):
        assert token == "read-only-fixture"
        if fail_health:
            raise bootstrap.Refused("fixture readiness failure")
        return {"catalog": 200}

    monkeypatch.setattr(bootstrap, "health", health)
    credential = {"provider_keys": {"OPENAI_API_KEY": "fixture-private-value"}}
    result = bootstrap.apply(value, proof, credential, "read-only-fixture", "fixture-binding")
    assert result["status"] == ("needs_reconcile" if fail_health else "complete")
    assert result["provider_calls"] == 0 and paths["STATE"].is_dir()
    assert (paths["STATE"] / "ledger.sqlite3").exists()
    marker = json.loads((paths["STATE"] / "instance.json").read_text())
    assert marker["credential_fingerprint"] == "fixture-binding"
    assert marker["historical_gap"] is True
    assert encrypted_input == [bootstrap.canonical(credential)]
    assert paths["CREDENTIAL"].read_bytes() == b"encrypted fixture, not plaintext"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"fixture-private-value" not in path.read_bytes()
    assert all(
        "18993" not in str(event) and "api-quota-dashboard" not in str(event) for event in events
    )
    assert all("python" not in str(event) for event in events)
    if fail_health:
        assert ["/usr/bin/systemctl", "stop", bootstrap.SERVICE] in events


def test_root_validation_stops_on_real_host_preflight_before_apply(monkeypatch):
    import io
    from types import SimpleNamespace

    value = {"payload": {}, "runtime_credential": {}, "read_token": "fixture"}
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap", "--validate"])
    monkeypatch.setattr(
        bootstrap.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(bootstrap.canonical(value)))
    )
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.socket, "gethostname", lambda: "hp-ubuntu2604-server")
    monkeypatch.setattr(bootstrap.signal, "alarm", lambda _: None)
    monkeypatch.setattr(bootstrap.os, "umask", lambda _: 0o022)
    monkeypatch.setattr(
        bootstrap,
        "validate_payload",
        lambda *_: {
            "files": {"config/access.json": b"{}", "config/gateway.json": b'{"targets":[]}'}
        },
    )
    monkeypatch.setattr(bootstrap, "validate_credentials", lambda *_, **__: "fixture-binding")

    def preflight():
        raise bootstrap.Refused("existing_state_requires_reconcile")

    monkeypatch.setattr(bootstrap, "preflight", preflight)
    with pytest.raises(bootstrap.Refused, match="existing_state_requires_reconcile"):
        bootstrap.main()


def test_expanded_credentials_are_bound_to_enabled_targets_only():
    credential = {
        "schema": 1,
        "instance_id": bootstrap.INSTANCE,
        "digest_key": base64.b64encode(b"d" * 32).decode(),
        "queue_key": base64.b64encode(b"q" * 32).decode(),
        "legacy_token": "L" * 48,
        "provider_keys": {
            "CLOUDFLARE_API_TOKEN": "fixture-private-token",
            "CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
        },
    }
    token = "R" * 48
    access = {
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
    gateway = {
        "targets": [
            {
                "enabled": True,
                "provider": "cloudflare",
                "secret_ref": "CLOUDFLARE_API_TOKEN",
                "account_id_ref": "CLOUDFLARE_ACCOUNT_ID",
            }
        ]
    }
    assert bootstrap.validate_credentials(credential, token, access, gateway=gateway)
    credential["provider_keys"]["GROQ_API_KEY"] = "fixture-unneeded-key"
    with pytest.raises(bootstrap.Refused, match="credential_scope"):
        bootstrap.validate_credentials(credential, token, access, gateway=gateway)
