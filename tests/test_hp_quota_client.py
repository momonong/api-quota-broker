"""No external IO: validate package and publication failure boundaries."""

import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("hp_quota_candidate", SCRIPTS / "hp_quota_client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@pytest.fixture
def payload(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    for name in client.META:
        (metadata / name).write_text("{}")
    return client.build(metadata, "new-release", "old-release", "a" * 64, str(uuid.uuid4()))


def test_exact_package_roundtrip_and_no_credential_files(payload):
    proof = client.validate(client.parse(client.canonical(payload)))
    assert proof["files"] == len(client.PATHS)
    assert "app/quota_broker/task_matching.py" in client.PATHS
    assert not any("auth.json" in p or "launcher" in p or ".service" in p for p in client.PATHS)


@pytest.mark.parametrize(
    "change",
    ["traversal", "extra", "missing", "duplicate", "content", "size", "operation", "parent"],
)
def test_malformed_packages_fail_closed(payload, change):
    data = copy.deepcopy(payload)
    if change == "traversal":
        data["files"][0]["path"] = "../unit"
    if change == "extra":
        data["files"][0]["root"] = True
    if change == "missing":
        data["files"].pop()
    if change == "duplicate":
        data["files"].append(data["files"][0])
    if change == "content":
        data["files"][0]["content_base64"] = "AAAA"
    if change == "size":
        data["files"][0]["size"] = True
    if change == "operation":
        data["operation_id"] = "../unsafe"
    if change == "parent":
        data["expected_current"] = "/opt/other"
    with pytest.raises(client.Refused):
        client.validate(data)


def test_duplicate_json_and_symlink_are_rejected(tmp_path):
    with pytest.raises(client.Refused):
        client.parse(b'{"a":1,"a":2}')
    target = tmp_path / "target"
    target.write_text("test")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        client.read_file(link, 100)


def test_publish_requires_baseline_validate_and_readback(payload, tmp_path):
    calls = []
    proof = client.validate(payload)

    def transport(request):
        calls.append(request["action"])
        if request["action"] == "status":
            if "operation_id" not in request:
                return {"current": "old-release", "manifest_sha256": "a" * 64}
            return {
                "status": "complete",
                "operation_id": payload["operation_id"],
                "current": "new-release",
                "manifest_sha256": proof["manifest_sha256"],
                "release_id": "new-release",
                "release_manifest_sha256": proof["manifest_sha256"],
            }
        if request["action"] == "validate":
            return {"status": "validated", **proof}
        return {"operation_id": payload["operation_id"]}

    state = tmp_path / "state"
    assert client.publish(payload, state, transport)["status"] == "complete"
    assert calls == ["status", "validate", "publish", "status"]
    with pytest.raises(client.Refused, match="do_not_replay"):
        client.publish(payload, state, transport)
    assert len(calls) == 4


def test_unknown_transport_retains_intent_and_never_replays(payload, tmp_path):
    calls = []

    def transport(request):
        calls.append(request["action"])
        if request["action"] == "status":
            return {"current": "old-release", "manifest_sha256": "a" * 64}
        if request["action"] == "validate":
            return {"status": "validated", **client.validate(payload)}
        raise TimeoutError("synthetic")

    state = tmp_path / "state"
    with pytest.raises(TimeoutError):
        client.publish(payload, state, transport)
    intent = json.loads((state / (payload["operation_id"] + ".intent.json")).read_text())
    assert intent["status"] == "outcome_unknown"
    with pytest.raises(client.Refused):
        client.publish(payload, state, transport)
    assert calls == ["status", "validate", "publish"]
    fresh = {**payload, "operation_id": str(uuid.uuid4())}
    with pytest.raises(client.Refused):
        client.publish(fresh, state, transport)
    assert calls == ["status", "validate", "publish"]


def test_changed_baseline_never_creates_intent(payload, tmp_path):
    state = tmp_path / "state"
    with pytest.raises(client.Refused, match="baseline_changed"):
        client.publish(payload, state, lambda _: {"current": "other"})
    assert list(state.glob("*.intent.json")) == []


def test_missing_scoped_doppler_token_never_uses_ambient_login(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "STATE", tmp_path)
    monkeypatch.setattr(
        client.subprocess, "run", lambda *a, **kw: pytest.fail("no external IO allowed")
    )
    with pytest.raises(OSError):
        client.credentials()


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_doppler_key_is_parseable_by_openssh_and_temporary(tmp_path, monkeypatch, trailing_newline):
    # Doppler can return a multiline secret without its final line terminator.
    # Exercise OpenSSH itself; a string-only fixture missed this integration failure.
    run = client.subprocess.run
    fixture = tmp_path / "synthetic-key"
    generated = run(
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(fixture)],
        capture_output=True,
        check=False,
    )
    assert generated.returncode == 0
    secret = fixture.read_text().rstrip("\n") + ("\n" if trailing_newline else "")
    monkeypatch.setattr(client, "credentials", lambda: secret)
    temporary = client.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        client.tempfile, "TemporaryDirectory", lambda **kw: temporary(dir=tmp_path)
    )
    identities = []

    def ssh(argv, **kwargs):
        assert argv[0] == "/usr/bin/ssh"
        identity = Path(argv[argv.index("-i") + 1])
        identities.append(identity)
        parsed = run(
            ["/usr/bin/ssh-keygen", "-y", "-f", str(identity)],
            capture_output=True,
            check=False,
        )
        assert parsed.returncode == 0
        assert parsed.stdout.split()[:2] == fixture.with_suffix(".pub").read_bytes().split()[:2]
        return SimpleNamespace(returncode=0, stdout=b'{"status":"ready"}', stderr=b"")

    monkeypatch.setattr(client.subprocess, "run", ssh)
    assert client.remote(client.envelope("status")) == {"status": "ready"}
    assert len(identities) == 1 and not identities[0].exists()


