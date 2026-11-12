"""Offline fixed-destination shared publishing; no real credentials or network."""

import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "hp_shared_client_candidate", SCRIPTS / "hp_shared_client.py"
)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


@pytest.fixture
def payload(tmp_path):
    directory = tmp_path / "configuration"
    directory.mkdir()
    documents = {
        "registry.json": {"schema_version": 1, "providers": [], "models": []},
        "gateway.json": {"targets": []},
        "access.json": {"version": 1, "principals": []},
        "observations.json": [],
        "secret-inventory.json": {
            "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
            "verified_at": "2026-11-11T00:00:00+00:00",
            "expires_at": "2026-12-11T00:00:00+00:00",
            "project": "api-quota-broker",
            "config": "dev",
        },
    }
    for name, document in documents.items():
        (directory / name).write_text(json.dumps(document))
    return client.build(directory, "new-release", "old-release", "a" * 64, str(uuid.uuid4()))


def receipt(payload, status="complete"):
    return {
        "status": status,
        "operation_id": payload["operation_id"],
        "release_id": payload["release_id"],
        "release_manifest_sha256": client.validate(payload)["manifest_sha256"],
        "previous": "old-release",
        "previous_manifest_sha256": "a" * 64,
        "current": "new-release",
        "manifest_sha256": client.validate(payload)["manifest_sha256"],
    }


def test_fixed_file_set_and_schema(payload):
    assert payload["schema"] == "hp-quota-shared-release-v1"
    assert client.validate(payload)["files"] == len(client.PATHS)
    assert all(p.startswith(("app/", "config/")) for p in client.PATHS)
    malformed = copy.deepcopy(payload)
    malformed["files"][0]["path"] = "../unit.service"
    with pytest.raises(ValueError):
        client.validate(malformed)


def test_publish_readback_and_never_replay(payload, tmp_path):
    calls = []

    def transport(request):
        calls.append(request["action"])
        if request["action"] == "status":
            return (
                receipt(payload)
                if "operation_id" in request
                else {"status": "ready", "current": "old-release", "manifest_sha256": "a" * 64}
            )
        if request["action"] == "validate":
            return {"status": "validated", **client.validate(payload)}
        return receipt(payload)

    state = tmp_path / "state"
    assert client.publish(payload, state, transport)["status"] == "complete"
    assert calls == ["status", "validate", "publish", "status"]
    with pytest.raises(ValueError):
        client.publish(payload, state, transport)
    assert len(calls) == 4


