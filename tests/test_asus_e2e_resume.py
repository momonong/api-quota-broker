"""Real files, service identity and Gateway ledger in an isolated user namespace.

Run these tests as namespace root, never as host root. No real HTTP/provider,
systemd, credential, or deployed database is accessed.
"""

import copy
import importlib.util
import io
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import ProviderPhaseTimeout, official_request

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires isolated mapped namespace root")


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owned(path, raw, mode=0o600, gid=0):
    path.write_bytes(raw)
    os.chown(path, 0, gid)
    path.chmod(mode)
    return path


def fixture(tmp_path, monkeypatch, fault=None):
    # Refuse host root: this suite is limited to a mapped unprivileged namespace.
    assert Path("/proc/self/uid_map").read_text().split()[1] != "0"
    for path in (tmp_path, tmp_path.parent, tmp_path.parent.parent):
        path.chmod(0o755)
    r, e = load("resume_representative_e2e"), load("representative_e2e")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    configdir = tmp_path / "config"
    configdir.mkdir(mode=0o750)
    os.chown(configdir, 0, 982)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    originaldir, recoverydir = backups / "original", backups / "recovery"
    originaldir.mkdir(mode=0o700)
    recoverydir.mkdir(mode=0o700)
    marker = backups / "old-marker.json"
    original = owned(originaldir / "gateway.original.json", b'{"targets":[]}\n')
    config = owned(configdir / "gateway.json", original.read_bytes(), 0o640, 982)
    old = {"post_attempted": False, "original_config_backup": str(original)}
    owned(marker, json.dumps(old, sort_keys=True).encode())
    old_sha = e.digest(marker.read_bytes())
    recovery = recoverydir / "recovery.json"
    candidate = owned(recoverydir / "gateway.failed.json", b"public preserved candidate fixture")
    candidate_sha = e.digest(candidate.read_bytes())
    receipt_value = copy.deepcopy(r.EXPECTED_RECOVERY)
    receipt_value.update(
        journal=str(recovery), preserved_candidate=str(candidate), marker_sha256=old_sha
    )
    receipt_value["current_config"]["sha256"] = candidate_sha
    owned(recovery, json.dumps(receipt_value, sort_keys=True).encode())
    claim, receipt = backups / "new.claim.json", backups / "new.json"
    for name, value in {
        "BACKUPS": backups,
        "OLD_MARKER": marker,
        "OLD_SHA": old_sha,
        "ORIGINAL": original,
        "RECOVERY": recovery,
        "CANDIDATE_SHA": candidate_sha,
        "CLAIM": claim,
        "RECEIPT": receipt,
        "EXPECTED_RECOVERY": receipt_value,
        "__file__": str(private / "resume_representative_e2e.py"),
    }.items():
        monkeypatch.setattr(r, name, value)
    for name, value in {
        "CONFIG": config,
        "BACKUPS": backups,
        "MARKER": marker,
        "DB": tmp_path / "ledger.sqlite",
    }.items():
        monkeypatch.setattr(e, name, value)
    template = (DEPLOY / "representative_e2e.disabled.json").read_bytes()
    owned(private / "representative_e2e.disabled.json", template)
    credentials = configdir / "credentials"
    credentials.mkdir(mode=0o700)
    metadata = {
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "credential_policy": "host",
        "human_dashboard_attested": True,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    owned(credentials / "doppler-metadata.json", json.dumps(metadata).encode())
    client = owned(private / "fixture_client", b"fixture-client-" + b"x" * 32, 0o400)
    unit = owned(
        private / "fixture_unit", (DEPLOY / "api-quota-broker.service").read_bytes(), 0o644
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/" + r.UNIT)
    real_read = e.read_file

    def read(path, uid, gid, mode, bound):
        if str(path) == "/etc/systemd/system/api-quota-broker.service":
            path = unit
        elif str(path) == "/run/credentials/" + r.UNIT + "/client_token":
            path = client
        return real_read(path, uid, gid, mode, bound)

    monkeypatch.setattr(e, "read_file", read)
    monkeypatch.setattr(r.os, "readlink", lambda _: r.RELEASE)
    monkeypatch.setattr(e.RootOps, "save", lambda *_: pytest.fail("legacy save must never run"))
    ops = r.build_ops(e)()
    events, provider_calls, secret_reads = [], [], []
    real_fsync = os.fsync

    def synced(fd):
        real_fsync(fd)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            events.append("fsync-file")
        elif info.st_ino == backups.stat().st_ino:
            events.append("fsync-backups")

    monkeypatch.setattr(r.os, "fsync", synced)
    state = {
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "User": "api-quota-broker",
        "Group": "api-quota-broker",
        "NRestarts": "0",
        "MemoryHigh": "268435456",
        "MemoryMax": "402653184",
        "MemorySwapMax": "0",
        "LimitCORE": "0",
        "MainPID": "201041",
        "ExecMainStartTimestampMonotonic": "587160836703",
    }
    ops.service = lambda _: dict(state)
    ops.orderflow = lambda: {
        "active": True,
        "http_status": 200,
        "n_restarts": 0,
        "pid": 172058,
        "start_monotonic": 515137840877,
    }
    gateway = Gateway(e.DB, (), b"public-fixture-digest" * 2, lambda _: "unused")
    with sqlite3.connect(e.DB) as con:
        con.execute("CREATE TABLE queue_jobs(id TEXT)")
        con.execute("CREATE TABLE queue_attempts(id TEXT)")
    os.chown(e.DB, 995, 982)
    e.DB.chmod(0o600)

    def provider(url, headers, payload, timeout):
        provider_calls.append(url)
        events.append("provider")
        assert "saved:post" in events and events.index("saved:post") < events.index("provider")
        intent_index = events.index("saved:post")
        assert events[intent_index - 2 : intent_index] == ["fsync-file", "fsync-backups"]
        persisted = json.loads(receipt.read_bytes())
        assert persisted["post_attempted"] is True and persisted["phase"] == "post"
        assert json.loads(claim.read_bytes())["request_key"] == r.KEY
        assert url == "https://api.groq.com/openai/v1/chat/completions" and timeout == 30
        assert (
            payload
            == official_request(
                "groq", e.MODEL, "fixture", "fixture-key", e.TASK["input"], 512, None, None
            )[2]
        )
        if fault == "provider_timeout":
            raise ProviderPhaseTimeout("timeout_before_headers") from ValueError(
                "fixture-private-error"
            )
        if fault == "provider_os_error":
            raise OSError("fixture-private-error fixture-key")
        return (
            200,
            {},
            json.dumps(
                {
                    "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 76, "completion_tokens": 18},
                }
            ).encode(),
        )

    def command(argv):
        nonlocal gateway
        events.append(argv[1])
        assert argv == ["/usr/bin/systemctl", argv[1], e.SERVICE]
        if argv[1] == "stop":
            state.update(ActiveState="inactive", SubState="dead")
            return b""
        assert argv[1] == "restart"
        gateway = Gateway(
            e.DB,
            load_gateway_config(config),
            b"public-fixture-digest" * 2,
            lambda name: secret_reads.append(name) or "fixture-key",
            transport=provider,
        )
        state.update(ActiveState="active", SubState="running")
        return b""

    ops.command = command

    def http(method, path, body=None):
        events.append(method + " " + path)
        if method == "GET" and path == "/v1/diagnostics":
            return 200, gateway.diagnostics()
        if method == "GET":
            assert path == "/v1/tasks/" + r.KEY
            if fault == "status":
                raise ValueError("fixture-private-error")
            return 200, gateway.status(r.KEY)
        assert (
            method == "POST"
            and path == "/v1/tasks"
            and body == e.TASK
            and body["request_key"] == r.KEY
        )
        return 200, gateway.run(body)

    ops.http = http
    actual_ready, actual_save, actual_write, actual_replace = (
        ops.ready,
        ops.save,
        ops.write,
        r.os.replace,
    )
    fired = set()

    def once(name):
        if fault == name and name not in fired:
            fired.add(name)
            raise OSError("fixture-private-error")

    def ready(count):
        once("ready" if count else "restore_ready")
        actual_ready(count)

    def save(record):
        once("save_" + record["phase"])
        actual_save(record)
        events.append("saved:" + record["phase"])

    def write(path, raw, mode, gid):
        if path == claim:
            once("claim_write")
        if path == receipt:
            once("receipt_write")
        if path.name.startswith(".groq-resume-config-"):
            once("restore_write" if raw == original.read_bytes() else "candidate_write")
        actual_write(path, raw, mode, gid)
        if (
            fault == "candidate_mode"
            and path.name.startswith(".groq-resume-config-")
            and raw != original.read_bytes()
        ):
            path.chmod(0o600)

    def replace(source, target):
        if target == config:
            once(
                "restore_replace"
                if source.read_bytes() == original.read_bytes()
                else "candidate_replace"
            )
        actual_replace(source, target)

    ops.ready, ops.save, ops.write = ready, save, write
    monkeypatch.setattr(r.os, "replace", replace)
    actual_sync = r.sync_directory

    def sync(path):
        actual_sync(path)
        if path == configdir and config.read_bytes() != original.read_bytes():
            once("candidate_sync")

    monkeypatch.setattr(r, "sync_directory", sync)
    if fault == "claim_exists":
        owned(claim, b"preserve foreign claim")
    elif fault == "receipt_exists":
        owned(receipt, b"preserve foreign receipt")
    elif fault == "claim_symlink":
        claim.symlink_to(original)
    elif fault == "config_mode":
        config.chmod(0o600)
    elif fault == "marker_hardlink":
        os.link(marker, backups / "marker.alias")
    before = {
        p: (p.read_bytes(), r.fingerprint(p, p.read_bytes()))
        for p in (marker, original, recovery, candidate)
    }
    return r, e, ops, config, before, events, provider_calls, secret_reads


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "claim_exists",
        "receipt_exists",
        "claim_symlink",
        "config_mode",
        "marker_hardlink",
        "claim_write",
        "receipt_write",
        "save_temporary_target",
        "candidate_write",
        "candidate_mode",
        "candidate_replace",
        "candidate_sync",
        "ready",
        "save_post",
        "provider_timeout",
        "provider_os_error",
        "status",
        "restore_write",
        "restore_replace",
        "restore_ready",
        "save_complete",
    ],
)
def test_real_files_all_phase_failures_preserve_history_and_never_replay(
    tmp_path, monkeypatch, fault
):
    r, e, ops, config, before, events, calls, reads = fixture(tmp_path, monkeypatch, fault)
    mask = os.umask(0o077)
    try:
        result = r.run(e, ops)
        if r.CLAIM.exists():
            assert stat.S_IMODE(r.CLAIM.stat().st_mode) == 0o600
        assert result["status"] == ("passed" if fault is None else "failed")
        for path, (raw, identity) in before.items():
            assert path.read_bytes() == raw and r.fingerprint(path, raw) == identity
        sent = fault in (
            None,
            "provider_timeout",
            "provider_os_error",
            "status",
            "restore_write",
            "restore_replace",
            "restore_ready",
            "save_complete",
        )
        assert len(calls) == int(sent) and reads == (["GROQ_API_KEY"] if sent else [])
        assert "fixture-key" not in json.dumps(result) and "fixture-private" not in json.dumps(
            result
        )
        assert '"answer"' not in json.dumps(result) and e.TASK["input"] not in json.dumps(result)
        if fault in ("restore_write", "restore_replace", "restore_ready"):
            assert result["failure_stop_completed"]
            assert [event for event in events if event in {"restart", "stop"}][-1] == "stop"
        elif ops.changed:
            assert result["restored_original_config"]
            assert (
                config.read_bytes() == b'{"targets":[]}\n'
                and stat.S_IMODE(config.stat().st_mode) == 0o640
            )
        if sent:
            for forbidden in (b"fixture-key", b"fixture-private", e.TASK["input"].encode()):
                assert forbidden not in e.DB.read_bytes()
            with sqlite3.connect(e.DB) as con:
                assert con.execute("SELECT request_key FROM gateway_attempts").fetchall() == [
                    (r.KEY,)
                ]
                assert con.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        if r.CLAIM.exists() and not r.CLAIM.is_symlink():
            before_replay = {p: p.read_bytes() for p in (r.CLAIM, r.RECEIPT) if p.exists()}
            repeated = r.run(e, r.build_ops(e)())
            assert repeated["status"] == "failed" and not repeated["post_attempted"]
            assert len(calls) == int(sent)
            assert all(p.read_bytes() == raw for p, raw in before_replay.items())
    finally:
        os.umask(mask)


