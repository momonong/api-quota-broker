"""Offline verification of one-shot orchestration, real ledger joins and recovery."""

import copy
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def restore_process_umask():
    # The real root entry intentionally tightens process umask until exit.
    # In-process fault fixtures must not leak that setting to later modules.
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import official_request

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"
spec = importlib.util.spec_from_file_location(
    "tested_representative_e2e", DEPLOY / "representative_e2e.py"
)
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


def completed():
    return {
        **{k: e2e.TASK[k] for k in ("request_key", "provider", "model")},
        "state": "completed",
        "http_status": 200,
        "error_code": None,
        "reported_input_tokens": 76,
        "reported_output_tokens": 18,
        "latency_ms": 300,
        "finish_reason": "stop",
        "response_truncated": False,
        "ledger_state": "completed",
        "ledger_basis": "settled_provider_usage",
        "answer": "READY",
        "secret_reflection": "fixture-private-token",
    }


def ledger():
    return {
        "quick_check": "ok",
        "rows": dict(zip(e2e.TABLES, (1, 1, 1, 3, 0, 0), strict=True)),
        "matching_attempts": 1,
        "attempt_states": ["completed"],
        "charges": [{"metric": "requests", "amount": 1}] * 2
        + [{"metric": "input_tokens", "amount": 76}],
    }


class FixtureOps:
    def __init__(self, fault=None):
        self.changed = self.claimed = False
        self.fault = fault
        self.events, self.saved = [], []
        self.result = completed()
        self.rows = ledger()

    def event(self, name):
        self.events.append(name)
        if name == self.fault:
            raise RuntimeError("fixture-private-token fixture-private-answer")

    def preflight(self):
        self.event("preflight")
        if self.claimed:
            raise e2e.GateError
        return {"empty_authenticated_targets": True, "empty_ledger": True}

    def attest(self):
        self.event("attest")

    def claim(self, record):
        self.claimed = True
        self.event("claim")

    def save(self, record):
        self.event("save:" + record["phase"])
        self.saved.append(copy.deepcopy(record))

    def enable(self):
        self.changed = True
        self.event("enable")

    def post(self):
        self.event("post")
        assert self.saved[-1]["post_attempted"] is True
        return self.result

    def status(self):
        self.event("status")
        return e2e.projected(self.result)

    def database(self):
        self.event("database")
        return self.rows

    def restore(self):
        self.event("restore")
        return {
            "empty_targets": True,
            "active_enabled": True,
            "orderflow_unchanged": True,
            "ledger": self.rows,
        }

    def stop(self):
        self.event("stop")


def test_success_once_saved_intent_restored_and_no_raw_data():
    ops = FixtureOps()
    result = e2e.run(ops)
    assert result["status"] == "passed"
    assert ops.events.count("post") == ops.events.count("restore") == 1
    assert "stop" not in ops.events
    raw = json.dumps([result, ops.saved])
    assert "fixture-private" not in raw and '"answer"' not in raw and e2e.TASK["input"] not in raw
    replay = e2e.run(ops)
    assert replay["status"] == "failed"
    assert ops.events.count("post") == 1


@pytest.mark.parametrize(
    "fault,post,restore",
    [
        ("preflight", 0, 0),
        ("attest", 0, 0),
        ("claim", 0, 0),
        ("save:temporary_target", 0, 0),
        ("enable", 0, 1),
        ("save:post", 0, 1),
        ("post", 1, 1),
        ("save:ledger_verify", 1, 1),
        ("status", 1, 1),
        ("database", 1, 1),
        ("restore", 1, 1),
        ("save:complete", 1, 1),
    ],
)
def test_phase_failures_never_retry_and_restore_after_change(fault, post, restore):
    ops = FixtureOps(fault)
    result = e2e.run(ops)
    assert result["status"] == "failed"
    assert ops.events.count("post") == post and ops.events.count("restore") == restore
    assert "fixture-private" not in json.dumps([result, ops.saved])
    assert ("stop" in ops.events) == (fault == "restore")


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "unknown"),
        ("state", "completed_usage_unknown"),
        ("finish_reason", "length"),
        ("response_truncated", True),
        ("answer", ""),
        ("reported_input_tokens", None),
        ("reported_output_tokens", True),
        ("ledger_state", "unknown"),
        ("ledger_basis", "held_estimate"),
        ("provider", "nvidia"),
        ("model", "wrong"),
        ("request_key", "wrong"),
    ],
)
def test_incomplete_or_wrong_identity_preserves_ledger_and_restores(field, value):
    ops = FixtureOps()
    ops.result[field] = value
    before = copy.deepcopy(ops.rows)
    result = e2e.run(ops)
    assert result["status"] == "failed" and result["restored_original_config"]
    assert ops.events.count("post") == 1 and ops.rows == before


