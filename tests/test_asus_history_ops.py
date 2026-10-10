"""Fixed history integration: real SQLite/locks and public data; fake OS/auth only."""

import importlib.util
import json
import os
import socket
import sqlite3
import sys
import threading
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def load(name, directory="deploy/asus"):
    spec = importlib.util.spec_from_file_location(name, ROOT / directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e, p, r, projection, installer = map(
    load,
    (
        "history_ops_entry",
        "history_audit_protocol",
        "history_audit_reader",
        "ops_history_projection",
        "install_history_ops",
    ),
)
old_test = load("test_asus_integrated_history", "tests")
KEY = "a" * 32
PRIVATE = "PUBLIC_PRIVATE_FIXTURE_MUST_NOT_PROJECT"


def report(db):
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        value = projection.classify_ledger(con, set())
    value.update(
        config_sha256="a" * 64,
        config_targets=0,
        config_providers=[],
        old_files={name: {"present": False, "sha256": None} for name in ("claim", "journal")},
        old_bootstrap={"present": False, "resume_permission": False},
        r14_pins={
            "policy_sha256": "b" * 64,
            "entry_sha256": "c" * 64,
            "entry_matches_installed_manifest": True,
            "config_pin_matches": True,
            "restart_enabled": False,
            "expiry_matches_r14": True,
            "credential_opened": False,
        },
        broker_pins={
            "release": "releases/release-" + "d" * 64,
            "manifest_sha256": "d" * 64,
            "unit_sha256": "e" * 64,
            "release_matches_r14": True,
            "unit_matches_r14": True,
        },
    )
    return value


class FixtureEntry:
    Denied = e.Denied

    def __init__(self):
        self.package_calls = 0

    def package(self):
        self.package_calls += 1

    def root_dir(self, path, mode=None):
        assert path.is_dir() and path.stat().st_mode & 0o777 == mode

    def read_root(self, path, *, mode, limit=131072):
        assert path.stat().st_mode & 0o777 == mode and path.stat().st_nlink == 1
        raw = path.read_bytes()
        assert len(raw) <= limit
        return raw

    strict_json = staticmethod(e.strict_json)

    def write_exclusive(self, path, raw):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def runtime_policy(self):
        return {"enabled": False, "expires_at": "2026-11-05T04:33:31+00:00"}


def setup(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "operation.lock").touch(mode=0o600)
    audit = state / "history-audit"
    audit.mkdir(mode=0o700)
    (audit / "reader.lock").touch(mode=0o600)
    db = tmp_path / "ledger.sqlite3"
    old_test.database(db)
    facade = FixtureEntry()
    calls = []

    def preflight(*, check):
        check("ledger")
        calls.append("readonly_sqlite")
        return report(db), b'{"targets":[]}'

    projected = SimpleNamespace(private_preflight=preflight, Blocked=projection.Blocked)
    return state, audit, db, facade, projected, calls


def control(state, audit, facade, projected, calls, *, fault=None):
    def run(argv, **kwargs):
        assert argv[0] == "/usr/bin/systemctl"
        if argv[1] == "list-units":
            assert argv[-1] == "api-quota-broker-history-audit@*.service"
            return 0, b""
        unit = argv[2]
        assert unit == "api-quota-broker-history-audit@" + KEY + ".service"
        if argv[1] == "show":
            return (
                0,
                b"LoadState=loaded\nActiveState=inactive\nSubState=dead\nExecMainStartTimestampMonotonic=0\nDropInPaths=\n",
            )
        assert argv[1] == "start" and kwargs["timeout"] == 100
        if fault == "timeout":
            raise e.Denied("native_timeout")
        value = r.perform(
            KEY,
            entry=facade,
            protocol=p,
            projection=projected,
            state=audit,
            scope=lambda _: None,
            owner=os.getuid(),
        )
        (audit / (KEY + ".result.json")).write_text(json.dumps(value))
        return (0 if value["status"] == "passed" else 1), b""

    return e.fixed_history_audit(
        {"operation": "history_audit", "request_id": KEY},
        state=state,
        run=run,
        read=facade.read_root,
        writer=facade.write_exclusive,
        root_check=facade.root_dir,
        protocol=p,
        owner=os.getuid(),
    )


def test_real_readonly_ledger_control_reader_protocol_and_reserved_hold(tmp_path):
    state, audit, db, facade, projected, calls = setup(tmp_path)
    with sqlite3.connect(db) as con:
        old_test.add(
            con, "PUBLIC_CF", "cloudflare", "completed_usage_unknown", rid="c", done=True, charges=0
        )
        con.execute(
            "INSERT INTO queue_jobs VALUES(?,?,?)", (PRIVATE, "completed", PRIVATE.encode())
        )
    before = db.read_bytes()
    value = control(state, audit, facade, projected, calls)
    assert p.validate(value, KEY)["status"] == "passed" and calls == ["readonly_sqlite"]
    assert value["summary"]["blocked_providers"] == ["cloudflare"]
    assert value["summary"]["probe_permission"] is False and PRIVATE not in json.dumps(value)
    assert db.read_bytes() == before and (audit / (KEY + ".reader.json")).exists()
    saved = (audit / (KEY + ".result.json")).read_bytes()
    with pytest.raises(e.Denied, match="audit_request_replayed"):
        control(state, audit, facade, projected, calls)
    assert calls == ["readonly_sqlite"] and (audit / (KEY + ".result.json")).read_bytes() == saved


def test_genuine_gateway_schema_converts_through_new_safe_summary(tmp_path):
    old_test.test_genuine_gateway_schema_classifies_unknown_usage_with_reserved_charges(tmp_path)
    value = p.passed(report(tmp_path / "ledger.sqlite3"), KEY)
    assert value["summary"]["classification"]["known_groq_settled"] == 1
    assert value["summary"]["blocked_providers"] == ["cloudflare"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("SQL", "SELECT payload"),
        ("path", "/etc/shadow"),
        ("unit", "ssh.service"),
        ("env", {"X": "Y"}),
    ],
)
def test_no_sql_path_unit_or_environment_request_fields(field, value):
    with pytest.raises(e.Denied):
        e.request(
            json.dumps({"operation": "history_audit", "request_id": KEY, field: value}).encode()
        )


