"""Explicit local budgets never masquerade as provider remaining evidence."""

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, task

from quota_broker.config import SecretInventory, load_gateway_config
from quota_broker.dashboard import Dashboard, GatewaySource
from quota_broker.gateway import Gateway
from quota_broker.registry import Registry

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from build_shared_runtime_config import validate_runtime_configuration

spec = importlib.util.spec_from_file_location(
    "local_budget_maintenance", Path(__file__).parents[1] / "deploy/hp_shared_maintenance.py"
)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def documents(provider="nvidia"):
    cf = provider == "cloudflare"
    name = "CLOUDFLARE_API_TOKEN" if cf else "NVIDIA_API_KEY"
    row = {
        "id": "fixture-free-budget",
        "provider": provider,
        "model": "@cf/meta/llama-3.2-1b-instruct" if cf else "google/gemma-4-31b-it",
        "account_id": "fixture-account",
        "enabled": True,
        "free_eligible": True,
        "billing_enabled": False,
        "billing_evidence_state": "disabled",
        "verified_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "source": "fixture verified free account; not a remaining balance",
        "secret_ref": name,
        "concurrency_limit": 1,
        "max_output_tokens": 64,
        "shared_concurrency_scope": provider + ":fixture",
        "shared_concurrency_limit": 1,
        "required_capacity": [],
        "provider_quota_facts": [],
        "capacity": {
            "kind": "unknown",
            "refresh_seconds": None,
            "as_of": None,
            "source": None,
            "scope": None,
            "expires_at": None,
        },
        "local_safety_caps": [
            {
                "bucket": provider + ":rpm",
                "metric": "requests",
                "window": "rolling_minute",
                "limit": 5,
            },
            {
                "bucket": provider + ":rpd",
                "metric": "requests",
                "window": "day",
                "limit": 100,
                "timezone": "UTC",
            },
        ],
    }
    names = [name]
    if cf:
        names.append("CLOUDFLARE_ACCOUNT_ID")
        row["account_id_ref"] = "CLOUDFLARE_ACCOUNT_ID"
        row["local_safety_caps"].append(
            {
                "bucket": "cloudflare:neurons",
                "metric": "neurons",
                "window": "day",
                "limit": 1000,
                "timezone": "UTC",
            }
        )
        row["neuron_estimate"] = {
            "amount": 100,
            "max_input_tokens": 4096,
            "max_output_tokens": 64,
            "source": "trusted_operator",
            "verified_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        }
    return {
        "gateway.json": {"targets": [row]},
        "registry.json": {"schema_version": 1, "providers": [], "models": []},
        "observations.json": [],
        "access.json": {"version": 1, "principals": []},
        "secret-inventory.json": {
            "names": names,
            "project": "api-quota-broker",
            "config": "dev",
            "verified_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
    }


def board(tmp_path, docs):
    p = tmp_path / "gateway.json"
    p.write_text(json.dumps(docs["gateway.json"]))
    registry = Registry.from_manifest(docs["registry.json"])
    targets = load_gateway_config(p, registry=registry)

    def forbidden(*args, **kwargs):
        raise AssertionError("advice must not resolve credentials or dispatch")

    gateway = Gateway(
        tmp_path / "ledger.sqlite",
        targets,
        HMAC_KEY,
        forbidden,
        forbidden,
        registry=registry,
        clock=lambda: NOW,
        secret_inventory=SecretInventory(
            frozenset(docs["secret-inventory.json"]["names"]),
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=20),
            "fixture",
            "fixture",
        ),
    )
    return Dashboard(GatewaySource(gateway), clock=lambda: NOW), gateway


@pytest.mark.parametrize("provider", ["nvidia", "cloudflare"])
def test_verified_free_local_budget_can_be_advised_without_provider_remaining(tmp_path, provider):
    ui, gateway = board(tmp_path, documents(provider))
    raw = task(provider=provider)
    assert gateway.explain(raw)["selected_target_id"] == "fixture-free-budget"
    result = ui.advice(raw)
    assert result["decision"] == "recommend"
    assert (
        result["selected_target"]["capacity_basis"]
        == "verified_free_eligibility_with_local_safety_caps"
    )
    assert result["reasons"] == ["verified_free_eligibility_and_local_safety_caps"]
    assert result["selected_target"]["remaining"] == []
    assert result["execution_allowed"] is False


@pytest.mark.parametrize(
    "change", ["free_unknown", "paid", "billing_unknown", "expired", "no_estimate", "cap_full"]
)
def test_local_budget_advice_preserves_existing_safety_gates(tmp_path, change):
    docs = documents("cloudflare")
    row = docs["gateway.json"]["targets"][0]
    if change == "free_unknown":
        row["free_eligible"] = False
    elif change == "paid":
        row["billing_enabled"] = True
    elif change == "billing_unknown":
        row["billing_evidence_state"] = "unknown"
    elif change == "expired":
        row["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "no_estimate":
        row.pop("neuron_estimate")
    elif change == "cap_full":
        row["local_safety_caps"][-1]["limit"] = 1
    ui, _gateway = board(tmp_path, docs)
    assert ui.advice(task(provider="cloudflare"))["decision"] == "ask_user"


@pytest.mark.parametrize("provider", ["nvidia", "cloudflare"])
def test_both_configuration_validators_allow_explicit_verified_local_budget(provider):
    docs = documents(provider)
    assert validate_runtime_configuration(docs, now=NOW)["enabled_targets"] == 1
    maintenance.validate_configuration(
        {"config/" + k: json.dumps(v).encode() for k, v in docs.items()}, now=NOW
    )


@pytest.mark.parametrize(
    "change",
    [
        "omitted_mode",
        "free_unknown",
        "paid",
        "billing_unknown",
        "expired",
        "inventory_expired",
        "caps_missing",
        "no_estimate",
        "expired_estimate",
        "nonutc",
        "unknown_estimate_source",
    ],
)
def test_local_budget_configuration_cannot_bypass_eligibility_or_budget_gates(change):
    docs = documents("cloudflare")
    row = docs["gateway.json"]["targets"][0]
    if change == "omitted_mode":
        row.pop("required_capacity")
    elif change == "free_unknown":
        row["free_eligible"] = False
    elif change == "paid":
        row["billing_enabled"] = True
    elif change == "billing_unknown":
        row["billing_evidence_state"] = "unknown"
    elif change == "expired":
        row["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "inventory_expired":
        docs["secret-inventory.json"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "caps_missing":
        row["local_safety_caps"] = []
    elif change == "no_estimate":
        row.pop("neuron_estimate")
    elif change == "expired_estimate":
        row["neuron_estimate"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "unknown_estimate_source":
        row["neuron_estimate"]["source"] = "unverified"
    else:
        row["local_safety_caps"][-1]["timezone"] = "Asia/Taipei"
    with pytest.raises(ValueError):
        validate_runtime_configuration(deepcopy(docs), now=NOW)
    with pytest.raises(ValueError):
        maintenance.validate_configuration(
            {"config/" + k: json.dumps(v).encode() for k, v in docs.items()}, now=NOW
        )


def test_known_cloudflare_reset_with_unknown_remaining_preserves_local_budget_advice(tmp_path):
    ui, gateway = board(tmp_path, documents("cloudflare"))
    gateway.observe_quota(
        "fixture-free-budget",
        {
            "metric": "neurons",
            "window": "day",
            "remaining": None,
            "limit": 10000,
            "as_of": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
            "reset_at": (NOW + timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat(),
            "source": "trusted_operator",
            "provenance": "official",
            "confidence": "administrator_verified",
        },
    )
    assert (
        gateway.explain(task(provider="cloudflare"))["selected_target_id"] == "fixture-free-budget"
    )
    result = ui.advice(task(provider="cloudflare"))
    assert result["decision"] == "recommend"
    selected = result["selected_target"]
    assert selected["remaining"][0]["remaining"] is None
    assert selected["remaining"][0]["effective_remaining"] is None
    assert selected["remaining"][0]["reset_at"] is not None
    assert selected["capacity_basis"] == "verified_free_eligibility_with_local_safety_caps"


def test_explicit_authoritative_capacity_still_requires_nonnull_current_remaining(tmp_path):
    docs = documents("cloudflare")
    docs["gateway.json"]["targets"][0]["required_capacity"] = [
        {"metric": "neurons", "window": "day"}
    ]
    ui, _gateway = board(tmp_path, docs)
    assert ui.advice(task(provider="cloudflare"))["decision"] == "ask_user"


def test_missing_credential_inventory_still_blocks_local_budget_advice(tmp_path):
    ui, gateway = board(tmp_path, documents())
    gateway.secret_inventory = None
    assert ui.advice(task(provider="nvidia"))["decision"] == "ask_user"


def test_local_calendar_cap_does_not_require_provider_window_alignment(tmp_path):
    docs = documents()
    docs["gateway.json"]["targets"][0]["local_safety_caps"][1]["timezone"] = "Asia/Taipei"
    ui, _gateway = board(tmp_path, docs)
    assert ui.advice(task(provider="nvidia"))["decision"] == "recommend"