@pytest.mark.parametrize(
    "fault",
    [
        "missing_charge",
        "wrong_amount",
        "extra_task",
        "unknown_attempt",
        "status_mismatch",
        "restore_mismatch",
        "stop_failure",
    ],
)
def test_verdict_requires_exact_ledger_status_and_restore(fault):
    ops = FixtureOps()
    if fault == "missing_charge":
        ops.rows["charges"].pop()
    elif fault == "wrong_amount":
        ops.rows["charges"][-1]["amount"] += 1
    elif fault == "extra_task":
        ops.rows["rows"]["gateway_tasks"] += 1
    elif fault == "unknown_attempt":
        ops.rows["attempt_states"] = ["unknown"]
    elif fault == "status_mismatch":
        ops.status = lambda: {**e2e.projected(ops.result), "reported_output_tokens": 0}
    elif fault == "restore_mismatch":
        ops.restore = lambda: {"ledger": {**ops.rows, "matching_attempts": 0}}
    else:
        ops.fault = "restore"
        ops.stop = lambda: (_ for _ in ()).throw(RuntimeError("fixture-private-token"))
    result = e2e.run(ops)
    assert result["status"] == "failed" and ops.events.count("post") == 1
    assert "fixture-private" not in json.dumps(result)
    if fault == "stop_failure":
        assert result["failure_stop_completed"] is False


