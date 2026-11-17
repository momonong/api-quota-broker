"""Allocation tests exercise trade-offs, not hard-coded provider ordering."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_gateway import NOW, make_gateway, target, task
from test_quota_priority import capacity


def test_urgency_dominates_quality_when_free_credit_is_about_to_expire(tmp_path):
    urgent = target(
        "urgent", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=60)
    )
    later = target(
        "later", "google", "gemini-2.5-flash-lite", capacity=capacity(credit_seconds=86400)
    )
    gateway = make_gateway(tmp_path, [later, urgent], [])
    gateway.observe_quality(
        "google",
        later.model,
        "general",
        passed=100,
        total=100,
        evaluation_id="quality-later",
        as_of=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    gateway.observe_quality(
        "nvidia",
        urgent.model,
        "general",
        passed=10,
        total=100,
        evaluation_id="quality-urgent",
        as_of=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    plan = gateway.explain(task())
    assert plan["selected_target_id"] == "urgent"
    assert plan["candidates"][0]["ranking_factors"]["algorithm"] == "expiry_utility_v1"


def test_quality_and_task_fit_choose_coding_model_for_similar_expiry(tmp_path):
    a = target(
        "aaa-general", "google", "gemini-2.5-flash-lite", capacity=capacity(credit_seconds=3600)
    )
    b = target(
        "zzz-coding", "nvidia", "google/gemma-4-31b-it", capacity=capacity(credit_seconds=3700)
    )
    gateway = make_gateway(tmp_path, [a, b], [])
    from quota_broker.registry import Registry

    models = dict(gateway.registry.models)
    models[(b.provider, b.model)] = replace(
        models[(b.provider, b.model)], task_profiles=("coding",)
    )
    gateway.registry = Registry(models)
    gateway.broker.registry = gateway.registry
    gateway.observe_quality(
        b.provider,
        b.model,
        "coding",
        passed=19,
        total=20,
        evaluation_id="coding-suite",
        as_of=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    request = {**task(), "requirements": {"profile": "coding"}}
    plan = gateway.explain(request)
    assert plan["selected_target_id"] == "zzz-coding"
    factors = plan["candidates"][0]["ranking_factors"]
    assert factors["task_profile"] == "coding"
    assert factors["quality_samples"] == 20
    assert factors["quality_basis"] == "measured"


def test_unknown_latency_is_not_faster_than_observed_fast_success(tmp_path):
    a = target("a-unknown", "google", "gemini-2.5-flash-lite")
    b = target("z-known", "nvidia", "google/gemma-4-31b-it")
    gateway = make_gateway(tmp_path, [a, b], [])
    gateway.routing.record(b.id, NOW, 200, 50, True, None, None)
    assert gateway.explain(task())["selected_target_id"] == b.id


def test_quality_evidence_expires_is_deduplicated_and_rejects_invalid_counts(tmp_path):
    a = target("a", "google", "gemini-2.5-flash-lite")
    gateway = make_gateway(tmp_path, [a], [])
    kwargs = {
        "passed": 8,
        "total": 10,
        "evaluation_id": "suite-v1",
        "as_of": NOW,
        "valid_until": NOW + timedelta(seconds=5),
    }
    gateway.observe_quality(a.provider, a.model, "general", **kwargs)
    gateway.observe_quality(a.provider, a.model, "general", **kwargs)
    assert gateway.explain(task())["candidates"][0]["ranking_factors"]["quality_samples"] == 10
    gateway.clock = lambda: NOW + timedelta(seconds=6)
    factors = gateway.explain(task())["candidates"][0]["ranking_factors"]
    assert factors["quality_samples"] == 0 and factors["quality_basis"] == "unmeasured"
    with pytest.raises(ValueError):
        gateway.observe_quality(a.provider, a.model, "general", **{**kwargs, "passed": 11})


def test_quality_cannot_bypass_free_or_feature_gates(tmp_path):
    a = replace(target("a", "google", "gemini-2.5-flash-lite"), billing_enabled=True)
    gateway = make_gateway(tmp_path, [a], [])
    gateway.observe_quality(
        a.provider,
        a.model,
        "general",
        passed=10,
        total=10,
        evaluation_id="suite",
        as_of=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    assert gateway.explain(task())["selected_target_id"] is None


def test_structured_text_preserves_requested_coding_profile(tmp_path):
    gateway = make_gateway(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], [])
    data = {
        **task(),
        "input": {"messages": [{"role": "user", "content": "Write a function"}]},
        "requirements": {"profile": "coding"},
    }
    assert gateway.validate_task(data)["requirements"]["profile"] == "coding"
    assert gateway.explain(data)["candidates"][0]["ranking_factors"]["task_profile"] == "coding"


def test_only_administrator_can_record_quality_evaluation(tmp_path):
    from test_framework import ADMIN, TOKEN, close, serve
    from test_gateway_access import request

    gateway = make_gateway(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], [])
    server, thread, base = serve(gateway)
    evaluation = {
        "profile": "coding",
        "passed": 9,
        "total": 10,
        "evaluation_id": "coding-smoke-v1",
        "as_of": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=1)).isoformat(),
    }
    try:
        assert request(base, TOKEN, "POST", "/v1/admin/targets/a/quality", evaluation)[0] == 401
        assert request(base, ADMIN, "POST", "/v1/admin/targets/a/quality", evaluation)[0] == 200
        row = gateway.explain({**task(), "requirements": {"profile": "coding"}})["candidates"][0]
        assert row["ranking_factors"]["quality_samples"] == 10
        assert (
            request(
                base, ADMIN, "POST", "/v1/admin/targets/a/quality", {**evaluation, "passed": 11}
            )[0]
            == 400
        )
    finally:
        close(server, thread)
