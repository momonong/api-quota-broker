"""Launcher validates local-budget policy shape; route freshness belongs to dispatch."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_gateway import NOW, task
from test_hp_shared_runtime import module
from test_local_capacity_advice import board, documents

from quota_broker.config import cloudflare_neuron_upper_bound


def candidate(tmp_path, provider):
    docs = documents(provider)
    row = docs["gateway.json"]["targets"][0]
    if provider == "cloudflare":
        row["model"] = "@cf/google/gemma-4-26b-a4b-it"
        row["neuron_estimate"].update(
            source="cloudflare_gemma_4_26b_formula",
            amount=cloudflare_neuron_upper_bound(4096, 64, model=row["model"]),
        )
    else:
        row["model"] = "z-ai/glm-5.3-flash"
    ui, gateway = board(tmp_path, docs)
    return ui, gateway, gateway.targets[0]


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("nvidia", {"NVIDIA_API_KEY"}),
        ("cloudflare", {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"}),
    ],
)
def test_actual_json_candidate_local_mode_passes_fixed_launcher_without_remaining(
    tmp_path, provider, expected
):
    ui, gateway, target = candidate(tmp_path, provider)
    assert target.required_capacity_declared and not target.required_capacity
    assert ui.advice(task(provider=provider))["decision"] == "recommend"
    assert gateway.explain(task(provider=provider))["selected_target_id"] == target.id
    assert module().enabled_provider_names([target]) == expected


@pytest.mark.parametrize("provider", ["nvidia", "cloudflare"])
def test_expired_route_does_not_kill_service_startup_but_cannot_dispatch(tmp_path, provider):
    ui, gateway, target = candidate(tmp_path, provider)
    target = replace(
        target, verified_at=NOW - timedelta(days=2), expires_at=NOW - timedelta(days=1)
    )
    if target.neuron_estimate:
        target = replace(
            target,
            neuron_estimate=replace(
                target.neuron_estimate,
                verified_at=NOW - timedelta(days=2),
                expires_at=NOW - timedelta(days=1),
            ),
        )
    gateway.targets = gateway.broker.targets = (target,)
    assert module().enabled_provider_names([target])
    assert not target.available(NOW)
    assert gateway.explain(task(provider=provider))["selected_target_id"] is None
    assert ui.advice(task(provider=provider))["decision"] == "ask_user"


@pytest.mark.parametrize(
    "change",
    [
        "undeclared",
        "free",
        "paid",
        "billing_unknown",
        "legacy_caps",
        "missing_request_day",
        "wrong_key",
    ],
)
def test_launcher_rejects_unverified_or_implicit_local_policy(tmp_path, change):
    _ui, _gateway, target = candidate(tmp_path, "nvidia")
    changes = {
        "undeclared": {"required_capacity_declared": False},
        "free": {"free_eligible": False},
        "paid": {"billing_enabled": True},
        "billing_unknown": {"billing_evidence_state": "unknown"},
        "legacy_caps": {"quota_basis": "legacy_v1"},
        "missing_request_day": {"quotas": tuple(q for q in target.quotas if q.window != "day")},
        "wrong_key": {"secret_ref": "GROQ_API_KEY"},
    }
    with pytest.raises(ValueError):
        module().enabled_provider_names([replace(target, **changes[change])])


@pytest.mark.parametrize(
    "change", ["missing", "wrong_model", "underbound", "nonutc", "over_cap", "invalid_interval"]
)
def test_cloudflare_launcher_keeps_exact_model_bound_and_utc_pool_contract(tmp_path, change):
    _ui, _gateway, target = candidate(tmp_path, "cloudflare")
    if change == "missing":
        target = replace(target, neuron_estimate=None)
    elif change == "wrong_model":
        target = replace(target, model="@cf/meta/llama-3.2-1b-instruct")
    elif change == "underbound":
        target = replace(target, neuron_estimate=replace(target.neuron_estimate, amount=1))
    elif change == "invalid_interval":
        target = replace(
            target,
            neuron_estimate=replace(
                target.neuron_estimate, expires_at=target.neuron_estimate.verified_at
            ),
        )
    else:
        target = replace(
            target,
            quotas=tuple(
                replace(
                    q, **({"timezone": "Asia/Taipei"} if change == "nonutc" else {"limit": 10001})
                )
                if q.metric == "neurons"
                else q
                for q in target.quotas
            ),
        )
    with pytest.raises(ValueError):
        module().enabled_provider_names([target])