def test_timeout_keeps_original_intent_and_blocks_new_uuid(payload, tmp_path):
    state = tmp_path / "state"
    calls = []

    def transport(request):
        calls.append(request["action"])
        if request["action"] == "status":
            return {"status": "ready", "current": "old-release", "manifest_sha256": "a" * 64}
        if request["action"] == "validate":
            return {"status": "validated", **client.validate(payload)}
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        client.publish(payload, state, transport)
    intent = json.loads((state / (payload["operation_id"] + ".intent.json")).read_text())
    assert intent["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        client.publish({**payload, "operation_id": str(uuid.uuid4())}, state, transport)
    assert calls == ["status", "validate", "publish"]
    assert (
        client.reconcile(payload["operation_id"], state, lambda _: receipt(payload))["status"]
        == "complete"
    )


def test_remote_pending_blocks_new_operation_without_local_intent(payload, tmp_path):
    with pytest.raises(ValueError):
        client.publish(
            payload,
            tmp_path / "state",
            lambda _: {
                "status": "reconcile_required",
                "current": "old-release",
                "manifest_sha256": "a" * 64,
            },
        )
    assert list((tmp_path / "state").glob("*.intent.json")) == []


def test_response_cannot_echo_private_fields_or_invalid_ids():
    for response in (
        {"status": "ready", "token": "synthetic-sensitive"},
        {"status": "ready", "current": "../private"},
        {"status": "ready", "provider_calls": 1},
    ):
        with pytest.raises(ValueError):
            client.sanitize_response(response)


def test_remote_uses_fixed_doppler_key_ram_identity_and_redacts_errors(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(client, "RUNTIME", runtime)
    names = []
    monkeypatch.setattr(
        client.credentials,
        "get",
        lambda name: (
            names.append(name)
            or "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
        ),
    )
    paths = []

    def run(argv, **kwargs):
        assert argv[-1] == "quota-broker-deploy@192.168.68.67"
        assert kwargs["timeout"] > 360  # wait for the root helper's final receipt
        assert "StrictHostKeyChecking=yes" in argv and "ProxyCommand=none" in argv
        path = Path(argv[argv.index("-i") + 1])
        paths.append(path)
        assert path.parent.parent == runtime and path.stat().st_mode & 0o777 == 0o600
        assert path.read_text().endswith("\n")
        assert "fixture" not in " ".join(argv)
        assert json.loads(kwargs["input"])["schema"] == "hp-quota-shared-maintenance-v1"
        return SimpleNamespace(
            returncode=0, stdout=b'{"status":"ready"}', stderr=b"private ignored"
        )

    monkeypatch.setattr(client.subprocess, "run", run)
    assert client.remote(client.envelope("status")) == {"status": "ready"}
    assert names == ["QUOTA_SHARED_MAINTENANCE_SSH_PRIVATE_KEY"]
    assert all(not path.exists() for path in paths)


def test_rollback_timeout_requires_original_readback(payload, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    proof = client.validate(payload)
    client.write_once(
        state / (payload["operation_id"] + ".intent.json"),
        {
            **{
                k: payload[k]
                for k in (
                    "operation_id",
                    "release_id",
                    "expected_current",
                    "expected_manifest_sha256",
                )
            },
            "manifest_sha256": proof["manifest_sha256"],
            "status": "outcome_unknown",
        },
    )
    calls = []

    def transport(request):
        calls.append(request["action"])
        if request["action"] == "status":
            return receipt(payload)
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        client.rollback(payload["operation_id"], state, transport)
    with pytest.raises(ValueError):
        client.rollback(payload["operation_id"], state, transport)
    assert calls == ["status", "rollback"]
    assert (
        client.reconcile(payload["operation_id"], state, lambda _: receipt(payload, "rolled_back"))[
            "status"
        ]
        == "rolled_back"
    )


def test_successful_rollback_resolves_original_unknown_publish(payload, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    proof = client.validate(payload)
    client.write_once(
        state / (payload["operation_id"] + ".intent.json"),
        {
            **{
                k: payload[k]
                for k in (
                    "operation_id",
                    "release_id",
                    "expected_current",
                    "expected_manifest_sha256",
                )
            },
            "manifest_sha256": proof["manifest_sha256"],
            "status": "outcome_unknown",
        },
    )
    status = [receipt(payload)]

    def transport(request):
        if request["action"] == "rollback":
            status[0] = receipt(payload, "rolled_back")
        return status[0]

    assert client.rollback(payload["operation_id"], state, transport)["status"] == "rolled_back"
    # The old publish intent must no longer block a different, reviewed update.
    client._clear(state)


def preclaim_absent(payload):
    return {
        "status": "not_started",
        "operation_id": payload["operation_id"],
        "current": payload["expected_current"],
        "manifest_sha256": payload["expected_manifest_sha256"],
        "provider_calls": 0,
        "paid_allowed": False,
        "automatic_retry": False,
    }


def store_unknown_intent(state, payload):
    state.mkdir(mode=0o700)
    intent = {
        **{
            k: payload[k]
            for k in ("operation_id", "release_id", "expected_current", "expected_manifest_sha256")
        },
        "manifest_sha256": client.validate(payload)["manifest_sha256"],
        "status": "outcome_unknown",
    }
    client.write_once(state / (payload["operation_id"] + ".intent.json"), intent)


def test_authoritative_not_started_resolves_original_without_automatic_publish(payload, tmp_path):
    state = tmp_path / "state"
    store_unknown_intent(state, payload)
    calls = []

    def transport(request):
        calls.append(request)
        return preclaim_absent(payload)

    result = client.reconcile(payload["operation_id"], state, transport)
    assert result["status"] == "not_started"
    assert calls == [client.envelope("status", operation_id=payload["operation_id"])]
    assert (state / (payload["operation_id"] + ".intent.json")).exists()
    client._clear(state)
    # Original ID stays immutable; a later new publication needs explicit invocation.
    with pytest.raises(ValueError):
        client.publish(payload, state, lambda _: pytest.fail("no replay"))
    # A human/owner action may later invoke a distinct publication explicitly.
    later = {**payload, "operation_id": str(uuid.uuid4())}
    actions = []

    def explicitly_requested(request):
        actions.append(request["action"])
        if request["action"] == "status":
            return (
                receipt(later)
                if "operation_id" in request
                else {
                    "status": "ready",
                    "current": later["expected_current"],
                    "manifest_sha256": later["expected_manifest_sha256"],
                }
            )
        if request["action"] == "validate":
            return {"status": "validated", **client.validate(later)}
        return receipt(later)

    assert client.publish(later, state, explicitly_requested)["status"] == "complete"
    assert actions == ["status", "validate", "publish", "status"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", str(uuid.uuid4())),
        ("current", "different-release"),
        ("manifest_sha256", "f" * 64),
    ],
)
def test_not_started_with_wrong_original_identity_or_baseline_stays_unknown(
    payload, tmp_path, field, value
):
    state = tmp_path / "state"
    store_unknown_intent(state, payload)
    with pytest.raises(ValueError):
        client.reconcile(
            payload["operation_id"], state, lambda _: {**preclaim_absent(payload), field: value}
        )
    assert not list(state.glob("*.reconciled.json"))
    with pytest.raises(ValueError):
        client._clear(state)


def test_incomplete_absence_response_is_not_authoritative(payload):
    response = preclaim_absent(payload)
    response.pop("automatic_retry")
    with pytest.raises(ValueError):
        client.sanitize_response(response)


def test_invalid_semantic_configuration_is_stopped_before_any_transport(payload, tmp_path):
    import base64

    malformed = copy.deepcopy(payload)
    row = next(row for row in malformed["files"] if row["path"] == "config/access.json")
    raw = b'{"version":1,"principals":[{"scopes":["execute"]}]}'
    row.update(
        content_base64=base64.b64encode(raw).decode(), size=len(raw), sha256=client.digest(raw)
    )
    with pytest.raises(ValueError):
        client.validate(malformed)
    calls = []
    with pytest.raises(ValueError):
        client.publish(malformed, tmp_path / "state", lambda request: calls.append(request))
    assert calls == [] and not (tmp_path / "state").exists()


def test_direct_remote_publish_cannot_bypass_local_preflight(payload, monkeypatch):
    import base64

    malformed = copy.deepcopy(payload)
    row = next(row for row in malformed["files"] if row["path"] == "config/registry.json")
    raw = b"{}"
    row.update(
        content_base64=base64.b64encode(raw).decode(), size=len(raw), sha256=client.digest(raw)
    )
    monkeypatch.setattr(
        client.credentials, "get", lambda _: pytest.fail("preflight must precede Doppler")
    )
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("preflight must precede SSH"),
    )
    with pytest.raises(ValueError):
        client.remote(client.envelope("publish", payload=malformed))


def test_cross_file_deadline_failure_precedes_any_remote_status(payload, tmp_path):
    import base64

    from build_shared_runtime_config import build
    from test_shared_runtime_config import NOW, evidence, runtime_documents

    owner, router = evidence()
    generated = build(
        {"schema_version": 1, "providers": [], "models": []}, {"targets": []}, owner, router, NOW
    )
    documents = runtime_documents(generated)
    malformed = copy.deepcopy(payload)
    for row in malformed["files"]:
        if row["path"].startswith("config/"):
            raw = json.dumps(documents[Path(row["path"]).name]).encode()
            row.update(
                content_base64=base64.b64encode(raw).decode(),
                size=len(raw),
                sha256=client.digest(raw),
            )
    with pytest.raises(ValueError, match="eligibility"):
        client.publish(
            malformed,
            tmp_path / "state",
            lambda _: pytest.fail("cross-file mismatch must precede even read-only SSH status"),
        )
    assert not (tmp_path / "state").exists()
