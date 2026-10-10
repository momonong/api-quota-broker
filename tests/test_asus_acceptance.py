"""Local fixture acceptance only: no ASUS, real credentials or provider I/O."""

import hashlib
import http.client
import importlib.util
import json
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_broker.gateway import Gateway
from quota_broker.gateway_server import make_gateway_server
from quota_broker.queue import DurableQueue

PATH = Path(__file__).resolve().parents[1] / "deploy/asus/acceptance.py"
spec = importlib.util.spec_from_file_location("asus_acceptance_fixture", PATH)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)
CLIENT = "synthetic-client-credential-value-for-fixture-only"
ADMIN = "synthetic-admin-credential-value-for-fixture-only"


def prohibited(*args, **kwargs):
    raise AssertionError("unexpected operation")


def diagnosis(targets=None):
    return {
        "as_of": "2026-10-03T00:00:00Z",
        "ready_targets": 0,
        "targets": [] if targets is None else targets,
        "basis": "local_minimum_admission_snapshot",
        "queue_worker": {
            "enabled": False,
            "running": False,
            "stopped": False,
            "error_code": None,
            "last_tick_at": None,
        },
    }


def fake_api(monkeypatch, *, targets=None, wrong_role=False):
    calls, decryptions = [], []

    def decrypt(name):
        decryptions.append(name)
        return CLIENT if name == "client_token" else ADMIN

    def http(method, path, token, body, sensitive):
        calls.append((method, path, token, body))
        assert sensitive == (CLIENT, ADMIN)
        if path == "/v1/diagnostics":
            return (401, {"error": "unauthorized"}) if token is None else (200, diagnosis(targets))
        if path == "/v1/catalog":
            return 200, [] if targets is None else targets
        if path == acceptance.ADMIN_PATH:
            return (
                (404, {"error": "not_found", "message": "PRIVATE-MESSAGE"})
                if token == ADMIN or wrong_role
                else (401, {"error": "unauthorized"})
            )
        if path == "/v1/routes/explain":
            assert body == acceptance.EXPLAIN
            return 200, {
                "selected_target_id": None,
                "estimated_input_tokens": 9,
                "candidates": [],
                "temporary": False,
                "permanent_rejection": True,
                "next_retry_at": None,
            }
        return 200, [] if path == "/v1/usage" else {
            "tasks": [],
            "next_before": None,
        } if path == "/v1/tasks" else {"tasks": []}

    monkeypatch.setattr(acceptance, "_decrypt", decrypt)
    monkeypatch.setattr(acceptance, "_http", http)
    return calls, decryptions


def test_dry_plan_performs_no_observation_read_or_decrypt(monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "run_checks", prohibited)
    monkeypatch.setattr(acceptance, "_command", prohibited)
    monkeypatch.setattr(acceptance, "_read", prohibited)
    monkeypatch.setattr(acceptance.http.client, "HTTPConnection", prohibited)
    assert acceptance.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry_plan" and result["execution_authorized"] is False
    assert result["provider_calls"] == 0


def test_initial_auth_roles_and_fixed_empty_route_use_only_two_credentials(monkeypatch):
    calls, decryptions = fake_api(monkeypatch)
    report = acceptance._api(True, True)
    assert decryptions == ["client_token", "admin_token"]
    assert report["explain"] == "no_candidates" and report["client_admin_status"] == 401
    assert report["admin_missing_target_status"] == 404
    assert len(calls) == 9
    serialized = json.dumps(report)
    assert CLIENT not in serialized and ADMIN not in serialized and "PRIVATE" not in serialized
    assert not any(path == "/v1/tasks" and method == "POST" for method, path, _, _ in calls)


@pytest.mark.parametrize("empty", [True, False])
def test_observer_allows_catalog_and_skips_every_maintenance_post(monkeypatch, empty):
    calls, decryptions = fake_api(
        monkeypatch,
        targets=[{"target_id": "configured", "provider": "mistral", "model": "PRIVATE-CONTENT"}],
    )
    report = acceptance._api(False, empty)
    assert len(calls) == 3 and all(method == "GET" for method, *_ in calls)
    assert decryptions == ["client_token", "admin_token"]
    assert report["catalog_rows"] == 1
    assert report["maintenance_probes"] == (
        "skipped_observer" if empty else "skipped_ledger_not_empty"
    )
    assert "PRIVATE" not in json.dumps(report)


def test_bad_client_admin_authorization_is_failure(monkeypatch):
    fake_api(monkeypatch, wrong_role=True)
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._api(True, True)
    assert caught.value.reason == "authorization_or_error"