def enabled_config(tmp_path):
    path = DEPLOY / "representative_e2e.disabled.json"
    raw = path.read_bytes()
    assert e2e.digest(raw) == e2e.TEMPLATE_SHA
    disabled = load_gateway_config(path)
    assert len(disabled) == 1 and not disabled[0].enabled and not disabled[0].free_eligible
    value = json.loads(raw)
    now = datetime.now(UTC)
    value["targets"][0].update(
        enabled=True,
        free_eligible=True,
        verified_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    candidate = tmp_path / "enabled-fixture.json"
    candidate.write_text(json.dumps(value))
    return load_gateway_config(candidate)


def test_actual_gateway_fixture_settles_correct_join_and_usage(tmp_path, monkeypatch):
    targets = enabled_config(tmp_path)
    assert targets[0].max_output_tokens == 512 and targets[0].concurrency_limit == 1
    path = tmp_path / "ledger.sqlite"
    calls, reads = [], []

    def provider(url, headers, payload, timeout):
        calls.append(url)
        assert url == "https://api.groq.com/openai/v1/chat/completions" and timeout == 30
        assert (
            payload
            == official_request(
                "groq", e2e.MODEL, "fixture", "fixture-key", e2e.TASK["input"], 512, None, None
            )[2]
        )
        return (
            200,
            {},
            json.dumps(
                {
                    "id": "chatcmpl-fixture",
                    "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 76, "completion_tokens": 18},
                }
            ).encode(),
        )

    gateway = Gateway(
        path,
        targets,
        b"fixture-digest-key" * 2,
        lambda name: reads.append(name) or "fixture-key",
        transport=provider,
    )
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE queue_jobs(id TEXT)")
        con.execute("CREATE TABLE queue_attempts(id TEXT)")
    path.chmod(0o600)
    ops = FixtureOps()
    ops.post = lambda: gateway.run(e2e.TASK)
    ops.status = lambda: e2e.projected(gateway.status(e2e.KEY))
    monkeypatch.setattr(e2e, "DB", path)
    real_lstat = Path.lstat

    def ledger_stat(p):
        info = real_lstat(p)
        if p != path:
            return info
        return SimpleNamespace(st_uid=995, st_gid=982, st_mode=info.st_mode, st_nlink=info.st_nlink)

    monkeypatch.setattr(Path, "lstat", ledger_stat)
    root = e2e.RootOps()
    ops.database = root.database
    ops.restore = lambda: {
        "ledger": root.database(),
        "empty_targets": True,
        "active_enabled": True,
        "orderflow_unchanged": True,
    }
    result = e2e.run(ops)
    assert result["status"] == "passed"
    assert calls == ["https://api.groq.com/openai/v1/chat/completions"] and reads == [
        "GROQ_API_KEY"
    ]
    with sqlite3.connect(path) as con:
        assert (
            con.execute("SELECT request_key FROM reservations").fetchone()[0]
            == "gw:" + e2e.KEY + ":0"
        )
    assert "fixture-key" not in json.dumps(result)


def test_default_and_nonroot_apply_never_touch_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["representative_e2e.py"])
    monkeypatch.setattr(e2e.RootOps, "preflight", lambda _: pytest.fail("plan mutated state"))
    assert e2e.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert (
        plan["actual_credential_reads"]
        == plan["actual_provider_calls"]
        == plan["actual_service_changes"]
        == 0
    )
    monkeypatch.undo()
    monkeypatch.setattr(e2e.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(Path, "lstat", lambda _: pytest.fail("nonroot private file read"))
    with pytest.raises(e2e.GateError):
        e2e.RootOps().preflight()


@pytest.mark.parametrize(
    "fault", ["mode", "symlink", "parent_symlink", "hardlink", "oversize", "empty"]
)
def test_private_read_rejects_unsafe_file(tmp_path, fault):
    path = tmp_path / "private"
    path.write_bytes(b"fixture-key")
    path.chmod(0o600)
    if fault == "mode":
        path.chmod(0o644)
    elif fault == "hardlink":
        os.link(path, tmp_path / "alias")
    elif fault == "symlink":
        path = tmp_path / "alias"
        path.symlink_to(tmp_path / "private")
    elif fault == "parent_symlink":
        alias = tmp_path / "parent"
        alias.symlink_to(tmp_path, target_is_directory=True)
        path = alias / "private"
    elif fault == "empty":
        path.write_bytes(b"")
    with pytest.raises((e2e.GateError, OSError)):
        e2e.read_file(path, os.getuid(), os.getgid(), 0o600, 4 if fault == "oversize" else 32)


def test_restore_refuses_foreign_config_before_writing(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"foreign":true}')
    monkeypatch.setattr(e2e, "CONFIG", path)
    monkeypatch.setattr(e2e, "read_file", lambda p, *_: p.read_bytes())
    ops = e2e.RootOps()
    ops.temporary_sha = "0" * 64
    ops.replace_config = lambda _: pytest.fail("overwrote foreign config")
    with pytest.raises(e2e.GateError):
        ops.restore()
    assert path.read_bytes() == b'{"foreign":true}'


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_ambiguous_json_rejected(raw):
    with pytest.raises(e2e.GateError):
        e2e.strict_json(raw)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/v1/tasks", {**e2e.TASK, "max_attempts": 2}),
        ("GET", "/v1/admin/targets", None),
        ("POST", "https://api.groq.com/", e2e.TASK),
    ],
)
def test_http_rejects_unapproved_requests_before_connection(monkeypatch, method, path, body):
    monkeypatch.setattr(
        e2e.http.client, "HTTPConnection", lambda *_a, **_k: pytest.fail("opened connection")
    )
    with pytest.raises(e2e.GateError):
        e2e.RootOps().http(method, path, body)


