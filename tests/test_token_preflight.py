"""Durable PDF count/generation separation; no real credentials or network."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from test_framework import QUEUE_KEY, observation
from test_orderflow_pdf_contract import build, pdf_task

from quota_broker.family_transport import json_request
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.queue import DurableQueue
from quota_broker.token_preflight import estimate


def enabled(tmp_path, *, count=300, status=200, failure=None):
    gateway, generations, _, secrets = build(tmp_path, estimate=False)
    gateway.google_pdf_count_tokens = True
    original = gateway.family_transport
    counts = []

    def transport(request, timeout, limit):
        if request.url.endswith(":countTokens"):
            counts.append(request)
            assert limit == 65536 and 0 < timeout <= 30
            with closing(sqlite3.connect(gateway.db)) as con:
                assert (
                    con.execute("SELECT state FROM gateway_token_counts").fetchone()[0] == "intent"
                )
            if failure:
                raise failure
            return status, {}, json.dumps({"totalTokens": count}).encode()
        return original(request, timeout, limit)

    gateway.family_transport = transport
    return gateway, counts, generations, secrets


def test_queries_never_count_and_queue_counts_once_full_wire_then_generates(tmp_path):
    gateway, counts, generations, secrets = enabled(tmp_path)
    queue = DurableQueue(gateway, QUEUE_KEY)
    task = pdf_task()
    plan = gateway.explain(task)
    assert plan["selected_target_id"] is None
    assert plan["preflight_target_id"] == "google-pdf" and plan["preflight_required"]
    assert plan["estimated_input_tokens"] is None
    gateway.catalog()
    gateway.diagnostics()
    queue.submit(task)
    queue.status(task["request_key"])
    assert not counts and not generations and not secrets
    assert queue.tick("worker")["state"] == "completed"
    result = queue.result(task["request_key"])
    assert result["estimated_input_tokens"] == 616
    assert result["reported_input_tokens"] == 300
    assert result["token_preflight"]["provenance"] == "estimated"
    assert result["token_preflight"]["counted_tokens"] == 300
    counted = counts[0].payload["generateContentRequest"]
    assert counted == {"model": "models/gemini-3.5-flash-lite", **generations[0].payload}
    assert len(counts) == len(generations) == 1
    # Fresh queue object and original key reads cannot replay either POST.
    restarted = DurableQueue(gateway, QUEUE_KEY)
    assert restarted.result(task["request_key"]) == result
    assert restarted.submit(task)["state"] == "completed"
    assert restarted.tick("restarted") is None
    assert gateway.status(result["request_key"])["state"] == "completed"
    assert len(counts) == len(generations) == 1
    with closing(sqlite3.connect(gateway.db)) as con:
        saved = str(con.execute("SELECT * FROM gateway_token_counts").fetchall())
        assert "Extract line items" not in saved and "fixture-provider-credential" not in saved
        assert con.execute("SELECT count(*) FROM gateway_count_charges").fetchone()[0] == 2


@pytest.mark.parametrize("count", [True, -1, 0, 1048577, "300", None])
def test_invalid_count_never_generates_and_original_key_never_repeats(tmp_path, count):
    gateway, counts, generations, _ = enabled(tmp_path, count=count)
    result = gateway.run(pdf_task())
    assert result["state"] == "rejected"
    assert result["token_preflight"]["state"] == "unknown"
    assert gateway.run(pdf_task()) == result
    assert len(counts) == 1 and not generations


@pytest.mark.parametrize(
    "failure,status,expected",
    [(TimeoutError(), 200, "unknown"), (None, 429, "rejected"), (None, 500, "unknown")],
)
def test_count_refusal_or_timeout_stops_queue(tmp_path, failure, status, expected):
    gateway, counts, generations, _ = enabled(tmp_path, failure=failure, status=status)
    queue = DurableQueue(gateway, QUEUE_KEY)
    queue.submit(pdf_task())
    outcome = queue.tick("worker")
    assert outcome["state"] == ("unknown" if expected == "unknown" else "failed")
    assert queue.tick("worker") is None
    assert len(counts) == 1 and not generations


@pytest.mark.parametrize("change", ["disabled", "billing", "expired", "output", "rpm"])
def test_local_gates_reject_before_count_or_secret(tmp_path, change):
    gateway, counts, generations, secrets = enabled(tmp_path)
    target = gateway.targets[0]
    kwargs = {
        "disabled": {"enabled": False},
        "billing": {"billing_enabled": True},
        "expired": {"expires_at": gateway.clock()},
        "output": {"max_output_tokens": 100},
        "rpm": {
            "quotas": tuple(
                replace(q, limit=1) if q.metric == "requests" else q for q in target.quotas
            )
        },
    }[change]
    gateway.targets = gateway.broker.targets = (replace(target, **kwargs),)
    assert gateway.run(pdf_task())["state"] == "rejected"
    assert not counts and not generations and not secrets


def test_large_valid_count_rejected_by_local_input_cap_before_generation(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path, count=10000)
    with pytest.raises(GatewayError):
        gateway.run(pdf_task())
    assert len(counts) == 1 and not generations
    assert gateway.status(pdf_task()["request_key"])["token_preflight"]["estimated_tokens"] == 12256


def test_count_request_charges_share_local_request_safety_cap(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path)
    target = gateway.targets[0]
    target = replace(
        target,
        quotas=tuple(replace(q, limit=3) if q.metric == "requests" else q for q in target.quotas),
    )
    gateway.targets = gateway.broker.targets = (target,)
    assert gateway.run(pdf_task())["state"] == "completed"
    assert gateway.run({**pdf_task(), "request_key": "second"})["state"] == "rejected"
    assert len(counts) == len(generations) == 1


def test_expired_estimate_is_not_used_for_generation(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path)
    original = gateway.secret_resolver
    lookups = 0
    now = gateway.clock()

    def resolve(ref):
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            gateway.clock = lambda: now + timedelta(seconds=121)
        return original(ref)

    gateway.secret_resolver = resolve
    with pytest.raises(GatewayError):
        gateway.run(pdf_task())
    assert len(counts) == 1 and not generations


def test_crash_after_count_intent_is_not_resumable(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path, failure=KeyboardInterrupt())
    task = pdf_task()
    with pytest.raises(KeyboardInterrupt):
        gateway.run(task)
    with closing(sqlite3.connect(gateway.db)) as con:
        assert not Gateway.can_resume_preparing(con, task["request_key"])
    gateway.run(task)
    assert len(counts) == 1 and not generations
    assert gateway.explain({**task, "request_key": "new"})["preflight_target_id"] is None


def test_estimate_margin_rounds_up():
    assert estimate(b'{"totalTokens":1}') == (1, 258)
    assert estimate(b'{"totalTokens":300}') == (300, 616)


def test_unknown_count_does_not_block_independent_text_or_pdf_work(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path, failure=TimeoutError())
    task = pdf_task()
    original = gateway.family_transport
    assert gateway.run(task)["token_preflight"]["state"] == "unknown"
    assert gateway.run(task)["state"] == "rejected"
    assert len(counts) == 1
    assert gateway.explain({**task, "request_key": "different-pdf"})["preflight_target_id"]
    result = gateway.run(
        {
            "request_key": "independent-text",
            "capability": "text_generation",
            "provider": "google",
            "model": task["model"],
            "input": "Reply OK.",
            "max_output_tokens": 32,
        }
    )
    assert result["state"] == "completed"
    assert gateway.family_transport is original and not generations


def test_changed_wire_is_rejected_after_count(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path)
    original = gateway.registry.request_task
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        request = original(*args, **kwargs)
        if calls == 2:
            request.payload["generationConfig"]["temperature"] = 0.5
            return json_request(request.url, request.headers, request.payload)
        return request

    gateway.registry.request_task = changed
    with pytest.raises(GatewayError):
        gateway.run(pdf_task())
    assert len(counts) == 1 and not generations


@pytest.mark.parametrize("remaining", [1, 2])
def test_observed_request_budget_includes_auxiliary_post(tmp_path, remaining):
    gateway, counts, generations, secrets = enabled(tmp_path)
    gateway.observe_quota(
        "google-pdf", observation(remaining, at=gateway.clock() - timedelta(seconds=1))
    )
    result = gateway.run(pdf_task())
    if remaining == 1:
        assert result["state"] == "rejected"
        assert not counts and not generations and not secrets
    else:
        assert result["state"] == "completed"
        assert len(counts) == len(generations) == 1
        with gateway.broker._tx() as con:
            values = gateway._quota_observations(con, gateway.targets[0], gateway.clock())
        assert values[0]["effective_remaining"] == 0


def test_queue_crash_after_completed_count_never_allocates_new_execution(tmp_path):
    gateway, counts, generations, _ = enabled(tmp_path)
    queue = DurableQueue(gateway, QUEUE_KEY)
    task = {**pdf_task(), "max_attempts": 2, "wait_policy": "wait"}
    queue.submit(task)
    original = gateway._count_pdf

    def crash(*args):
        original(*args)
        raise KeyboardInterrupt

    gateway._count_pdf = crash
    with pytest.raises(KeyboardInterrupt):
        queue.tick("worker")
    now = gateway.clock()
    gateway.clock = queue.clock = lambda: now + timedelta(seconds=181)
    assert queue.status(task["request_key"])["state"] == "unknown"
    assert queue.tick("restart-worker") is None
    assert len(counts) == 1 and not generations
