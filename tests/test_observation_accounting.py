"""Reset boundaries and filtered ledger reports must preserve uncertainty."""

import sqlite3
from datetime import timedelta

import pytest
from test_dashboard import dashboard
from test_dashboard_reports import recorded_attempts
from test_framework import build, observation
from test_gateway import NOW, target, task


@pytest.mark.parametrize("elapsed", [59, 60, 61, 4 * 86400])
def test_observation_reset_expires_before_valid_until_without_inventing_balance(tmp_path, elapsed):
    gateway, clock, calls = build(tmp_path, [target("a", "groq", "openai/gpt-oss-20b")])
    item = observation(7)
    item["valid_until"] = (NOW + timedelta(days=7)).isoformat()
    gateway.observe_quota("a", item)
    clock.now = NOW + timedelta(seconds=elapsed)
    # Direct observation read avoids conflating this regression with eligibility expiry.
    with sqlite3.connect(gateway.db) as con:
        row = gateway.routing.observations(con, "groq:account:fixture-account", clock.now)[0]
    assert row["remaining"] == 7
    assert row["current"] is (elapsed < 60)
    assert row["freshness_reason"] == ("current" if elapsed < 60 else "reset_reached")
    assert row["effective_remaining"] == (7 if elapsed < 60 else None)
    assert calls == []