@pytest.mark.parametrize("key", ["../etc/shadow", "a" * 31, "a" * 33, "A" * 32, "a;id" * 8])
def test_nonce_is_not_a_unit_name_or_path(key):
    with pytest.raises(e.Denied):
        e.request(json.dumps({"operation": "history_audit", "request_id": key}).encode())


def test_unknown_start_keeps_claim_and_empty_result_and_never_redispatches(tmp_path):
    state, audit, _, facade, projected, calls = setup(tmp_path)
    value = control(state, audit, facade, projected, calls, fault="timeout")
    assert value["code"] == "audit_unit_wait_unknown" and calls == []
    assert (audit / (KEY + ".claim.json")).exists() and (
        audit / (KEY + ".result.json")
    ).read_bytes() == b""
    with pytest.raises(e.Denied, match="audit_request_replayed"):
        control(state, audit, facade, projected, calls)
    assert calls == []


def test_empty_result_reader_marker_rejects_manual_unit_retry_before_database(tmp_path):
    _, audit, _, facade, projected, calls = setup(tmp_path)
    facade.write_exclusive(
        audit / (KEY + ".claim.json"),
        json.dumps(
            {
                "schema": 1,
                "operation": "history_audit",
                "request_id": KEY,
                "dispatch_intent": True,
                "credential_auth_verified": True,
            }
        ).encode(),
    )
    facade.write_exclusive(audit / (KEY + ".reader.json"), b"PUBLIC_PRIOR_ATTEMPT")
    value = r.perform(
        KEY,
        entry=facade,
        protocol=p,
        projection=projected,
        state=audit,
        scope=lambda _: None,
        owner=os.getuid(),
    )
    assert value["code"] == "audit_request_replayed" and calls == []


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("DROP TABLE queue_jobs", "audit_schema_missing_tables"),
        (
            "ALTER TABLE gateway_tasks RENAME COLUMN model TO missing",
            "audit_schema_missing_columns",
        ),
        ("INSERT INTO queue_jobs VALUES('PUBLIC','pending',NULL)", "audit_queue_active"),
        ("DELETE FROM execution_completion", "audit_groq_evidence_mismatch"),
    ],
)
def test_meaningful_fixed_schema_or_ledger_errors_reach_client_contract(tmp_path, mutation, code):
    state, audit, db, facade, projected, calls = setup(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(mutation)
    value = control(state, audit, facade, projected, calls)
    assert value["code"] == code and value["check"] == "ledger"
    assert p.validate(value, KEY)["private_values_exposed"] is False


@pytest.mark.parametrize(
    "inject",
    [
        lambda v: v["summary"].update(raw_response=PRIVATE),
        lambda v: v["summary"]["config"].update(sha256=PRIVATE),
        lambda v: v.update(request_id="b" * 32),
        lambda v: v["summary"].update(probe_permission=True),
        lambda v: v["summary"]["ops_pins"].update(restart_enabled=True),
    ],
)
def test_result_whitelist_never_relays_private_or_unbound_values(tmp_path, inject):
    db = tmp_path / "fixture.sqlite3"
    old_test.database(db)
    value = p.passed(report(db), KEY)
    inject(value)
    with pytest.raises(p.Invalid):
        p.validate(value, KEY)


def test_inspect_stays_original_contract_and_history_never_calls_restart(tmp_path):
    events = []
    args = {
        "pins": lambda: events.append("pins"),
        "inspect": lambda: {"PUBLIC": True},
        "restart": lambda: pytest.fail("restart is forbidden"),
        "audit": lambda _: p.blocked("audit_busy", "result", KEY),
    }
    result = e.helper_operation({"operation": "inspect", "request_id": KEY}, **args)
    assert result["state"] == {"PUBLIC": True} and "summary" not in result
    result = e.helper_operation({"operation": "history_audit", "request_id": KEY}, **args)
    assert result["code"] == "audit_busy"


def test_reader_output_guard_never_writes_existing_result(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["PUBLIC", KEY])
    monkeypatch.setattr(
        r, "output_guard", lambda _: (_ for _ in ()).throw(r.Stop("audit_request_replayed"))
    )
    monkeypatch.setattr(r, "modules", lambda: pytest.fail("no private module load on replay"))
    assert r.main() == 1 and capsys.readouterr().out == ""


class FixtureUpgrade:
    def __init__(self, fail=None, rollback_fail=False):
        self.claimed = False
        self.fail, self.rollback_fail, self.events = fail, rollback_fail, []

    def step(self, name):
        self.events.append(name)
        if self.fail == name:
            raise installer.Blocked("upgrade_validation")

    def preflight(self):
        self.step("preflight")

    def claim_and_backup(self):
        self.step("claim")
        self.claimed = True

    def prepare_additions(self):
        self.step("prepare")

    def publish(self):
        self.step("publish")

    def validate(self):
        self.step("validate")

    def verify_preservation(self):
        self.step("preserve")

    def rollback(self):
        self.step("rollback")
        if self.rollback_fail:
            raise RuntimeError(PRIVATE)

    def close(self):
        self.step("close")


@pytest.mark.parametrize("fault", [None, "preflight", "prepare", "publish", "validate", "preserve"])
def test_upgrade_transaction_order_and_failure_rollback_gate(fault):
    fixture = FixtureUpgrade(fail=fault)
    value = installer.upgrade(fixture)
    assert value["status"] == ("passed" if fault is None else "blocked")
    assert ("rollback" in fixture.events) == (fault not in (None, "preflight"))
    assert fixture.events[-1] == "close" and PRIVATE not in json.dumps(value)
    if fault is None:
        assert value["native_inspect_executed"] is False and value["Doppler_GET"] == 0


def test_upgrade_rollback_unknown_is_never_reported_as_restored():
    value = installer.upgrade(FixtureUpgrade(fail="publish", rollback_fail=True))
    assert value["rollback_verified"] is False and value["automatic_retry"] is False


def test_timeout_chain_and_credential_mount_contracts():
    worker = (ROOT / "deploy/asus/history-api-quota-broker-ops@.service").read_text()
    audit = (ROOT / "deploy/asus/api-quota-broker-history-audit@.service").read_text()
    assert "RuntimeMaxSec=190" in worker
    assert "InaccessiblePaths=/etc/api-quota-broker/credentials /var/lib/api-quota-broker" in worker
    assert "TimeoutStartSec=90" in audit and "TimeoutStopSec=5" in audit
    assert (
        "NoNewPrivileges=yes" in audit
        and "ProtectSystem=strict" in audit
        and "PrivateNetwork=yes" in audit
    )
    assert "LoadCredential" not in audit and "JoinsNamespaceOf" not in audit
    chain = [8 + 8 + 100, 135, 175, 190, 210]
    assert all(a < b for a, b in pairwise(chain))


def test_new_projection_sees_committed_wal_without_changing_db_or_wal(tmp_path):
    db = tmp_path / "fixture.sqlite3"
    old_test.database(db)
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        old_test.add(writer, "PUBLIC_WAL", "google", "completed", rid="w", done=True)
        writer.commit()
        wal = Path(str(db) + "-wal")
        before = {path: path.read_bytes() for path in (db, wal)}
        result = report(db)
        assert result["classification"]["dispatched_known_result"] == 1
        assert all(path.read_bytes() == value for path, value in before.items())


def test_actual_unix_worker_roundtrip_authenticated_history_result_and_wipe(tmp_path):
    state, audit, _, facade, projected, calls = setup(tmp_path)
    auth = load("broker_ops_policy")
    token = bytearray(b"dp.st.dev." + b"0" * 40)
    passwords, fetches = [], []
    fixture_password = "PUBLIC_FIXTURE_PASSWORD_NOT_REAL_0000000"

    class Session:
        def execute(self, password, req):
            passwords.append(password)
            assert password.decode() == fixture_password and req["operation"] == "history_audit"
            return control(state, audit, facade, projected, calls)

        def close(self):
            pass

    def transport(path, headers):
        assert path == auth.SECRET_PATH and set(headers) == {"Authorization", "Accept"}
        fetches.append("fake_GET")
        return 200, json.dumps(
            {"name": auth.SECRET, "value": {"raw": fixture_password, "computed": fixture_password}}
        ).encode()

    client, worker = socket.socketpair()
    errors = []

    def serve():
        try:
            e.worker_connection(
                worker,
                prepare=lambda: None,
                run=lambda req: e.manage(
                    req,
                    lambda: token,
                    transport,
                    auth,
                    guard=lambda: None,
                    probe=lambda: None,
                    session_factory=Session,
                ),
            )
        except BaseException as error:  # noqa: BLE001 - collect thread fixture failures.
            errors.append(error)
        finally:
            worker.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client.settimeout(3)
        client.sendall(json.dumps({"operation": "history_audit", "request_id": KEY}).encode())
        client.shutdown(socket.SHUT_WR)
        raw = b""
        while part := client.recv(32768):
            raw += part
    finally:
        client.close()
        thread.join(timeout=3)
    assert not errors and not thread.is_alive()
    assert p.validate(json.loads(raw), KEY)["status"] == "passed"
    assert fetches == ["fake_GET"] and not any(token) and all(not any(x) for x in passwords)
    assert fixture_password.encode() not in raw and PRIVATE.encode() not in raw


def test_reader_raw_os_error_is_meaningful_without_private_exception_text(tmp_path):
    _, audit, _, facade, projected, _ = setup(tmp_path)
    facade.write_exclusive(
        audit / (KEY + ".claim.json"),
        json.dumps(
            {
                "schema": 1,
                "operation": "history_audit",
                "request_id": KEY,
                "dispatch_intent": True,
                "credential_auth_verified": True,
            }
        ).encode(),
    )

    def denied(*, check):
        check("ledger")
        raise PermissionError(PRIVATE)

    projected.private_preflight = denied
    result = r.perform(
        KEY,
        entry=facade,
        protocol=p,
        projection=projected,
        state=audit,
        scope=lambda _: None,
        owner=os.getuid(),
    )
    assert result["code"] == "audit_required_input_inaccessible" and result["check"] == "ledger"
    assert PRIVATE not in json.dumps(result)