@pytest.fixture
def observed(monkeypatch):
    database = {
        "quick_check": "ok",
        "rows": dict.fromkeys(acceptance.TABLES, 0),
        "states": {"queue_jobs": {}, "reservations": {}, "gateway_tasks": {}},
    }
    properties = {"MainPID": "123"}
    monkeypatch.setattr(acceptance, "_execution_gate", lambda: None)
    monkeypatch.setattr(acceptance, "_service", lambda: ({"main_pid": 123}, properties, 1001, 1001))
    monkeypatch.setattr(
        acceptance, "_token_metadata", lambda: {"unexpired": True, "remote_scope_verified": False}
    )
    monkeypatch.setattr(acceptance, "_systemd", lambda *args: properties)
    monkeypatch.setattr(acceptance, "_read", lambda *args: b'{"targets":[]}')
    monkeypatch.setattr(acceptance, "_database", lambda *args: database)
    monkeypatch.setattr(
        acceptance, "_orderflow", lambda: {"active": True, "http_status": 200, "n_restarts": 0}
    )
    calls, _ = fake_api(monkeypatch)
    return database, calls


def test_safe_database_receipt_is_stable_across_initial_checks(observed):
    first, second = acceptance.run_checks(initial=True), acceptance.run_checks(initial=True)
    assert first["status"] == second["status"] == "passed"
    assert first["database"] == second["database"]
    assert first["provider_calls"] == first["doppler_calls"] == first["service_changes"] == 0


def test_initial_nonempty_ledger_stops_before_credentials_or_http(observed):
    database, calls = observed
    database["rows"]["queue_jobs"] = 1
    report = acceptance.run_checks(initial=True)
    assert report["reason"] == "ledger_not_empty" and calls == []


def test_observer_preserves_nonempty_ledger_and_safe_counts(observed):
    database, calls = observed
    database["rows"]["queue_jobs"] = 2
    database["states"]["queue_jobs"] = {"unknown": 2}
    report = acceptance.run_checks(initial=False)
    assert report["status"] == "passed" and report["database"]["rows"]["queue_jobs"] == 2
    assert len(calls) == 3


def test_os_errors_cannot_reflect_secrets_in_failure_receipt(monkeypatch):
    def fail():
        raise OSError("PRIVATE-SECRET")

    monkeypatch.setattr(acceptance, "_execution_gate", fail)
    report = acceptance.run_checks()
    assert report["status"] == "failed" and "PRIVATE" not in json.dumps(report)


def test_unknown_cli_option_does_not_echo_user_content(capsys):
    assert acceptance.main(["--PRIVATE-SECRET"]) == 2
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert json.loads(captured.out)["reason"] == "invalid_options"


def test_metadata_row_raw_input_field_is_rejected(monkeypatch):
    fake_api(
        monkeypatch,
        targets=[{"target_id": "x", "provider": "mistral", "model": "x", "input": "PRIVATE"}],
    )
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._api(False, False)
    assert caught.value.reason == "metadata_rows_schema" and "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "group,core,swap",
    [
        ("/system.slice/ssh.service", (0, 0), b"0"),
        ("/system.slice/api-quota-broker-acceptance.service", (0, 1), b"0"),
        ("/system.slice/api-quota-broker-install.service", (0, 0), b"1"),
    ],
)
def test_self_secret_gate_requires_exact_cgroup_no_core_no_swap(group, core, swap, monkeypatch):
    monkeypatch.setattr(acceptance.os, "geteuid", lambda: 0)
    monkeypatch.setattr(acceptance.platform, "node", lambda: "asus-ubuntu2604-server")
    monkeypatch.setattr(acceptance.resource, "getrlimit", lambda *_: core)
    monkeypatch.setattr(acceptance, "_cgroup", lambda *_: group)
    monkeypatch.setattr(acceptance, "_read", lambda *_: swap)
    monkeypatch.setattr(acceptance, "_decrypt", prohibited)
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._execution_gate()


@pytest.mark.parametrize(
    "raw",
    [
        b"0::/system.slice/api-quota-broker.service/child\n",
        b"1:memory:/system.slice/api-quota-broker.service\n",
        b"0::/system.slice/api-quota-broker.service\n0::/system.slice/other.service\n",
    ],
)
def test_actual_cgroup_rejects_v1_child_and_duplicate_membership(raw, monkeypatch):
    monkeypatch.setattr(acceptance, "_read", lambda *_: raw)
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._cgroup(123)