def test_atomic_backup_marker_and_restore_keep_ledger_untouched(tmp_path, monkeypatch):
    config = tmp_path / "gateway.json"
    original = b'{"targets":[]}\n'
    config.write_bytes(original)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    marker = backups / "once.json"
    monkeypatch.setattr(e2e, "CONFIG", config)
    monkeypatch.setattr(e2e, "BACKUPS", backups)
    monkeypatch.setattr(e2e, "MARKER", marker)
    monkeypatch.setattr(e2e, "EMPTY_SHA", e2e.digest(original))
    monkeypatch.setattr(e2e.os, "fchown", lambda *_: None)
    monkeypatch.setattr(e2e, "read_file", lambda p, *_: p.read_bytes())
    ops = e2e.RootOps()
    ops.original = original
    ops.before_orderflow = {"active": True}
    record = {"phase": "claim", "post_attempted": False}
    ops.claim(record)
    backup = ops.backup / "gateway.original.json"
    assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600
    assert marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        ops.write(marker, b"overwrite", 0o600, 0)
    temporary = b'{"targets":[{"enabled":true}]}'
    ops.temporary_sha = e2e.digest(temporary)
    ops.replace_config(temporary)
    events = []
    ops.command = lambda argv: events.append(argv)
    ops.ready = lambda count: events.append(count)
    ops.service = lambda _: {"ActiveState": "active", "UnitFileState": "enabled"}
    ops.orderflow = lambda: ops.before_orderflow
    ops.database = lambda: ledger()
    restored = ops.restore()
    assert config.read_bytes() == original and backup.read_bytes() == original
    assert events == [["/usr/bin/systemctl", "restart", e2e.SERVICE], 0]
    assert restored["ledger"] == ledger()


def test_bounded_http_discards_oversized_body_without_secret_output(monkeypatch):
    class Connection:
        sock = None
        length = None

        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 18084, 45)
            self.remaining = 262145
            self.status = 200

        def request(self, method, path, body, headers):
            assert method == "POST" and json.loads(body) == e2e.TASK
            assert headers["Authorization"] == "Bearer fixture-private-token"

        def getresponse(self):
            return self

        def isclosed(self):
            return False

        def read1(self, count):
            size = min(count, self.remaining)
            self.remaining -= size
            return b"x" * size

        def close(self):
            pass

    monkeypatch.setattr(e2e.http.client, "HTTPConnection", Connection)
    ops = e2e.RootOps()
    ops.client = "fixture-private-token"
    with pytest.raises(e2e.GateError):
        ops.http("POST", "/v1/tasks", e2e.TASK)