@pytest.mark.parametrize(
    "change",
    ["false", "bool_as_int", "extra", "missing", "posted", "recovery_hardlink", "recovery_symlink"],
)
def test_history_gate_requires_exact_success_receipt_and_unposted_old_marker(
    tmp_path, monkeypatch, change
):
    r, e, ops, config, _, events, calls, _ = fixture(tmp_path, monkeypatch)
    value = json.loads(r.RECOVERY.read_bytes())
    if change == "false":
        value["marker_preserved"] = False
    elif change == "bool_as_int":
        value["provider_calls"] = False
    elif change == "extra":
        value["unverified"] = True
    elif change == "missing":
        value.pop("ledger_unchanged")
    elif change == "posted":
        old = json.loads(r.OLD_MARKER.read_bytes())
        old["post_attempted"] = True
        r.OLD_MARKER.write_text(json.dumps(old))
        monkeypatch.setattr(r, "OLD_SHA", e.digest(r.OLD_MARKER.read_bytes()))
    elif change == "recovery_hardlink":
        os.link(r.RECOVERY, r.RECOVERY.parent / "alias")
    elif change == "recovery_symlink":
        alias = r.RECOVERY.parent / "original.json"
        r.RECOVERY.rename(alias)
        r.RECOVERY.symlink_to(alias)
    if change in {"false", "bool_as_int", "extra", "missing"}:
        r.RECOVERY.write_text(json.dumps(value))
    result = r.run(e, ops)
    assert result["status"] == "failed" and not events and not calls and not r.CLAIM.exists()
    assert config.read_bytes() == b'{"targets":[]}\n'


