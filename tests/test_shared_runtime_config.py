"""Real config/parser admission against fabricated account evidence only."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_shared_runtime_config import build

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway
from quota_broker.registry import Registry

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def evidence():
    owner = {
        "received_at": (NOW - timedelta(hours=1)).isoformat(),
        "provider": "openai",
        "source_type": "account_owner_pasted_billing_page",
        "billing_state": "not_started_no_payment_method",
        "plan_label": "Free trial",
        "recurrence": "unknown",
        "balance_usd": "200.00",
        "expires": {"precision": "date", "value": "2026-12-03"},
        "source": "https://platform.openai.com/settings/organization/billing/overview",
    }
    router = {
        "checked_at": NOW.isoformat(),
        "is_free_tier": True,
        "free_model_daily_requests": {"used": 7, "limit": 50, "remaining": 43},
        "model": {
            "id": "liquid/lfm-2.5-2.6b:free",
            "context_length": 65536,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
    }
    return owner, router


def test_generated_targets_share_original_balances_and_pass_real_admission(tmp_path):
    owner, router = evidence()
    result = build(
        {"schema_version": 1, "providers": [], "models": []}, {"targets": []}, owner, router, NOW
    )
    registry = Registry.from_manifest(result["registry"])
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps(result["gateway"]))
    targets = load_gateway_config(config, registry=registry)

    def no_secret(_):
        raise AssertionError("read-only admission cannot fetch a secret")

    gateway = Gateway(
        tmp_path / "db", targets, b"d" * 32, no_secret, registry=registry, clock=lambda: NOW
    )
    for row in result["observations"]:
        gateway.observe_quota(row["target_id"], row["observation"])
    candidates = gateway.explain(
        {
            "request_key": "plan",
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "max_output_tokens": 64,
        }
    )["candidates"]
    assert len(candidates) == 2 and all(c["eligible"] for c in candidates)
    assert result["observations"][0]["observation"]["remaining"] == 199_999_000
    assert result["observations"][1]["observation"]["remaining"] == 43
    assert result["derivation"]["provider_current_balance_usd"] is None
    assert owner["balance_usd"] == "200.00"
    assert targets[0].capacity.credit_expires_at == datetime(2026, 12, 2, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "failure", ["old_owner", "old_counter", "paid", "missing_counter", "price", "wrong_model"]
)
def test_builder_stops_on_unknown_or_changed_evidence(failure):
    owner, router = evidence()
    if failure == "old_owner":
        owner["received_at"] = (NOW - timedelta(days=2)).isoformat()
    elif failure == "old_counter":
        router["checked_at"] = (NOW - timedelta(minutes=16)).isoformat()
    elif failure == "paid":
        router["is_free_tier"] = False
    elif failure == "missing_counter":
        router["free_model_daily_requests"] = None
    elif failure == "price":
        router["model"]["pricing"]["prompt"] = "0.01"
    elif failure == "wrong_model":
        router["model"]["id"] = "paid-model"
    with pytest.raises(ValueError):
        build(
            {"schema_version": 1, "providers": [], "models": []},
            {"targets": []},
            owner,
            router,
            NOW,
        )


def test_builder_marks_placeholder_billing_unknown_and_replaces_same_openrouter_model():
    owner, router = evidence()
    base = {
        "targets": [
            {
                "id": "old-router",
                "provider": "openrouter",
                "model": router["model"]["id"],
                "enabled": False,
                "billing_enabled": True,
            },
            {
                "id": "other-router",
                "provider": "openrouter",
                "model": "other-model:free",
                "enabled": False,
                "billing_enabled": True,
            },
            {
                "id": "other-provider",
                "provider": "google",
                "model": "fixture-google",
                "enabled": False,
                "billing_enabled": True,
            },
        ]
    }
    before = json.dumps(base, sort_keys=True)
    result = build({"schema_version": 1, "providers": [], "models": []}, base, owner, router, NOW)
    targets = result["gateway"]["targets"]
    assert not any(row["id"] == "old-router" for row in targets)
    assert (
        len(
            [
                row
                for row in targets
                if row["provider"] == "openrouter" and row["model"] == router["model"]["id"]
            ]
        )
        == 1
    )
    for row in targets:
        if row["enabled"]:
            assert row["billing_evidence_state"] == "disabled" and row["billing_enabled"] is False
        else:
            assert row["billing_evidence_state"] == "unknown" and row["billing_enabled"] is True
    assert json.dumps(base, sort_keys=True) == before


def runtime_documents(result):
    return {
        "registry.json": result["registry"],
        "gateway.json": result["gateway"],
        "observations.json": result["observations"],
        "secret-inventory.json": {
            "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
            "verified_at": (NOW - timedelta(hours=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=30) - timedelta(minutes=38)).isoformat(),
            "project": "api-quota-broker",
            "config": "dev",
        },
        "access.json": {"version": 1, "principals": []},
    }


def test_refresh_retains_original_eligibility_deadline_instead_of_renewing_it():
    owner, router = evidence()
    old_deadline = NOW + timedelta(days=30) - timedelta(minutes=38)
    result = build(
        {"schema_version": 1, "providers": [], "models": []},
        {"targets": []},
        owner,
        router,
        NOW,
        existing_eligibility_deadline=old_deadline,
    )
    route = next(row for row in result["gateway"]["targets"] if row["provider"] == "openrouter")
    assert route["expires_at"] == old_deadline.isoformat()
    assert route["capacity"]["expires_at"] == old_deadline.isoformat()
    from build_shared_runtime_config import validate_runtime_configuration

    assert (
        validate_runtime_configuration(runtime_documents(result), now=NOW)["enabled_targets"] == 2
    )


def test_runtime_preflight_rejects_cross_file_deadline_mismatch_before_remote_io():
    from build_shared_runtime_config import validate_runtime_configuration

    owner, router = evidence()
    result = build(
        {"schema_version": 1, "providers": [], "models": []}, {"targets": []}, owner, router, NOW
    )
    with pytest.raises(ValueError, match="eligibility_exceeds_inventory_validity"):
        validate_runtime_configuration(runtime_documents(result), now=NOW)


@pytest.mark.parametrize(
    "problem",
    [
        "registry",
        "access",
        "missing_capacity",
        "wrong_secret",
        "inventory_scope",
        "observation_target",
    ],
)
def test_runtime_preflight_rejects_other_invalid_bindings(problem):
    from build_shared_runtime_config import validate_runtime_configuration

    owner, router = evidence()
    result = build(
        {"schema_version": 1, "providers": [], "models": []},
        {"targets": []},
        owner,
        router,
        NOW,
        existing_eligibility_deadline=NOW + timedelta(days=29),
    )
    documents = runtime_documents(result)
    if problem == "registry":
        documents["registry.json"] = {}
    elif problem == "access":
        documents["access.json"] = {"version": 1, "principals": [{"scopes": ["execute"]}]}
    elif problem == "missing_capacity":
        documents["gateway.json"]["targets"][0]["required_capacity"] = []
    elif problem == "wrong_secret":
        documents["gateway.json"]["targets"][0]["secret_ref"] = "OPENROUTER_API_KEY"
    elif problem == "inventory_scope":
        documents["secret-inventory.json"]["config"] = "wrong"
    else:
        documents["observations.json"][0]["target_id"] = "missing-target"
    with pytest.raises(ValueError):
        validate_runtime_configuration(documents, now=NOW)


@pytest.mark.parametrize("deadline", [NOW, NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_eligibility_update_cannot_reopen_expired_or_timezone_unknown_review(deadline):
    owner, router = evidence()
    with pytest.raises(ValueError):
        build(
            {"schema_version": 1, "providers": [], "models": []},
            {"targets": []},
            owner,
            router,
            NOW,
            existing_eligibility_deadline=deadline,
        )


def test_existing_deadline_cannot_lengthen_new_review_envelope():
    owner, router = evidence()
    result = build(
        {"schema_version": 1, "providers": [], "models": []},
        {"targets": []},
        owner,
        router,
        NOW,
        existing_eligibility_deadline=(NOW + timedelta(days=60)).isoformat(),
    )
    route = next(row for row in result["gateway"]["targets"] if row["provider"] == "openrouter")
    assert route["expires_at"] == (NOW + timedelta(days=30)).isoformat()
