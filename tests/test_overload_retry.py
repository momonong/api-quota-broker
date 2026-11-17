"""Complete refusals can retry; uncertain execution must never replay."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, target, task

from quota_broker.gateway import Gateway
from quota_broker.queue import DurableQueue


class Clock:
    def __init__(self):
        self.now = NOW
        self.delays = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += timedelta(seconds=seconds)


def build(tmp_path, responses, *, requests_only=True):
    clock = Clock()
    calls = []
    route = target("google", "google", "gemini-2.5-flash-lite")
    if requests_only:
        route = replace(route, quotas=tuple(q for q in route.quotas if q.metric == "requests"))

    def transport(*args):
        calls.append(args)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, Exception):
            raise response
        status, body, headers = response
        return status, headers, json.dumps(body).encode() if not isinstance(body, bytes) else body

    gateway = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda ref: "fixture-key",
        transport,
        clock=clock,
    )
    gateway.sleep = clock.sleep
    return gateway, clock, calls


OVERLOAD = (
    503,
    {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}},
    {"Content-Type": "application/json"},
)
SUCCESS = (
    200,
    {
        "candidates": [{"content": {"parts": [{"text": "okay"}]}}],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3},
    },
    {"Content-Type": "application/json"},
)


def test_complete_overload_retries_and_counts_both_attempts(tmp_path):
    gateway, clock, calls = build(tmp_path, [OVERLOAD, SUCCESS])
    result = gateway.run(task())
    assert result["state"] == "completed"
    assert [a["state"] for a in result["attempts"]] == ["failed", "completed"]
    assert len(calls) == 2 and clock.delays and clock.delays[0] >= 1
    assert gateway.usage()[0]["requests"] == 2
    assert result["attempts"][0]["ledger_state"] == "failed"
    assert all(c["amount"] == 1 for c in result["attempts"][0]["ledger_charges"])
    assert gateway.run(task())["state"] == "completed" and len(calls) == 2


def test_refusal_retains_unknown_token_charge_but_releases_execution_slot(tmp_path):
    gateway, _clock, calls = build(tmp_path, [OVERLOAD, SUCCESS], requests_only=False)
    result = gateway.run(task())
    assert result["state"] == "completed" and len(calls) == 2
    first = result["attempts"][0]
    assert first["ledger_state"] == "unknown" and first["execution_finished_at"]
    assert any(c["metric"] == "input_tokens" and c["amount"] > 0 for c in first["ledger_charges"])
    usage = gateway.usage()[0]
    assert (
        usage["outcome_unknown_count"] == 0
        and usage["input_unknown_count"] == 1
        and usage["ledger_held_count"] == 1
    )


@pytest.mark.parametrize(
    "response",
    [
        TimeoutError(),
        (503, b'{"error":', {"Content-Type": "application/json"}),
        (502, {"error": {"code": 502}}, {}),
        (202, {"id": "pending"}, {}),
        (
            503,
            {
                "error": {"code": 503, "status": "UNAVAILABLE"},
                "candidates": [{"content": {"parts": [{"text": "partial"}]}}],
            },
            {"Content-Type": "application/json"},
        ),
    ],
)
def test_uncertain_or_partial_result_never_replays(tmp_path, response):
    gateway, clock, calls = build(tmp_path, [response, SUCCESS])
    result = gateway.run(task())
    assert result["state"] == "unknown" and len(calls) == 1 and clock.delays == []
    gateway.run(task())
    assert len(calls) == 1


def test_retry_attempt_limit_and_retry_after_deadline(tmp_path):
    gateway, clock, calls = build(tmp_path, [OVERLOAD])
    result = gateway.run({**task(), "max_attempts": 3})
    assert result["state"] == "failed" and len(calls) == 3
    other = tmp_path / "deadline"
    other.mkdir()
    gateway, clock, calls = build(
        other, [(OVERLOAD[0], OVERLOAD[1], {**OVERLOAD[2], "Retry-After": "60"})]
    )
    result = gateway.run({**task(), "deadline": (NOW + timedelta(seconds=10)).isoformat()})
    assert result["state"] == "failed" and len(calls) == 1 and clock.delays == []


def test_queue_persists_retry_across_restart_and_keeps_one_business_request(tmp_path):
    gateway, clock, calls = build(tmp_path, [OVERLOAD, SUCCESS])
    Path(gateway.db).chmod(0o600)
    cipher = b"retry-queue-fixture-key".ljust(32, b"!")
    queue = DurableQueue(gateway, cipher)
    queue.submit({**task(), "max_attempts": 3})
    result = queue.tick("worker-one")
    assert result["state"] == "waiting" and len(calls) == 1
    assert result["attempt_budget_used"] == 1 and result["next_retry_at"]
    assert clock.delays == []
    clock.sleep(10)
    queue = DurableQueue(gateway, cipher)
    assert queue.tick("worker-two")["state"] == "completed"
    assert queue.submit({**task(), "max_attempts": 3})["state"] == "completed"
    assert queue.status("fixture-task")["attempt_budget_used"] == 2 and len(calls) == 2
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM queue_jobs").fetchone()[0] == 1


def test_new_default_retry_budget_preserves_historical_default_request_identity(tmp_path):
    gateway, _clock, calls = build(tmp_path, [SUCCESS])
    Path(gateway.db).chmod(0o600)
    queue = DurableQueue(gateway, b"retry-fixture-key".ljust(32, b"!"))
    # The prior version materialized an omitted max_attempts as 32 in its HMAC.
    queue.submit({**task(), "max_attempts": 32})
    assert queue.submit(task())["max_attempts"] == 32
    assert queue.submit(task("new-request"))["max_attempts"] == 3
    assert calls == []


def test_queue_overload_backoff_uses_durable_total_attempts_across_restart(tmp_path):
    from datetime import datetime

    gateway, clock, calls = build(tmp_path, [OVERLOAD])
    Path(gateway.db).chmod(0o600)
    cipher = b"retry-durable-backoff".ljust(32, b"!")
    queue = DurableQueue(gateway, cipher)
    queue.submit({**task(), "max_attempts": 5})
    delays = []
    for attempt in range(4):
        now = clock()
        result = queue.tick("worker")
        assert result["state"] == "waiting"
        retry_at = datetime.fromisoformat(result["next_retry_at"])
        delays.append((retry_at - now).total_seconds())
        assert result["attempt_budget_used"] == attempt + 1
        clock.now = retry_at
        queue = DurableQueue(gateway, cipher)
    assert all(b > a for a, b in pairwise(delays))
    assert all(5 <= delay <= 38 for delay in delays)
    assert any(delay != int(delay) for delay in delays)
    assert queue.tick("worker")["state"] == "failed"
    assert len(calls) == 5
