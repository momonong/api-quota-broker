"""The maintenance exception admits exactly one proved unsent preparation."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_asus_maintenance_repair import load

ops = load("maintenance_ops")


def fixture_db():
    con = sqlite3.connect(":memory:")
    now = datetime.now(UTC)
    con.executescript("""
    CREATE TABLE gateway_tasks(request_key,state,reservation_id,target_id,provider,model,dispatched_at,completed_at);
    CREATE TABLE gateway_attempts(request_key);
    CREATE TABLE reservations(request_key);
    CREATE TABLE charges(value);
    CREATE TABLE execution_completion(value);
    CREATE TABLE queue_jobs(request_key,state,attempt_count,max_attempts,execution_key,run_started,wait_policy,payload,result,expires_at,deadline,lease_until,execution_until);
    CREATE TABLE queue_attempts(request_key,attempt_no,execution_key);
    CREATE TABLE queue_settings(id,verifier);
    """)
    for table, count in [
        ("gateway_tasks", 8),
        ("gateway_attempts", 8),
        ("reservations", 8),
        ("charges", 26),
        ("execution_completion", 8),
    ]:
        col = (
            "request_key"
            if table in {"gateway_tasks", "gateway_attempts", "reservations"}
            else "value"
        )
        con.executemany(f"INSERT INTO {table}({col}) VALUES(?)", [(str(i),) for i in range(count)])
    con.execute(
        "INSERT INTO gateway_tasks VALUES(?,?,?,?,?,?,?,?)",
        (ops.FD_RECOVERY_EXECUTION, "preparing", None, None, None, None, None, None),
    )
    con.execute(
        "INSERT INTO queue_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ops.FD_RECOVERY_QUEUE,
            "running",
            1,
            1,
            ops.FD_RECOVERY_EXECUTION,
            1,
            "reject",
            b"ciphertext",
            None,
            (now + timedelta(hours=1)).isoformat(),
            None,
            (now - timedelta(minutes=1)).isoformat(),
            (now - timedelta(minutes=1)).isoformat(),
        ),
    )
    con.execute(
        "INSERT INTO queue_attempts VALUES(?,?,?)",
        (ops.FD_RECOVERY_QUEUE, 1, ops.FD_RECOVERY_EXECUTION),
    )
    con.execute("INSERT INTO queue_settings VALUES(1,'verifier')")
    con.commit()
    return con


def test_exact_recovery_and_both_implementations_agree():
    installer = load("install_fd_recovery")
    with closing(fixture_db()) as con:
        assert ops.fd_recovery_gate(con) == installer.fd_recovery_gate(con) == 1
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM reservations")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE queue_jobs SET max_attempts=2",
        "UPDATE queue_jobs SET state='unknown'",
        "UPDATE queue_jobs SET execution_key='different'",
        "UPDATE queue_jobs SET payload=NULL",
        "UPDATE queue_jobs SET expires_at='2000-01-01T00:00:00+00:00'",
        "UPDATE queue_jobs SET lease_until='2100-01-01T00:00:00+00:00'",
        "UPDATE gateway_tasks SET dispatched_at='yes' WHERE state='preparing'",
        "INSERT INTO reservations VALUES('gw:" + ops.FD_RECOVERY_EXECUTION + ":0')",
        "INSERT INTO gateway_attempts VALUES('" + ops.FD_RECOVERY_EXECUTION + "')",
        "UPDATE queue_attempts SET execution_key='different'",
    ],
)
def test_any_scope_or_dispatch_drift_rejected(sql):
    with closing(fixture_db()) as con:
        con.execute(sql)
        con.commit()
        with pytest.raises(ValueError, match="queue_not_quiescent"):
            ops.fd_recovery_gate(con)


def test_preservation_allows_only_original_job_and_preparing_row(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    original = backup / "ledger.sqlite3"
    live = tmp_path / "live.sqlite3"
    tables = (
        "gateway_tasks",
        "gateway_attempts",
        "reservations",
        "charges",
        "execution_completion",
        "queue_jobs",
        "queue_attempts",
        "queue_settings",
    )
    with closing(sqlite3.connect(original)) as con, con:
        for name in tables:
            con.execute(f"CREATE TABLE {name}(request_key,value)")
            con.execute(f"INSERT INTO {name} VALUES(?,?)", ("history", "unknown hold"))
        con.execute(
            "INSERT INTO gateway_tasks VALUES(?,?)", (ops.FD_RECOVERY_EXECUTION, "preparing")
        )
        con.execute("INSERT INTO queue_jobs VALUES(?,?)", (ops.FD_RECOVERY_QUEUE, "running"))
    with closing(sqlite3.connect(original)) as con, closing(sqlite3.connect(live)) as target:
        con.backup(target)
    native = object.__new__(ops.Native)
    native.backup = backup
    native.recovery = True
    native.db = lambda: sqlite3.connect(live)
    for state in ("completed", "unknown"):
        with closing(sqlite3.connect(live)) as con, con:
            con.execute(
                "UPDATE gateway_tasks SET value=? WHERE request_key=?",
                (state, ops.FD_RECOVERY_EXECUTION),
            )
            con.execute(
                "UPDATE queue_jobs SET value=? WHERE request_key=?", (state, ops.FD_RECOVERY_QUEUE)
            )
        native.preserved_rows()
    with closing(sqlite3.connect(live)) as con, con:
        con.execute("UPDATE reservations SET value='changed' WHERE request_key='history'")
    with pytest.raises(ValueError, match="foreign_change"):
        native.preserved_rows()


def test_failed_post_stop_gate_leaves_broker_stopped_without_restore(monkeypatch):
    from types import SimpleNamespace

    protocol = load("maintenance_protocol")
    events = []
    monkeypatch.setattr(ops, "module", lambda *_: protocol)

    class Backend:
        recovery = True
        changed = False
        legacy_clear = True
        jobs = 1

        def __init__(self, *_):
            pass

        def preflight(self, **_):
            pass

        def deploy(self):
            events.append("stop")
            self.changed = True
            raise ValueError("queue_not_quiescent")

        def command(self, *args):
            events.append(args)

        def restore(self):
            raise AssertionError("must not restore or replay")

    result = ops.execute(
        SimpleNamespace(),
        {"operation": "deploy", "request_id": ops.FD_RECOVERY_ID},
        backend=Backend,
    )
    assert result["maintenance_state"] == "blocked" and result["code"] == "rollback_unverified"
    assert result["database_restored"] is False and result["original_restored"] is False
    assert events == ["stop", ("stop", "api-quota-broker.service")]
