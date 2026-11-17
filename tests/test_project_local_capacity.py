"""Explicit local budget intent survives project admission and retry compatibility."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_gateway import fixture_transport, target, task
from test_gateway_access import TOKENS, policy_file
from test_local_capacity_advice import board, documents

from quota_broker.access import AccessPolicyFile, ProjectAccess
from quota_broker.core import Broker, BrokerError
from quota_broker.gateway import GatewayError
from quota_broker.queue import DurableQueue


def scoped(tmp_path, provider="nvidia", declared=True):
    docs = documents(provider)
    if not declared:
        docs["gateway.json"]["targets"][0].pop("required_capacity")
    ui, gateway = board(tmp_path, docs)
    Path(gateway.db).chmod(0o600)
    calls = []
    gateway.secret_resolver = lambda name: (
        "0" * 32 if name == "CLOUDFLARE_ACCOUNT_ID" else "fixture-secret-value"
    )
    gateway.transport = fixture_transport(calls)
    access = ProjectAccess(gateway, AccessPolicyFile(policy_file(tmp_path)))
    principal = access.policy.authenticate(TOKENS["a"])
    return ui, access, principal, calls


@pytest.mark.parametrize("provider", ["nvidia", "cloudflare"])
def test_json_explicit_empty_mode_aligns_real_catalog_dashboard_project_and_dispatch(
    tmp_path, provider
):
    ui, access, principal, calls = scoped(tmp_path, provider)
    gateway = access.gateway
    raw = task(provider=provider)
    if provider == "cloudflare":
        now = gateway.clock()
        gateway.observe_quota(
            "fixture-free-budget",
            {
                "metric": "neurons",
                "window": "day",
                "remaining": None,
                "limit": 10000,
                "as_of": now.isoformat(),
                "valid_until": (now + timedelta(minutes=10)).isoformat(),
                "reset_at": (now + timedelta(days=1))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat(),
                "source": "trusted_operator",
                "provenance": "official",
                "confidence": "administrator_verified",
            },
        )
    assert gateway.explain(raw)["selected_target_id"] == "fixture-free-budget"
    assert access.catalog(principal)[0]["available"]
    assert gateway.catalog()[0]["required_capacity_declared"] is True
    assert ui.advice(raw)["decision"] == "recommend"
    assert access.explain(principal, raw)["selected_target_id"] == "fixture-free-budget"
    assert calls == []
    _internal, payload = access.claim(principal, "tasks", raw)
    assert gateway.run(payload)["state"] == "completed"
    assert len(calls) == 1


def test_omitted_capacity_stays_strict_in_project_and_dashboard(tmp_path):
    ui, access, principal, calls = scoped(tmp_path, declared=False)
    assert access.gateway.explain(task(provider="nvidia"))["selected_target_id"] is not None
    assert not access.catalog(principal)[0]["available"]
    assert access.gateway.catalog()[0]["required_capacity_declared"] is False
    assert ui.advice(task(provider="nvidia"))["decision"] == "ask_user"
    assert access.explain(principal, task(provider="nvidia"))["selected_target_id"] is None
    assert calls == []


def test_programmatic_target_default_does_not_opt_in_and_snapshot_changes_only_for_local_mode(
    tmp_path,
):
    _ui, access, principal, calls = scoped(tmp_path)
    local = access.gateway.targets[0]
    assert local.required_capacity_declared is True
    legacy = replace(
        target(local.id, local.provider, local.model),
        quota_basis="local_safety_cap",
        billing_evidence_state="disabled",
    )
    assert legacy.required_capacity_declared is False
    assert "required_capacity_declared" not in json.loads(Broker._snapshot(legacy))
    assert json.loads(Broker._snapshot(local))["required_capacity_declared"] is True
    declared = replace(local, required_capacity=(("requests", "day"),))
    undeclared = replace(declared, required_capacity_declared=False)
    assert Broker._snapshot(declared) == Broker._snapshot(undeclared)
    access.gateway.targets = access.gateway.broker.targets = (legacy,)
    assert not access.catalog(principal)[0]["available"]
    assert calls == []


@pytest.mark.parametrize("change", ["inventory", "billing_unknown", "free", "budget"])
def test_local_project_rechecks_safety_after_claim_before_provider_dispatch(tmp_path, change):
    _ui, access, principal, calls = scoped(tmp_path)
    gateway = access.gateway
    _internal, payload = access.claim(principal, "tasks", task(provider="nvidia"))
    if change == "inventory":
        gateway.secret_inventory = None
    elif change == "budget":
        route = gateway.targets[0]
        with sqlite3.connect(gateway.db) as con:
            # Existing current-window charges from a different trusted client consume the cap.
            day = next(q for q in route.quotas if q.window == "day")
            from quota_broker.core import calendar_bounds, stamp

            con.execute(
                "INSERT INTO charges VALUES(?,?,?,?,?,?)",
                (
                    "fixture-prior",
                    day.bucket,
                    day.metric,
                    day.limit,
                    stamp(gateway.clock()),
                    stamp(calendar_bounds(gateway.clock(), day)[0]),
                ),
            )
    else:
        route = replace(
            gateway.targets[0],
            **(
                {"billing_evidence_state": "unknown"}
                if change == "billing_unknown"
                else {"free_eligible": False}
            ),
        )
        gateway.targets = gateway.broker.targets = (route,)
    assert not access.catalog(principal)[0]["available"]
    with pytest.raises((GatewayError, BrokerError)):
        gateway.run(payload)
    assert calls == []


def test_new_project_job_default_is_three_and_repeated_claim_keeps_identity(tmp_path):
    _ui, access, principal, calls = scoped(tmp_path)
    raw = task("new-default")
    internal, payload = access.claim(principal, "jobs", raw)
    assert payload["max_attempts"] == 3
    assert access.claim(principal, "jobs", raw) == (internal, payload)
    queue = DurableQueue(access.gateway, b"fixture-project-budget-key".ljust(32, b"!"))
    assert queue.submit(payload)["max_attempts"] == 3
    assert calls == []


def test_legacy_thirty_two_fingerprint_and_queue_budget_survive_omitted_reclaim(tmp_path):
    _ui, access, principal, calls = scoped(tmp_path)
    raw = task("old-default")
    internal, payload = access.claim(principal, "jobs", {**raw, "max_attempts": 32})
    queue = DurableQueue(access.gateway, b"fixture-project-budget-key".ljust(32, b"!"))
    queue.submit(payload)
    with sqlite3.connect(access.gateway.db) as con:
        con.execute("UPDATE queue_jobs SET attempt_count=7 WHERE request_key=?", (internal,))

    def saved():
        with sqlite3.connect(access.gateway.db) as con:
            return (
                con.execute(
                    "SELECT internal_key,payload_hmac FROM gateway_project_objects WHERE external_key='old-default'"
                ).fetchone(),
                con.execute(
                    "SELECT payload_hmac,max_attempts,attempt_count FROM queue_jobs WHERE request_key=?",
                    (internal,),
                ).fetchone(),
            )

    before = saved()
    again, restored = access.claim(principal, "jobs", raw)
    assert again == internal and restored == payload
    assert queue.submit(restored)["max_attempts"] == 32
    assert saved() == before
    for changed in ({**raw, "max_attempts": 3}, {**raw, "input": "different content"}):
        with pytest.raises(GatewayError, match="project key already bound"):
            access.claim(principal, "jobs", changed)
    assert saved() == before and calls == []


def test_explicit_local_mode_without_billing_evidence_is_rejected_consistently(tmp_path):
    docs = documents()
    docs["gateway.json"]["targets"][0].pop("billing_evidence_state")
    ui, gateway = board(tmp_path, docs)
    access = ProjectAccess(gateway, AccessPolicyFile(policy_file(tmp_path)))
    principal = access.policy.authenticate(TOKENS["a"])
    assert not access.catalog(principal)[0]["available"]
    assert ui.advice(task(provider="nvidia"))["decision"] == "ask_user"
