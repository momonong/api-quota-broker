"""Crash before reservation resumes the same durable attempt, never dispatched work."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from test_gateway import NOW, make_gateway, target, task
from test_queue import KEY, Crash

from quota_broker.queue import DurableQueue


def prepare(tmp_path, monkeypatch, *, deadline=None):
    calls = []
    gateway = make_gateway(tmp_path, [target("groq", "groq", "openai/gpt-oss-20b")], calls)
    now = [NOW]
    gateway.clock = gateway.broker.clock = lambda: now[0]
    (tmp_path / "gateway.db").chmod(0o600)
    queue = DurableQueue(gateway, KEY, lease_seconds=10)
    data = {**task(), "max_attempts": 1}
    if deadline:
        data["deadline"] = deadline
    queue.submit(data)
    reserve = gateway.broker.reserve

    def crash(_):
        raise Crash()

    monkeypatch.setattr(gateway.broker, "reserve", crash)
    with pytest.raises(Crash):
        queue.tick("old-worker")
    monkeypatch.setattr(gateway.broker, "reserve", reserve)
    before = queue.status(data["request_key"])
    assert before["state"] == "running"
    assert gateway.status(before["execution_key"])["state"] == "preparing"
    assert calls == []
    return gateway, queue, calls, now, before


def test_expired_lease_resumes_original_attempt_once(tmp_path, monkeypatch):
    _gateway, queue, calls, now, before = prepare(tmp_path, monkeypatch)
    assert queue.tick("other-worker") is None
    now[0] += timedelta(seconds=11)
    result = queue.tick("replacement-worker")
    assert result["state"] == "completed"
    assert result["execution_key"] == before["execution_key"]
    assert result["attempts"] == before["attempts"]
    assert result["attempt_count"] == result["attempt_budget_used"] == 1
    assert len(calls) == 1
    assert queue.tick("again") is None
    assert queue.submit({**task(), "max_attempts": 1})["state"] == "completed"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mutation", ["task_dispatch", "attempt", "orphan_reservation", "orphan_dispatch"]
)
def test_any_dispatch_or_reservation_evidence_prevents_resume(tmp_path, monkeypatch, mutation):
    gateway, queue, calls, now, before = prepare(tmp_path, monkeypatch)
    key = before["execution_key"]
    with closing(sqlite3.connect(gateway.db)) as con, con:
        if mutation == "task_dispatch":
            con.execute(
                "UPDATE gateway_tasks SET dispatched_at=?,state='dispatched' WHERE request_key=?",
                (NOW.isoformat(), key),
            )
        elif mutation == "attempt":
            con.execute(
                "INSERT INTO gateway_attempts(request_key,attempt_no,reservation_id,target_id,provider,model,state,created_at) VALUES(?,0,'missing','groq','groq','openai/gpt-oss-20b','preparing',?)",
                (key, NOW.isoformat()),
            )
        else:
            con.execute(
                "INSERT INTO reservations(id,request_key,fingerprint,target_id,state,created_at,expires_at) VALUES('orphan',?,'fixture','groq','reserved',?,?)",
                ("gw:" + key + ":0", NOW.isoformat(), (NOW + timedelta(minutes=1)).isoformat()),
            )
        if mutation == "orphan_dispatch":
            con.execute(
                "UPDATE reservations SET state='dispatched',dispatched_at=? WHERE id='orphan'",
                (NOW.isoformat(),),
            )
    now[0] += timedelta(seconds=11)
    queue.tick("replacement-worker")
    state = queue.status(task()["request_key"])
    assert state["state"] == (
        "unknown" if mutation in {"task_dispatch", "orphan_dispatch", "attempt"} else "failed"
    )
    assert state["attempt_count"] == 1 and calls == []


def test_expired_deadline_is_not_extended(tmp_path, monkeypatch):
    _, queue, calls, now, _ = prepare(
        tmp_path, monkeypatch, deadline=(NOW + timedelta(seconds=5)).isoformat()
    )
    now[0] += timedelta(seconds=11)
    assert queue.tick("replacement-worker") is None
    assert queue.status(task()["request_key"])["state"] == "expired"
    assert calls == []
