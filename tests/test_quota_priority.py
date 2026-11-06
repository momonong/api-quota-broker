"""Value-loss routing is data-driven; evidence expiry never becomes credit expiry."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_gateway import NOW, make_gateway, target, task

from quota_broker.config import Capacity, ConfigError, Evidence, QuotaFact, parse_capacity
from quota_broker.queue import DurableQueue


def capacity(kind="one_time_gift", *, refresh=None, credit_seconds=None):
    return Capacity(
        kind,
        refresh,
        NOW,
        "fixture",
        "fixture",
        NOW + timedelta(hours=1),
        credit_expires_at=NOW + timedelta(seconds=credit_seconds)
        if credit_seconds is not None
        else None,
    )


def test_earliest_actual_value_loss_precedes_priority_and_provider(tmp_path):
    calls = []
    gift = target(
        "gift", "nvidia", "google/gemma-4-31b-it", priority=99, capacity=capacity(credit_seconds=30)
    )
    renewable = target(
        "refresh",
        "google",
        "gemini-2.5-flash-lite",
        capacity=capacity("short_renewable", refresh=60),
    )
    gateway = make_gateway(tmp_path, [renewable, gift], calls)
    plan = gateway.explain(task())
    assert plan["selected_target_id"] == "gift"
    assert plan["candidates"][0]["ranking_factors"]["value_loss_seconds"] == 30
    assert plan["candidates"][0]["ranking_factors"]["value_loss_basis"] == "credit_expiry"
    gateway.targets = (replace(gift, capacity=capacity(credit_seconds=120)), renewable)
    # A 60-second interval does not prove a reset will happen before this expiry.
    assert gateway.explain(task())["selected_target_id"] == "gift"
    assert calls == []


def test_expired_credit_is_ineligible_even_when_eligibility_evidence_is_current(tmp_path):
    calls = []
    expired = target(
        "expired", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=-1)
    )
    gateway = make_gateway(tmp_path, [expired], calls)
    plan = gateway.explain(task())
    row = plan["candidates"][0]
    assert row["eligible"] is False
    assert "credit_expired" in row["reasons"]
    assert plan["permanent_rejection"] is True
    assert plan["temporary"] is False
    Path(gateway.db).chmod(0o600)
    queue = DurableQueue(gateway, b"synthetic-priority-queue-key".ljust(32, b"!"))
    queue.submit(task())
    queue.tick("synthetic-worker")
    assert queue.status("fixture-task")["state"] == "failed"
    assert queue.status("fixture-task")["error_code"] == "no_compatible_route"
    assert calls == []


def test_expired_credit_does_not_hide_another_recoverable_route(tmp_path):
    calls = []
    expired = target(
        "expired", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=-1)
    )
    renewable = target("renewable", "google", "gemini-2.5-flash-lite")
    gateway = make_gateway(tmp_path, [expired, renewable], calls)
    with sqlite3.connect(gateway.db) as con:
        con.execute(
            "INSERT INTO cooldowns(target_id,until_at,backoff_level) VALUES(?,?,1)",
            ("renewable", (NOW + timedelta(seconds=30)).isoformat()),
        )
    plan = gateway.explain(task())
    assert plan["selected_target_id"] is None
    assert plan["temporary"] is True
    assert plan["permanent_rejection"] is False
    assert calls == []


def test_evidence_expiry_does_not_claim_urgent_credit_and_old_views_stay_stable():
    old = Capacity("one_time_gift", None, NOW, "fixture", "fixture", NOW + timedelta(seconds=1))
    assert "credit_expires_at" not in old.view()
    assert parse_capacity(old.view()) == old
    known = capacity(credit_seconds=30)
    assert parse_capacity(known.view()) == known
    with pytest.raises(ConfigError):
        parse_capacity({**old.view(), "credit_expires_at": "2026-11-03"})
    with pytest.raises(ConfigError):
        parse_capacity({**Capacity().view(), "credit_expires_at": NOW.isoformat()})


def test_renewal_interval_is_not_an_actual_deadline(tmp_path):
    renewable = target(
        "renewable",
        "google",
        "gemini-2.5-flash-lite",
        capacity=Capacity(
            "short_renewable", 60, NOW, "fixture", "fixture", NOW + timedelta(hours=1)
        ),
    )
    gift = target("gift", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=120))
    gateway = make_gateway(tmp_path, [renewable, gift], [])
    rows = gateway.explain(task())["candidates"]
    assert rows[0]["target_id"] == "gift"
    r = next(r for r in rows if r["target_id"] == "renewable")
    assert r["ranking_factors"]["value_loss_seconds"] is None


def test_missing_limit_never_becomes_one_hundred_percent_headroom(tmp_path):
    route = target("partial", "google", "gemini-2.5-flash-lite")
    evidence = Evidence(1, "official", NOW, "fixture", "account", NOW + timedelta(minutes=1))
    route = replace(
        route,
        provider_quota_facts=(
            QuotaFact("requests", "day", replace(evidence, value=None), evidence),
        ),
    )
    gateway = make_gateway(tmp_path, [route], [])
    row = gateway.explain(task())["candidates"][0]
    assert row["quota_observations"][0]["effective_remaining"] == 1
    assert row["ranking_factors"]["observed_headroom"] is None


def test_exact_current_reset_can_rank_before_credit_expiry(tmp_path):
    reset = target("reset", "google", "gemini-2.5-flash-lite")
    gift = target("gift", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=120))
    gateway = make_gateway(tmp_path, [gift, reset], [])
    gateway.observe_quota(
        "reset",
        {
            "metric": "requests",
            "window": "rolling_minute",
            "remaining": 5,
            "limit": 10,
            "as_of": NOW.isoformat(),
            "valid_until": (NOW + timedelta(seconds=30)).isoformat(),
            "reset_at": (NOW + timedelta(seconds=60)).isoformat(),
            "source": "trusted_operator",
            "provenance": "observed",
            "confidence": "administrator_verified",
        },
    )
    plan = gateway.explain(task())
    assert plan["selected_target_id"] == "reset"
    factors = plan["candidates"][0]["ranking_factors"]
    assert factors["value_loss_basis"] == "quota_reset" and factors["value_loss_seconds"] == 60
    gateway.clock = lambda: NOW + timedelta(seconds=31)
    plan = gateway.explain(task())
    assert plan["selected_target_id"] == "gift"