def test_service_verifies_actual_uid_core_and_cgroup_metadata(monkeypatch):
    fields = {
        **acceptance.RESOURCES,
        "User": "api-quota-broker",
        "Group": "api-quota-broker",
        "MainPID": "123",
        "ActiveState": "active",
        "SubState": "running",
        "NRestarts": "0",
        "ExecMainStartTimestampMonotonic": "456",
    }
    monkeypatch.setattr(
        acceptance.pwd, "getpwnam", lambda *_: SimpleNamespace(pw_uid=1001, pw_gid=1001)
    )
    monkeypatch.setattr(acceptance.grp, "getgrnam", lambda *_: SimpleNamespace(gr_gid=1001))
    monkeypatch.setattr(acceptance, "_systemd", lambda *_: fields)
    monkeypatch.setattr(acceptance, "_cgroup", lambda *_: acceptance.CGROUP)
    metadata = []
    monkeypatch.setattr(
        acceptance, "_metadata", lambda path, *args, **kwargs: metadata.append(path)
    )
    monkeypatch.setattr(acceptance, "_command", lambda *_: b"tmpfs\n")
    uid = [1001]

    def read(path, bound):
        if path.name == "status":
            return f"Uid:\t{uid[0]}\t1001\t1001\t1001\nGid:\t1001\t1001\t1001\t1001\n".encode()
        return (
            b"Max core file size         0             0           bytes\n"
            if path.name == "limits"
            else b"0\n"
        )

    monkeypatch.setattr(acceptance, "_read", read)
    assert acceptance._service()[0]["core_limit"] == 0
    assert len([p for p in metadata if p.suffix == ".cred"]) == 5
    uid[0] = 0
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._service()
    assert caught.value.reason == "process_identity"


def test_readonly_database_uses_only_safe_count_state_fields(tmp_path, monkeypatch):
    database = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        for name in acceptance.TABLES:
            connection.execute("CREATE TABLE " + name + "(state TEXT,payload BLOB)")
        connection.execute(
            "INSERT INTO queue_jobs VALUES('unknown',?)", (b"PRIVATE-ENCRYPTED-CONTENT",)
        )
        connection.execute("CREATE TABLE queue_settings(verifier BLOB)")
        connection.execute("INSERT INTO queue_settings VALUES(?)", (b"PRIVATE-VERIFIER",))
    database.chmod(0o600)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(acceptance, "DB", database)
    before = hashlib.sha256(database.read_bytes()).digest()
    statements = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        assert args[0].endswith("?mode=ro") and kwargs["uri"] is True
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(acceptance.sqlite3, "connect", connect)
    report = acceptance._database(os.getuid(), os.getgid())
    assert report["states"]["queue_jobs"] == {"unknown": 1}
    assert hashlib.sha256(database.read_bytes()).digest() == before
    assert all(
        "PRIVATE" not in statement and "verifier" not in statement and "payload" not in statement
        for statement in statements
    )
    assert "PRIVATE" not in json.dumps(report)
    assert not any(statement.startswith(("UPDATE", "DELETE", "INSERT")) for statement in statements)


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "DB", tmp_path / "missing.sqlite3")
    with pytest.raises(OSError):
        acceptance._database(os.getuid(), os.getgid())
    assert not (tmp_path / "missing.sqlite3").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
def test_metadata_rejects_secret_file_aliases_without_read(kind, tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"PRIVATE-CONTENT")
    original.chmod(0o600)
    target = tmp_path / "alias"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        os.link(original, target)
    with pytest.raises((OSError, acceptance.AcceptanceError)):
        acceptance._metadata(target, os.getuid(), os.getgid(), 0o600)


class FakeSocket:
    def settimeout(self, value):
        assert 0 < value <= acceptance.HTTP_SECONDS


def fake_connection(monkeypatch, *, status=200, raw=b"{}", content_type="application/json"):
    class Response:
        def __init__(self):
            self.status, self.data = status, raw

        def getheader(self, name, default):
            return content_type

        def read1(self, bound):
            result, self.data = self.data[:bound], self.data[bound:]
            return result

        def isclosed(self):
            return not self.data

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 18084, acceptance.HTTP_SECONDS)
            self.sock = FakeSocket()

        def connect(self):
            pass

        def request(self, method, path, body, headers):
            assert "Host" not in headers

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(acceptance.http.client, "HTTPConnection", Connection)


@pytest.mark.parametrize(
    "status,raw,reason",
    [
        (302, b"{}", "redirect_rejected"),
        (200, CLIENT.encode(), "secret_reflection"),
        (200, b'{"x":1,"x":2}', "duplicate_json_field"),
        (200, b"X" * (acceptance.BODY_BOUND + 1), "body_bound"),
    ],
)
def test_http_redirect_secret_duplicate_and_body_bounds(status, raw, reason, monkeypatch):
    fake_connection(monkeypatch, status=status, raw=raw)
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._http("GET", "/v1/diagnostics", CLIENT, None, (CLIENT, ADMIN))
    assert caught.value.reason == reason and CLIENT not in str(caught.value)


def test_fixed_http_paths_cannot_send_provider_or_arbitrary_task(monkeypatch):
    monkeypatch.setattr(acceptance.http.client, "HTTPConnection", prohibited)
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._http("POST", "/v1/tasks", CLIENT, {}, (CLIENT, ADMIN))
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._http(
            "POST", "/v1/routes/explain", CLIENT, {"input": "PRIVATE"}, (CLIENT, ADMIN)
        )


