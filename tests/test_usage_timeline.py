import json
import sqlite3
from datetime import timedelta

import pytest
from test_gateway import NOW, make_gateway, target, task

from quota_broker.gateway import GatewayError


def test_daily_usage_preserves_known_total_and_missing_coverage(tmp_path):
    gateway = make_gateway(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], [])
    gateway.run(task("first"))
    gateway.clock = lambda: NOW + timedelta(days=1)
    # Seed the second day from an actual dispatch, without asking for expired evidence.
    gateway.clock = lambda: NOW
    gateway.run(task("second"))
    with sqlite3.connect(gateway.db) as con:
        con.execute(
            "UPDATE gateway_attempts SET reported_resources_json=? WHERE request_key='first'",
            (json.dumps({"requests": 1, "total_tokens": 14}),),
        )
        con.execute(
            "UPDATE gateway_attempts SET reported_input_tokens=NULL,reported_output_tokens=NULL,reported_resources_json=NULL,dispatched_at=? WHERE request_key='second'",
            ((NOW + timedelta(days=1)).isoformat(),),
        )
        con.execute(
            "UPDATE reservations SET dispatched_at=? WHERE id=(SELECT reservation_id FROM gateway_attempts WHERE request_key='second')",
            ((NOW + timedelta(days=1)).isoformat(),),
        )
    rows = gateway.usage()
    assert rows[0]["reported_total_tokens"] == 14
    assert rows[0]["total_tokens_unknown_count"] == 1
    days = gateway.usage(group_by="day")
    assert [r["date"] for r in days] == ["2026-09-30", "2026-10-01"]
    assert [(r["requests"], r["completed_count"]) for r in days] == [(1, 1), (1, 1)]
    assert days[0]["reported_total_tokens"] == 14 and days[0]["total_tokens_unknown_count"] == 0
    assert days[1]["reported_total_tokens"] is None and days[1]["total_tokens_unknown_count"] == 1
    assert gateway.usage(group_by="day", to_at=(NOW + timedelta(hours=1)).isoformat()) == days[:1]


def test_usage_rejects_arbitrary_group_by_and_separates_inapplicable_metrics(tmp_path):
    gateway = make_gateway(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], [])
    gateway.run(task())
    row = gateway.usage()[0]
    assert row["audio_seconds_unknown_count"] is None
    assert row["images_unknown_count"] is None
    assert row["total_tokens_unknown_count"] == 1
    assert row["reported_total_tokens"] is None
    assert row["reported_input_tokens"] == 11 and row["reported_output_tokens"] == 3
    with pytest.raises(GatewayError):
        gateway.usage(group_by="project_id; SELECT * FROM secrets")


@pytest.mark.parametrize("first", ["google", "cloudflare"])
def test_daily_usage_counts_unknown_only_on_applicable_provider_rows(tmp_path, first):
    routes = [
        target("google", "google", "gemini-2.5-flash-lite"),
        target("cloudflare", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
    ]
    gateway = make_gateway(tmp_path, routes, [], missing_usage=True)
    for provider in (first, "cloudflare" if first == "google" else "google"):
        gateway.run(task(provider, provider=provider, neurons=30))
    day = gateway.usage(group_by="day")[0]
    assert day["requests"] == 2
    assert day["neurons_unknown_count"] == 1
    assert day["input_unknown_count"] == 2


@pytest.mark.parametrize("total", [19, None])
def test_legacy_google_preserves_explicit_thinking_total_without_inventing_it(tmp_path, total):
    from quota_broker.gateway import Gateway

    gateway = make_gateway(tmp_path, [target("google", "google", "gemini-2.5-flash-lite")], [])
    counters = {"promptTokenCount": 11, "candidatesTokenCount": 3, "thoughtsTokenCount": 5}
    if total is not None:
        counters["totalTokenCount"] = total
    body = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}], "usageMetadata": counters}
    gateway.transport = lambda *args: (
        200,
        {"Content-Type": "application/json"},
        json.dumps(body).encode(),
    )
    assert gateway.run(task())["state"] == "completed"
    restored = Gateway(
        gateway.db, gateway.targets, gateway.digest_key, gateway.secret_resolver, clock=lambda: NOW
    )
    for report in (restored.usage()[0], restored.usage(group_by="day")[0]):
        assert report["reported_total_tokens"] == total
        assert report["total_tokens_unknown_count"] == int(total is None)
        assert report["reported_input_tokens"] == 11 and report["reported_output_tokens"] == 3


@pytest.mark.parametrize(
    "usage",
    [
        b'"promptTokenCount":100,"promptTokenCount":1,"candidatesTokenCount":3,"totalTokenCount":4',
        b'"promptTokenCount":11,"candidatesTokenCount":3,"thoughtsTokenCount":5,"totalTokenCount":14',
    ],
)
def test_invalid_usage_keeps_answer_but_never_settles_legacy_token_hold(tmp_path, usage):
    gateway = make_gateway(tmp_path, [target("google", "google", "gemini-2.5-flash-lite")], [])
    raw = (
        b'{"candidates":[{"content":{"parts":[{"text":"answer"}]}}],"usageMetadata":{'
        + usage
        + b"}}"
    )
    gateway.transport = lambda *args: (200, {"Content-Type": "application/json"}, raw)
    result = gateway.run(task())
    assert result["answer"] == "answer"
    assert result["state"] == "completed_usage_unknown"
    assert result["ledger_state"] == "unknown"
    assert result["reported_input_tokens"] is None
    assert gateway.usage()[0]["ledger_held_count"] == 1