def test_dashboard_does_not_revalidate_reset_observation_from_old_upstream(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.diagnostics

    def diagnostics():
        result = original()
        row = result["targets"][0]["quota_observations"][0]
        row.update(reset_at=NOW.isoformat(), current=True)
        return result

    source.diagnostics = diagnostics
    row = board.snapshot()["targets"][0]["observations"][0]
    assert row["current"] is False
    assert row["freshness_reason"] == "reset_reached"
    assert row["effective_remaining"] is None
    assert board.advice(task())["decision"] == "ask_user"


def test_provider_summary_joins_same_filtered_history_once(tmp_path):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    calls = []
    original = source.usage_history

    def history(filters):
        calls.append(dict(filters))
        return original(filters)

    source.usage_history = history
    report = board.usage_report({"provider": "google", "task": "unknown"})
    row = report["provider_usage"]["providers"][0]
    assert row["local_recorded_generation_attempts"] == 9
    # Legacy fixture rows have no reservation identity, so held state is unknown.
    assert row["ledger_held_count"] is None
    assert row["local_ledger_state"] == "available"
    metric = next(m for m in row["local_reported_metrics"] if m["metric"] == "input_tokens")
    assert metric == {
        "metric": "input_tokens",
        "reported": 90,
        "unknown_count": 0,
        "requests": 9,
        "coverage": "complete",
    }
    assert sum("group_by" not in c for c in calls) == 1
    assert (
        board.usage_report({"provider": "google", "task": "text_generation"})["provider_usage"][
            "providers"
        ][0]["local_recorded_generation_attempts"]
        == 0
    )


def test_failed_history_cannot_claim_available_or_zero_holds(tmp_path):
    board, source = dashboard(tmp_path)
    source.usage_history = lambda _: (_ for _ in ()).throw(RuntimeError("fixture failure"))
    report = board.usage_report({})
    assert report["rows"] is None
    assert report["provider_usage"]["local_ledger_state"] == "unavailable"
    for row in report["provider_usage"]["providers"]:
        assert row["local_recorded_generation_attempts"] is None
        assert row["ledger_held_count"] is None


def test_partial_metric_sums_and_missing_legacy_coverage_are_preserved(tmp_path):
    board, source = dashboard(tmp_path)
    base = {"provider": "google", "model": "gemini-3.5-flash-lite", "task_type": "unknown"}
    source.usage_history = lambda _: {
        "task_types_recorded": True,
        "rows": [
            {
                **base,
                "requests": 2,
                "reported_input_tokens": 12,
                "input_unknown_count": 1,
                "ledger_held_count": 1,
            },
            {**base, "requests": 1, "reported_input_tokens": None, "input_unknown_count": 1},
        ],
    }
    report = board.usage_report({})
    row = report["provider_usage"]["providers"][0]
    assert row["local_recorded_generation_attempts"] == 3
    assert row["ledger_held_count"] is None
    metrics = {m["metric"]: m for m in row["local_reported_metrics"]}
    assert metrics["input_tokens"]["reported"] == 12
    assert metrics["input_tokens"]["unknown_count"] == 2
    assert metrics["input_tokens"]["coverage"] == "partial"
    assert metrics["total_tokens"]["unknown_count"] is None
    assert metrics["total_tokens"]["coverage"] == "unknown"


@pytest.mark.parametrize("timeline_available", [True, False])
def test_timeline_failure_is_separate_from_successful_aggregate(tmp_path, timeline_available):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    aggregate = source.usage_history

    def history(filters):
        if filters.get("group_by") == "day":
            if not timeline_available:
                raise RuntimeError("old upstream")
            return {
                "task_types_recorded": True,
                "rows": [
                    {
                        "date": "2026-09-30",
                        "requests": 6,
                        "completed_count": 6,
                        "failed_count": 0,
                        "outcome_unknown_count": 0,
                        "reported_total_tokens": 72,
                        "total_tokens_unknown_count": 0,
                        "ledger_held_count": 0,
                        "private_input": "must not leak",
                    }
                ],
            }
        return aggregate(filters)

    source.usage_history = history
    report = board.usage_report({"provider": "google"})
    assert report["source"]["state"] == "available"
    assert report["provider_usage"]["providers"][0]["local_recorded_generation_attempts"] == 9
    assert report["timeline_state"] == ("available" if timeline_available else "unavailable")
    if timeline_available:
        assert report["timeline"][0]["requests"] == 6
        assert "private_input" not in report["timeline"][0]
    else:
        assert report["timeline"] is None


def test_advice_projects_measured_ranking_factors_without_inventing_missing_scores(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.explain

    def explain(raw):
        plan = original(raw)
        plan["candidates"][0]["ranking_factors"].update(
            algorithm="expiry_utility_v1",
            score=81.5,
            components={
                "expiry_urgency": 0.75,
                "task_fit": 1,
                "quality": None,
                "free_headroom": float("nan"),
                "private": "not public",
            },
            quality_basis="unmeasured",
            quality_samples=0,
            task_profile="coding",
            reliability_samples=4,
        )
        return plan

    source.explain = explain
    row = board.advice(task())["candidates"][0]["ranking"]
    assert row["algorithm"] == "expiry_utility_v1"
    assert row["score"] == 81.5
    assert row["components"]["expiry_urgency"] == 0.75
    assert row["components"]["quality"] is None
    assert row["components"]["free_headroom"] is None
    assert "private" not in row["components"]
    assert row["quality_basis"] == "unmeasured"
    assert row["task_profile"] == "coding"
    assert row["reliability_samples"] == 4


def test_daily_balance_cannot_use_monthly_reset_or_outlive_a_daily_window():
    from quota_broker.routing import observation_freshness

    item = {
        "as_of": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
        "reset_at": (NOW + timedelta(days=20)).isoformat(),
        "window": "day",
    }
    assert observation_freshness(item, NOW) == "window_mismatch"
    item["reset_at"] = None
    assert observation_freshness(item, NOW + timedelta(days=2)) == "window_elapsed"


def test_provider_ledger_distinguishes_explicit_not_applicable_from_missing_field():
    from quota_broker.dashboard import Dashboard

    report = {"providers": [{"provider": "google"}]}
    rows = [
        {"provider": "google", "requests": 1, "reported_images": None, "images_unknown_count": None}
    ]
    Dashboard._join_provider_ledger(report, rows)
    metrics = {m["metric"]: m for m in report["providers"][0]["local_reported_metrics"]}
    assert metrics["images"]["coverage"] == "not_applicable"
    assert metrics["audio_seconds"]["coverage"] == "unknown"
    rows.append(
        {"provider": "google", "requests": 1, "reported_images": 2, "images_unknown_count": 0}
    )
    Dashboard._join_provider_ledger(report, rows)
    metric = next(
        m for m in report["providers"][0]["local_reported_metrics"] if m["metric"] == "images"
    )
    assert metric["coverage"] == "complete" and metric["reported"] == 2


def test_daily_projection_preserves_missing_usage_coverage_fields(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.usage_history

    def history(filters):
        if filters.get("group_by") == "day":
            return {
                "task_types_recorded": True,
                "rows": [
                    {
                        "date": "2026-09-30",
                        "requests": 1,
                        "completed_count": 1,
                        "failed_count": 0,
                        "outcome_unknown_count": 0,
                    }
                ],
            }
        return original(filters)

    source.usage_history = history
    row = board.usage_report({})["timeline"][0]
    assert "reported_total_tokens" not in row
    assert "total_tokens_unknown_count" not in row