def test_http_deadline_is_shared_across_reads(monkeypatch):
    fake_connection(monkeypatch)
    values = iter([0, 0, 0, 6])
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: next(values))
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._http("GET", "/v1/diagnostics", CLIENT, None, (CLIENT, ADMIN))
    assert caught.value.reason == "deadline"


def test_decryption_only_client_admin_in_memory_no_key_argv(monkeypatch):
    commands = []
    monkeypatch.setattr(
        acceptance,
        "_command",
        lambda command, **kwargs: commands.append(command) or CLIENT.encode(),
    )
    assert acceptance._decrypt("client_token") == CLIENT
    assert commands[0][-1] == "-" and CLIENT not in " ".join(commands[0])
    with pytest.raises(acceptance.AcceptanceError):
        acceptance._decrypt("doppler_service_token")


def token_record():
    path = PATH.with_name("import_credentials.py")
    spec = importlib.util.spec_from_file_location("host_import_metadata_fixture", path)
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    now = datetime.now(UTC).replace(microsecond=0)
    return importer.metadata_record(now + timedelta(days=1), human_attested=True, now=now)


def test_observer_metadata_matches_importer_and_does_not_claim_remote_scope(monkeypatch):
    record = token_record()
    paths = []
    monkeypatch.setattr(acceptance, "_metadata", lambda *args: None)
    monkeypatch.setattr(
        acceptance,
        "_read",
        lambda path, bound: paths.append((path, bound)) or json.dumps(record).encode(),
    )
    monkeypatch.setattr(acceptance, "_decrypt", prohibited)
    report = acceptance._token_metadata()
    assert report["expires_at"] == record["expires_at"] and report["unexpired"] is True
    assert (
        report["remote_scope_verified"] is False
        and report["evidence"] == "human_dashboard_attestation_only"
    )
    assert paths == [(acceptance.CREDS / "doppler-metadata.json", 8192)]


@pytest.mark.parametrize(
    "mode", ["expired", "overlong", "scope", "remote_claim", "unknown_field", "bool_schema"]
)
def test_token_metadata_expiry_scope_evidence_are_strict(mode, monkeypatch):
    record = token_record()
    if mode == "expired":
        record["expires_at"] = record["created_at"]
    elif mode == "overlong":
        record["expires_at"] = (datetime.now(UTC) + timedelta(days=31)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    elif mode == "scope":
        record["access"] = "write"
    elif mode == "remote_claim":
        record["remote_scope_verified"] = True
    elif mode == "unknown_field":
        record["token"] = "PRIVATE-SECRET"
    else:
        record["schema_version"] = True
    monkeypatch.setattr(acceptance, "_metadata", lambda *args: None)
    monkeypatch.setattr(acceptance, "_read", lambda *args: json.dumps(record).encode())
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance._token_metadata()
    assert caught.value.phase == "credential_metadata" and "PRIVATE" not in str(caught.value)


def test_private_receipt_is_exclusive_and_not_service_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "BACKUPS", tmp_path)
    original_metadata = acceptance._metadata
    monkeypatch.setattr(
        acceptance,
        "_metadata",
        lambda path, uid, gid, *args, **kwargs: original_metadata(
            path, os.getuid(), os.getgid(), *args, **kwargs
        ),
    )
    tmp_path.chmod(0o700)
    path = tmp_path / "acceptance-fixture.json"
    acceptance._receipt(path, {"status": "passed"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        acceptance._receipt(path, {"status": "different"})
    assert json.loads(path.read_bytes()) == {"status": "passed"}


def test_real_local_gateway_zero_provider_initial_contract(tmp_path, monkeypatch):
    gateway = Gateway(tmp_path / "fixture.sqlite3", (), b"d" * 32, prohibited, transport=prohibited)
    Path(gateway.db).chmod(0o600)
    tmp_path.chmod(0o700)
    queue = DurableQueue(gateway, key=b"q" * 32)
    server = make_gateway_server(
        gateway, CLIENT, admin_token=ADMIN, queue=queue, worker=False, port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = http.client.HTTPConnection
    monkeypatch.setattr(
        acceptance.http.client,
        "HTTPConnection",
        lambda host, port, **kwargs: original(host, server.server_port, **kwargs),
    )
    monkeypatch.setattr(
        acceptance, "_decrypt", lambda name: CLIENT if name == "client_token" else ADMIN
    )
    try:
        report = acceptance._api(True, True)
        assert report["explain"] == "no_candidates" and report["credential_decryptions"] == 2
        with sqlite3.connect(gateway.db) as connection:
            assert all(
                connection.execute("SELECT count(*) FROM " + name).fetchone()[0] == 0
                for name in acceptance.TABLES
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