def test_shared_lock_prevents_second_operation_while_first_is_inflight(payload, tmp_path):
    state = tmp_path / "state"
    other = {**payload, "operation_id": str(uuid.uuid4())}

    def transport(request):
        with pytest.raises(BlockingIOError):
            client.publish(other, state, lambda _: pytest.fail("second operation sent"))
        return {"current": "drifted"}

    with pytest.raises(client.Refused, match="baseline_changed"):
        client.publish(payload, state, transport)


def test_reconcile_requires_original_identity_release_and_digest(payload, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    operation = payload["operation_id"]
    proof = client.validate(payload)
    client.write_once(
        state / (operation + ".intent.json"),
        {
            "operation_id": operation,
            "release_id": payload["release_id"],
            "manifest_sha256": proof["manifest_sha256"],
            "status": "outcome_unknown",
        },
    )
    receipt = {
        "operation_id": operation,
        "current": payload["release_id"],
        "release_id": payload["release_id"],
        "release_manifest_sha256": proof["manifest_sha256"],
        "status": "complete",
        "manifest_sha256": proof["manifest_sha256"],
    }
    for key in ("operation_id", "release_id", "release_manifest_sha256", "status"):
        with pytest.raises(client.Refused):
            client.reconcile(operation, state, lambda _, k=key: {**receipt, k: "unknown"})
    assert not (state / (operation + ".reconciled.json")).exists()
    assert client.reconcile(operation, state, lambda _: receipt)["status"] == "complete"
    assert (state / (operation + ".intent.json")).exists()
    # New operations now reach baseline checking; original UUID never resubmits.
    with pytest.raises(client.Refused, match="baseline_changed"):
        client.publish({**payload, "operation_id": str(uuid.uuid4())}, state, lambda _: {})


def test_misfiled_complete_receipt_does_not_clear_other_intent(payload, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    operation = payload["operation_id"]
    intent = {"operation_id": operation, "release_id": "new-release", "manifest_sha256": "b" * 64}
    client.write_once(state / (operation + ".intent.json"), intent)
    client.write_once(
        state / (operation + ".result.json"),
        {**intent, "operation_id": str(uuid.uuid4()), "status": "complete"},
    )
    with pytest.raises(client.Refused, match="do_not_replay"):
        client.publish(
            {**payload, "operation_id": str(uuid.uuid4())},
            state,
            lambda _: pytest.fail("must stop before network"),
        )


@pytest.mark.parametrize("scope", ["correct", "wrong_config", "extra_secret"])
def test_doppler_fetch_is_scoped_fresh_and_does_not_leak_token(scope, monkeypatch):
    from types import SimpleNamespace

    token = "dp.st.synthetic-service-token"
    monkeypatch.setattr(client, "read_file", lambda *a, **k: token.encode())
    monkeypatch.setattr(client.resource, "setrlimit", lambda *a: None)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert token not in " ".join(argv)
        assert kwargs["env"]["DOPPLER_TOKEN"] == token
        assert kwargs["env"].keys() == {"PATH", "HOME", "DOPPLER_TOKEN"}
        assert "--no-fallback" in argv and "--no-cache" in argv
        assert argv[argv.index("--attempts") + 1] == "1"
        data = {
            "DOPPLER_PROJECT": client.PROJECT,
            "DOPPLER_CONFIG": client.CONFIG,
            client.SECRET: "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n",
        }
        if scope == "wrong_config":
            data["DOPPLER_CONFIG"] = "prd_hp"
        if scope == "extra_secret":
            data["HP_DEPLOY_SSH_PRIVATE_KEY"] = "synthetic"
        return SimpleNamespace(returncode=0, stdout=json.dumps(data).encode())

    monkeypatch.setattr(client.subprocess, "run", run)
    if scope == "correct":
        assert client.credentials().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    else:
        with pytest.raises(client.Refused):
            client.credentials()
    assert len(calls) == 1


@pytest.mark.parametrize("terminal", ["complete", "rolled_back"])
def test_reconcile_uses_operation_proof_after_later_release(payload, tmp_path, terminal):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    op = payload["operation_id"]
    sha = client.validate(payload)["manifest_sha256"]
    intent = {
        "operation_id": op,
        "release_id": "new-release",
        "manifest_sha256": sha,
        "expected_current": "old-release",
        "expected_manifest_sha256": "a" * 64,
    }
    client.write_once(state / (op + ".intent.json"), intent)
    receipt = {
        "operation_id": op,
        "status": terminal,
        "current": "later-release",
        "manifest_sha256": "c" * 64,
        "release_id": "new-release",
        "release_manifest_sha256": sha,
        "previous": "old-release",
        "previous_manifest_sha256": "a" * 64,
    }
    result = client.reconcile(op, state, lambda _: receipt)
    assert result["status"] == terminal
    if terminal == "rolled_back":
        with pytest.raises(client.Refused, match="baseline_changed"):
            client.publish({**payload, "operation_id": str(uuid.uuid4())}, state, lambda _: {})
