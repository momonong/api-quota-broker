"""Explicit operator action releases slots only, never uncertain budget or identity."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from test_gateway_access import TOKENS, policy_file
from test_shared_credit_admission import body, run_project, setup_credit

from quota_broker.core import BrokerError
from quota_broker.gateway import GatewayError

ACK = "af197ad9-cdf5-4eca-a221-ab1f71ca99b8"
HUMAN = "e0834f9c-44ed-4086-8e64-bd692c594b39"


def fixture(tmp_path):
    gateway, _item, calls, clock, access = setup_credit(
        tmp_path, scoped=True, remaining=200, unknown=True
    )
    policy_file(tmp_path, inflight=1)
    result = run_project(access, "a", "original")
    assert result["state"] == "unknown"
    with sqlite3.connect(gateway.db) as con:
        reservation = con.execute("SELECT reservation_id FROM gateway_attempts").fetchone()[0]
    clock[0] += timedelta(seconds=181)
    return gateway, _item, calls, clock, access, reservation


def snapshot(db):
    with sqlite3.connect(db) as con:
        return {
            table: con.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("charges", "observed_holds", "reports", "execution_completion")
        }


def test_explicit_ack_keeps_unknown_budget_and_original_key_while_releasing_slots(tmp_path):
    gateway, item, calls, clock, access, reservation = fixture(tmp_path)
    before = snapshot(gateway.db)
    with pytest.raises(GatewayError, match="inflight"):
        access.claim(access.policy.authenticate(TOKENS["a"]), "tasks", body("next"))
    evidence = gateway.broker.preview_unknown_slot(reservation, "a")
    receipt = gateway.broker.acknowledge_unknown_slot(
        reservation, "a", ACK, HUMAN, evidence["sha256"]
    )
    assert receipt["outcome"] == "unknown" and receipt["budget_released"] is False
    assert snapshot(gateway.db) == before
    assert run_project(access, "a", "original")["state"] == "unknown"
    assert len(calls) == 1
    assert gateway.explain(body("next"))["selected_target_id"] == item.id
    access.claim(access.policy.authenticate(TOKENS["a"]), "tasks", body("next"))
    with sqlite3.connect(gateway.db) as con:
        observation = gateway._quota_observations(con, item, clock[0])[0]
    assert observation["local_observation_hold"] == 100
    assert observation["effective_remaining"] == 100
    assert (
        gateway.broker.acknowledge_unknown_slot(reservation, "a", ACK, HUMAN, evidence["sha256"])
        == receipt
    )
    with pytest.raises(BrokerError):
        gateway.broker.acknowledge_unknown_slot(reservation, "a", ACK, "f" * 36, evidence["sha256"])


def test_ack_requires_exact_owner_evidence_and_finished_execution(tmp_path):
    gateway, _item, _calls, clock, _access, reservation = fixture(tmp_path)
    with pytest.raises(BrokerError):
        gateway.broker.preview_unknown_slot(reservation, "b")
    gateway.broker.preview_unknown_slot(reservation, "a")
    with pytest.raises(BrokerError):
        gateway.broker.acknowledge_unknown_slot(reservation, "a", ACK, HUMAN, "0" * 64)
    clock[0] -= timedelta(seconds=181)
    with pytest.raises(BrokerError):
        gateway.broker.preview_unknown_slot(reservation, "a")
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM unknown_slot_acknowledgements").fetchone()[0] == 0


def test_queued_unknown_requires_all_attempts_and_keeps_queue_nonreplayable(tmp_path):
    from quota_broker.queue import DurableQueue

    gateway, _item, calls, clock, access = setup_credit(tmp_path, scoped=True, unknown=True)
    policy_file(tmp_path, inflight=1)
    Path(gateway.db).chmod(0o600)
    queue = DurableQueue(gateway, b"fixture-queue-key".ljust(32, b"!"))
    principal = access.policy.authenticate(TOKENS["a"])
    internal, data = access.claim(principal, "jobs", {**body("job"), "max_attempts": 1})
    queue.submit(data)
    queue.tick("fixture-worker")
    assert queue.status(internal)["state"] == "unknown" and len(calls) == 1
    clock[0] += timedelta(seconds=181)
    with sqlite3.connect(gateway.db) as con:
        reservation = con.execute("SELECT reservation_id FROM gateway_attempts").fetchone()[0]
    evidence = gateway.broker.preview_unknown_slot(reservation, "a")
    before = snapshot(gateway.db)
    gateway.broker.acknowledge_unknown_slot(reservation, "a", ACK, HUMAN, evidence["sha256"])
    assert snapshot(gateway.db) == before
    assert queue.status(internal)["state"] == "unknown"
    queue.tick("fixture-worker")
    assert len(calls) == 1
    queue.submit(data)
    assert len(calls) == 1
    access.claim(principal, "jobs", {**body("different"), "max_attempts": 1})
    # An additional unresolved attempt cannot inherit the first receipt.
    with sqlite3.connect(gateway.db) as con:
        con.execute(
            "INSERT INTO queue_attempts VALUES(?,?,?,?)",
            (internal, 2, "missing-execution", clock[0].isoformat()),
        )
        assert gateway.broker.object_unknown_slots_acknowledged(con, "jobs", internal) is False


def test_ack_survives_restart_and_does_not_make_old_unknown_a_completed_execution(tmp_path):
    from quota_broker.core import Broker

    gateway, item, calls, clock, _access, reservation = fixture(tmp_path)
    before = snapshot(gateway.db)
    evidence = gateway.broker.preview_unknown_slot(reservation, "a")
    receipt = gateway.broker.acknowledge_unknown_slot(
        reservation, "a", ACK, HUMAN, evidence["sha256"]
    )
    restarted = Broker(gateway.db, (item,), clock=lambda: clock[0], registry=gateway.registry)
    assert (
        restarted.acknowledge_unknown_slot(reservation, "a", ACK, HUMAN, evidence["sha256"])
        == receipt
    )
    assert snapshot(gateway.db) == before and restarted.status(reservation)["state"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["reserved", "dispatched", "missing_owner", "missing_task"])
def test_incomplete_or_non_unknown_evidence_cannot_be_acknowledged(tmp_path, change):
    gateway, _item, _calls, _clock, _access, reservation = fixture(tmp_path)
    with sqlite3.connect(gateway.db) as con:
        if change in {"reserved", "dispatched"}:
            con.execute("UPDATE reservations SET state=? WHERE id=?", (change, reservation))
        elif change == "missing_owner":
            con.execute("DELETE FROM gateway_project_objects")
        else:
            con.execute("DELETE FROM gateway_tasks")
    with pytest.raises(BrokerError):
        gateway.broker.preview_unknown_slot(reservation, "a")
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM unknown_slot_acknowledgements").fetchone()[0] == 0


def test_acknowledged_unknown_does_not_raise_project_concurrency_limit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    gateway, _item, calls, _clock, access, reservation = fixture(tmp_path)
    evidence = gateway.broker.preview_unknown_slot(reservation, "a")
    gateway.broker.acknowledge_unknown_slot(reservation, "a", ACK, HUMAN, evidence["sha256"])
    principal = access.policy.authenticate(TOKENS["a"])

    def claim(key):
        try:
            access.claim(principal, "tasks", body(key))
            return "claimed"
        except GatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["new-a", "new-b"]))
    assert sorted(results) == ["claimed", "project_limit"]
    assert len(calls) == 1