@pytest.mark.parametrize("tamper", ["history", "claim", "foreign_config"])
def test_post_rechecks_fingerprints_without_overwriting_tampered_evidence(
    tmp_path, monkeypatch, tamper
):
    r, e, ops, config, _, events, calls, _ = fixture(tmp_path, monkeypatch)
    actual = ops.enable

    def enable():
        actual()
        if tamper == "history":
            r.RECOVERY.write_bytes(b"preserve foreign journal")
        elif tamper == "claim":
            r.CLAIM.write_bytes(b"preserve foreign claim")
        else:
            config.write_bytes(b"preserve foreign config")

    ops.enable = enable
    result = r.run(e, ops)
    assert result["status"] == "failed" and result["post_attempted"] and not calls
    assert result["failure_stop_completed"] and "stop" in events
    if tamper == "foreign_config":
        assert config.read_bytes() == b"preserve foreign config"
    else:
        assert config.read_bytes() == b'{"targets":[]}\n'
        path = r.RECOVERY if tamper == "history" else r.CLAIM
        assert path.read_bytes().startswith(b"preserve foreign")


def test_real_service_uid_reads_group0640_but_rejects0600(tmp_path, monkeypatch):
    r, e, _, config, _, _, _, _ = fixture(tmp_path, monkeypatch)
    r.service_can_read(config, e.EMPTY_SHA)
    config.chmod(0o600)
    with pytest.raises(r.ResumeError):
        r.service_can_read(config, e.EMPTY_SHA)
    config.chmod(0o640)
    os.chown(config, 0, 0)
    with pytest.raises(r.ResumeError):
        r.service_can_read(config, e.EMPTY_SHA)