def preflight_fixture(monkeypatch, fault=None):
    """Match root unit contracts using only public fixture bytes and fake stat data."""
    private = Path("/fixture/bootstrap")
    unit = Path("/etc/systemd/system/api-quota-broker.service")
    credential = Path("/run/credentials/api-quota-broker-e2e.service/client_token")
    template = (DEPLOY / "representative_e2e.disabled.json").read_bytes()
    original = b'{"targets":[]}\n'
    metadata = {
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "credential_policy": "host",
        "human_dashboard_attested": True,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    if fault == "doppler_metadata_scope":
        metadata["access"] = "write"
    if fault == "doppler_metadata_expiry":
        metadata["expires_at"] = datetime.now(UTC).isoformat()
    data = {
        private / "representative_e2e.py": b"public-helper-fixture",
        private / "representative_e2e.disabled.json": template,
        unit: b"unit-fixture",
        e2e.CONFIG: original,
        e2e.CONFIG.parent / "credentials/doppler-metadata.json": json.dumps(metadata).encode(),
        credential: b"x" * 43,
    }
    if fault == "template_hash":
        data[private / "representative_e2e.disabled.json"] = b"{}"
    if fault == "empty_config_hash":
        data[e2e.CONFIG] = b'{"targets":[{}]}'
    if fault == "client_credential_format":
        data[credential] = b"INVALID_NON_SECRET_FIXTURE"
    dirs = {private: (0, 0o700), e2e.CONFIG.parent: (982, 0o750), e2e.BACKUPS: (0, 0o700)}
    unsafe_modes = {
        "bootstrap_directory": private,
        "helper_file": private / "representative_e2e.py",
        "template_file": private / "representative_e2e.disabled.json",
        "installed_unit_file": unit,
        "config_directory": e2e.CONFIG.parent,
        "backups_directory": e2e.BACKUPS,
        "empty_config_file": e2e.CONFIG,
        "doppler_metadata_file": e2e.CONFIG.parent / "credentials/doppler-metadata.json",
        "client_credential_metadata": credential,
    }

    def info(path):
        isdir = path not in data
        gid, mode = (
            dirs.get(path, (0, 0o755))
            if isdir
            else (
                982 if path == e2e.CONFIG else 0,
                0o400
                if path == credential
                else 0o644
                if path == unit
                else 0o640
                if path == e2e.CONFIG
                else 0o600,
            )
        )
        if unsafe_modes.get(fault) == path:
            mode = 0o777
        return SimpleNamespace(
            st_uid=0,
            st_gid=gid,
            st_mode=(stat.S_IFDIR if isdir else stat.S_IFREG) | mode,
            st_nlink=1,
            st_size=len(data.get(path, b"")),
        )

    reads = []

    def read(path, uid, gid, mode, bound):
        reads.append(path)
        i = info(path)
        if (i.st_uid, i.st_gid, stat.S_IMODE(i.st_mode), i.st_nlink) != (uid, gid, mode, 1):
            raise e2e.GateError
        if path == credential and fault == "client_credential_read":
            raise OSError("fixture-private-error")
        assert len(data[path]) <= bound
        return data[path]

    files = {
        Path("/proc/self/cgroup"): b"0::/system.slice/api-quota-broker-e2e.service",
        Path("/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/memory.max"): b"134217728",
        Path("/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/memory.swap.max"): b"0",
        Path("/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/cpu.max"): b"50000 100000",
    }
    for gate, suffix in (
        ("self_cgroup", "/proc/self/cgroup"),
        ("memory_max", "/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/memory.max"),
        ("swap_max", "/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/memory.swap.max"),
        ("cpu_quota", "/sys/fs/cgroup/system.slice/api-quota-broker-e2e.service/cpu.max"),
    ):
        if fault == gate:
            files[Path(suffix)] = b"1" if gate != "cpu_quota" else b"max 100000"
    monkeypatch.setattr(e2e, "__file__", str(private / "representative_e2e.py"))
    monkeypatch.setattr(e2e, "EMPTY_SHA", e2e.digest(original))
    digest = e2e.digest
    monkeypatch.setattr(
        e2e,
        "digest",
        lambda raw: (
            (
                "wrong"
                if fault == "installed_unit_hash"
                else "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
            )
            if raw == b"unit-fixture"
            else digest(raw)
        ),
    )
    monkeypatch.setattr(e2e.os, "geteuid", lambda: 1000 if fault == "root_uid" else 0)
    monkeypatch.setattr(
        e2e.socket,
        "gethostname",
        lambda: "wrong" if fault == "host_identity" else "asus-ubuntu2604-server",
    )
    monkeypatch.setattr(e2e.sys, "stdin", SimpleNamespace(isatty=lambda: fault != "true_tty"))
    monkeypatch.setattr(
        e2e.resource, "getrlimit", lambda _: (0, -1) if fault == "core_limits" else (0, 0)
    )
    monkeypatch.setattr(Path, "read_bytes", lambda p: files[p])
    monkeypatch.setattr(Path, "lstat", info)
    monkeypatch.setattr(Path, "exists", lambda p: p == e2e.MARKER and fault == "once_marker_absent")
    monkeypatch.setattr(
        e2e.os,
        "readlink",
        lambda _: (
            "wrong"
            if fault == "release_selector"
            else "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
        ),
    )
    monkeypatch.setattr(e2e, "read_file", read)
    monkeypatch.setenv(
        "CREDENTIALS_DIRECTORY",
        "wrong"
        if fault == "credential_directory_environment"
        else "/run/credentials/api-quota-broker-e2e.service",
    )
    ops = e2e.RootOps()
    ops.service = lambda _: {
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "User": "api-quota-broker",
        "Group": "api-quota-broker",
        "NRestarts": "1" if fault == "broker_service" else "0",
        "MemorySwapMax": "0",
        "LimitCORE": "0",
    }
    ops.targets = lambda: [{}] if fault == "empty_authenticated_targets" else []
    ops.database = lambda: {"rows": dict.fromkeys(e2e.TABLES, 1 if fault == "empty_ledger" else 0)}

    def orderflow():
        if fault == "orderflow":
            raise e2e.GateError
        return {"active": True}

    ops.orderflow = orderflow
    return ops, reads, credential


@pytest.mark.parametrize("gate", sorted(e2e.PREFLIGHT_GATES - {"metadata_diagnosis_complete"}))
def test_each_preflight_failure_has_static_specific_code(monkeypatch, gate):
    ops, reads, credential = preflight_fixture(monkeypatch, gate)
    result = e2e.run(ops)
    assert result["status"] == "failed" and result["failure_gate"] == gate
    assert result["post_attempted"] is False and not ops.changed and not ops.claimed
    assert "fixture-private" not in json.dumps(result)
    if gate not in {
        "client_credential_read",
        "client_credential_format",
        "empty_authenticated_targets",
        "empty_ledger",
        "orderflow",
    }:
        assert credential not in reads


def test_exact_root_fixture_diagnosis_reads_no_credential_and_cannot_dispatch(monkeypatch):
    ops, reads, credential = preflight_fixture(monkeypatch)
    ops.targets = lambda: pytest.fail("diagnosis used authenticated request")
    result = ops.preflight(diagnosis=True)
    assert result["empty_ledger"] and result["once_marker_absent"]
    assert credential not in reads and ops.client is None and not ops.changed and not ops.claimed
    assert (
        result["actual_credential_reads"]
        == result["actual_provider_calls"]
        == result["actual_service_changes"]
        == 0
    )


def test_fixture_live_preflight_reaches_attestation_after_all_gates(monkeypatch):
    ops, reads, credential = preflight_fixture(monkeypatch)
    assert ops.preflight() == {"empty_authenticated_targets": True, "empty_ledger": True}
    assert reads.count(credential) == 1 and not ops.changed and not ops.claimed


def test_failure_gate_never_reflects_arbitrary_exception_or_string():
    ops = FixtureOps("preflight")
    ops.gate = "fixture-private-token"
    result = e2e.run(ops)
    assert result["failure_gate"] == "unclassified" and "fixture-private" not in json.dumps(result)


@pytest.mark.parametrize(
    "gate", ["core_limits", "helper_file", "client_credential_metadata", "empty_ledger"]
)
def test_diagnosis_reports_gate_and_independent_state_without_reads_or_writes(
    monkeypatch, capsys, gate
):
    ops, reads, credential = preflight_fixture(monkeypatch, gate)
    ops.write = lambda *_: pytest.fail("diagnosis wrote receipt/config/marker")
    ops.http = lambda *_: pytest.fail("diagnosis authenticated or dispatched")
    monkeypatch.setattr(e2e, "RootOps", lambda: ops)
    monkeypatch.setattr(sys, "argv", ["representative_e2e.py", "--diagnose"])
    assert e2e.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["failure_gate"] == gate and result["state_snapshot"]["once_marker_absent"]
    assert result["state_snapshot"]["original_empty_config_hash_matches"]
    assert credential not in reads and ops.client is None and not ops.changed and not ops.claimed
    assert (
        result["actual_credential_reads"]
        == result["actual_provider_calls"]
        == result["actual_service_changes"]
        == 0
    )


@pytest.fixture
def real_http_fixture(monkeypatch):
    """Route the exact production helper to a random loopback port, dummy auth only."""
    settings = {
        "protocol": "HTTP/1.0",
        "close": True,
        "chunked": False,
        "declared_extra": 0,
        "delay": 0,
        "omit_length": False,
    }
    calls = []
    response = b'{"targets":[]}'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def respond(self):
            calls.append(self.command)
            assert self.headers["Authorization"] == "Bearer INVALID_NON_SECRET_FIXTURE"
            if self.command == "POST":
                body = self.rfile.read(int(self.headers["Content-Length"]))
                assert json.loads(body) == e2e.TASK
            self.protocol_version = settings["protocol"]
            self.send_response(200)
            if settings["chunked"]:
                self.send_header("Transfer-Encoding", "chunked")
            elif not settings["omit_length"]:
                self.send_header("Content-Length", str(len(response) + settings["declared_extra"]))
            if settings["close"]:
                self.send_header("Connection", "close")
            self.end_headers()
            try:
                for offset in range(0, len(response), 8192):
                    raw = response[offset : offset + 8192]
                    if settings["chunked"]:
                        self.wfile.write(f"{len(raw):x}\r\n".encode())
                    self.wfile.write(raw)
                    if settings["chunked"]:
                        self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    if settings["delay"]:
                        time.sleep(settings["delay"])
                if settings["chunked"]:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (OSError, ConnectionError):
                pass  # Expected peer closure in oversize/timeout fixtures.

        do_GET = respond
        do_POST = respond

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original = e2e.http.client.HTTPConnection

    def route(host, port, timeout):
        assert (host, port) == ("127.0.0.1", 18084)
        return original(host, server.server_port, timeout=timeout)

    monkeypatch.setattr(e2e.http.client, "HTTPConnection", route)
    ops = e2e.RootOps()
    ops.client = "INVALID_NON_SECRET_FIXTURE"

    def set_body(value):
        nonlocal response
        response = value

    yield ops, settings, calls, set_body
    server.shutdown()
    server.server_close()
    worker.join(2)


@pytest.mark.parametrize(
    "protocol,close", [("HTTP/1.0", True), ("HTTP/1.1", True), ("HTTP/1.1", False)]
)
@pytest.mark.parametrize(
    "method,path",
    [("GET", "/v1/diagnostics"), ("GET", "/v1/tasks/" + e2e.KEY), ("POST", "/v1/tasks")],
)
def test_real_http_close_and_keepalive_complete_once(
    real_http_fixture, protocol, close, method, path
):
    ops, settings, calls, set_body = real_http_fixture
    settings.update(protocol=protocol, close=close)
    payload = {"targets": [], "padding": "x" * 40000}
    set_body(json.dumps(payload).encode())
    status, data = ops.http(method, path, e2e.TASK if method == "POST" else None)
    assert status == 200 and data == payload and calls == [method]


def test_real_http_chunked_and_eof_framing(real_http_fixture):
    ops, settings, calls, set_body = real_http_fixture
    payload = {"targets": [], "padding": "x" * 40000}
    set_body(json.dumps(payload).encode())
    for opts in (
        {"protocol": "HTTP/1.1", "close": False, "chunked": True},
        {"protocol": "HTTP/1.0", "close": True, "chunked": False, "omit_length": True},
    ):
        settings.update(opts)
        assert ops.http("GET", "/v1/diagnostics") == (200, payload)
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("fault", ["truncated_length", "duplicate_json", "oversize", "deadline"])
def test_real_http_rejects_partial_ambiguous_oversize_and_late_without_retry(
    real_http_fixture, monkeypatch, fault
):
    ops, settings, calls, set_body = real_http_fixture
    if fault == "truncated_length":
        settings["declared_extra"] = 10
    elif fault == "duplicate_json":
        set_body(b'{"targets":[],"targets":[]}')
    elif fault == "oversize":
        set_body(json.dumps({"padding": "x" * 262145}).encode())
    else:
        set_body(json.dumps({"targets": [], "padding": "x" * 9000}).encode())
        settings["delay"] = 0.3
        clock = time.monotonic
        monkeypatch.setattr(e2e, "time", SimpleNamespace(monotonic=lambda: clock() * 30))
    with pytest.raises(e2e.GateError):
        ops.http("GET", "/v1/diagnostics")
    assert calls == ["GET"]