def test_ledger_read_failure_still_restores_real_config(tmp_path, monkeypatch):
    r, e, ops, config, _, _, calls, _ = fixture(tmp_path, monkeypatch)
    actual, actual_post = ops.database, ops.post
    posted, failed = False, False

    def post():
        nonlocal posted
        result = actual_post()
        posted = True
        return result

    def database():
        nonlocal failed
        if posted and not failed:
            failed = True
            raise ValueError("fixture-private-error")
        return actual()

    ops.post, ops.database = post, database
    result = r.run(e, ops)
    assert result["status"] == "failed" and result["restored_original_config"]
    assert len(calls) == 1 and config.read_bytes() == b'{"targets":[]}\n'


def test_reused_http_allowlist_uses_new_key_for_post_and_status(tmp_path, monkeypatch):
    r, e, _, _, _, _, _, _ = fixture(tmp_path, monkeypatch)
    requests = []
    old_key = "asus-v01-groq-e2e-2026-10-04-once"

    class Connection:
        sock = None

        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 18084) and timeout in {5, 45}

        def request(self, method, path, body, headers):
            requests.append((method, path, json.loads(body) if body else None))

        def getresponse(self):
            raw = b'{"request_key":"' + r.KEY.encode() + b'"}'
            stream = io.BytesIO(raw)
            return SimpleNamespace(
                status=200, length=0, isclosed=lambda: stream.tell() == len(raw), read1=stream.read
            )

        def close(self):
            pass

    monkeypatch.setattr(e.http.client, "HTTPConnection", Connection)
    ops = r.build_ops(e)()
    ops.client = "public-fixture-client"
    assert ops.http("POST", "/v1/tasks", e.TASK)[1]["request_key"] == r.KEY
    assert ops.http("GET", "/v1/tasks/" + r.KEY)[1]["request_key"] == r.KEY
    with pytest.raises(e.GateError):
        ops.http("GET", "/v1/tasks/" + old_key)
    with pytest.raises(e.GateError):
        ops.http("POST", "/v1/tasks", {**e.TASK, "request_key": old_key})
    assert len(requests) == 2 and requests[0][2]["request_key"] == r.KEY
