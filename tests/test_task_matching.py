"""Capability matching never turns catalog presence into free execution authority."""

import copy

import pytest
from test_dashboard import dashboard
from test_gateway import task

from quota_broker.task_matching import match_models, validate_match


def query(**changes):
    return {
        "mode": "match",
        "task": "text_generation",
        "inputs": ["text"],
        "outputs": ["text"],
        **changes,
    }


def model(name="new-model", variants=None, status="listed"):
    return {
        "id": name,
        "status": status,
        "source": "https://example.com/models",
        "variants": variants
        or [{"task": "text_generation", "inputs": ["text"], "outputs": ["text"]}],
    }


def snapshot(models):
    return {
        "source": {"kind": "reviewed"},
        "targets": [],
        "models": [],
        "provider_inventory": {
            "providers": [{"id": "new-provider", "catalogs": [{"models": models}]}]
        },
    }


def test_new_provider_matches_without_route_or_quota_inheritance():
    result = match_models(query(), snapshot([model()]), None)
    assert result["decision"] == "ask_user"
    assert result["selected_target"] is None
    assert result["candidates"][0]["provider"] == "new-provider"
    assert "route_not_configured" in result["candidates"][0]["reasons"]
    assert result["candidates"][0]["remaining"] is None
    assert result["execution_allowed"] is result["paid_allowed"] is False


def test_task_only_query_recommends_capability_without_granting_free_execution():
    result = match_models(query(), snapshot([model()]), None)
    assert result["recommended_model"]["model"] == "new-model"
    assert result["recommended_model"]["capability_verified"] is True
    assert result["shortlist"] == [result["recommended_model"]]
    assert result["selected_target"] is None and result["decision"] == "ask_user"
    assert result["recommended_model"]["safe"] is False
    assert result["recommended_model"]["remaining"] is None
    assert result["recommended_model"]["documentation_url"] == "https://example.com/models"


def test_current_exact_model_free_plan_beats_catalog_but_not_expired_evidence():
    data = snapshot([model("a-catalog"), model("z-free"), model("b-expired")])
    data["source"]["fetched_at"] = "2026-11-06T12:00:00+00:00"
    data["targets"] = [
        {
            "provider": "new-provider",
            "model": name,
            "target_id": name,
            "free_eligible": True,
            "free_verified_at": "2026-11-05T12:00:00+00:00",
            "free_evidence_expires_at": due,
            "reasons": [],
            "features": [],
        }
        for name, due in [
            ("z-free", "2026-11-07T12:00:00+00:00"),
            ("b-expired", "2026-11-06T11:59:59+00:00"),
        ]
    ]
    result = match_models(query(), data, None)
    assert result["recommended_model"]["model"] == "z-free"
    by_model = {r["model"]: r for r in result["candidates"]}
    assert by_model["z-free"]["free_plan_verified"] is True
    assert by_model["b-expired"]["free_plan_verified"] is False
    assert by_model["a-catalog"]["free_plan_verified"] is False
    assert all(not r["safe"] and r["remaining"] is None for r in result["candidates"])


def test_shortlist_diversifies_providers_without_losing_full_model_list():
    data = snapshot([model(str(i)) for i in range(30)])
    data["provider_inventory"]["providers"].append(
        {"id": "other", "catalogs": [{"models": [model("choice")]}]}
    )
    result = match_models(query(), data, None)
    assert {r["provider"] for r in result["shortlist"][:2]} == {"new-provider", "other"}
    assert len(result["candidates"]) == 31


def test_shortlist_never_replaces_better_free_evidence_with_provider_variety():
    data = snapshot([model("free-one"), model("free-two")])
    data["source"]["fetched_at"] = "2026-11-06T12:00:00+00:00"
    data["targets"] = [
        {
            "provider": "new-provider",
            "model": name,
            "target_id": name,
            "free_eligible": True,
            "free_verified_at": "2026-11-05T00:00:00+00:00",
            "free_evidence_expires_at": "2026-11-10T00:00:00+00:00",
            "reasons": [],
        }
        for name in ("free-one", "free-two")
    ]
    data["provider_inventory"]["providers"].extend(
        {"id": "other-" + str(i), "catalogs": [{"models": [model("unknown")]}]} for i in range(5)
    )
    result = match_models(query(), data, None)
    assert [r["model"] for r in result["shortlist"][:2]] == ["free-one", "free-two"]


def test_unverified_features_never_become_capability_recommendation():
    result = match_models(query(features=["tools"]), snapshot([model()]), None)
    assert result["recommended_model"] is None and result["shortlist"] == []
    assert result["candidates"][0]["capability_verified"] is False


def test_snapshot_disabled_is_not_reported_as_live_broker_disabled():
    data = snapshot([model()])
    data["targets"] = [
        {
            "provider": "new-provider",
            "model": "new-model",
            "target_id": "t",
            "reasons": ["disabled", "gateway_blocked"],
            "enabled": False,
        }
    ]
    row = match_models(query(), data, None)["recommended_model"]
    assert row["execution_status"] == "reviewed_snapshot"
    assert row["safe"] is False


def test_catalog_does_not_erase_complete_registry_json_variant():
    data = snapshot([model()])
    data["models"] = [
        {
            "provider": "new-provider",
            "model": "new-model",
            "variants": [
                {
                    "task": "text_generation",
                    "inputs": ["text"],
                    "outputs": ["text"],
                    "features": ["json_output"],
                }
            ],
        }
    ]
    result = match_models(query(outputs=["json"]), data, None)
    assert result["recommended_model"]["model"] == "new-model"
    assert result["selected_target"] is None
    # A different task's complete variant cannot grant this task the same feature.
    data["models"][0]["variants"][0]["task"] = "ocr"
    assert match_models(query(outputs=["json"]), data, None)["recommended_model"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"credit_expires_at": "2026-11-06T10:00:00+00:00"},
        {"billing_state": "enabled"},
        {"billing_enabled": True},
    ],
)
def test_known_expired_credit_or_billing_never_gets_free_priority(change):
    data = snapshot([model()])
    data["source"]["fetched_at"] = "2026-11-06T12:00:00+00:00"
    data["targets"] = [
        {
            "provider": "new-provider",
            "model": "new-model",
            "target_id": "t",
            "free_eligible": True,
            "free_verified_at": "2026-11-05T00:00:00+00:00",
            "free_evidence_expires_at": "2026-11-10T00:00:00+00:00",
            "reasons": [],
            **change,
        }
    ]
    assert match_models(query(), data, None)["recommended_model"]["free_plan_verified"] is False


def test_model_without_individual_doc_uses_its_catalog_source():
    m = model()
    m.pop("source")
    data = snapshot([m])
    data["provider_inventory"]["providers"][0]["catalogs"][0]["source"] = "https://example.com/api"
    assert (
        match_models(query(), data, None)["recommended_model"]["documentation_url"]
        == "https://example.com/api"
    )


@pytest.mark.parametrize(
    "reason", ["context_limit", "output_limit", "adapter_admission", "features_mismatch"]
)
def test_known_task_incompatibility_loses_to_unknown_free_capacity(reason):
    data = snapshot([model("a-too-small"), model("b-fits")])
    data["targets"] = [
        {"provider": "new-provider", "model": name, "target_id": name}
        for name in ("a-too-small", "b-fits")
    ]
    advice = {
        "source": data["source"],
        "candidates": [
            {
                "provider": "new-provider",
                "model": name,
                "target_id": name,
                "safe": False,
                "reasons": [why],
            }
            for name, why in [("a-too-small", reason), ("b-fits", "quota_evidence_missing")]
        ],
    }
    result = match_models(query(), data, advice)
    assert result["recommended_model"]["model"] == "b-fits"
    assert not next(r for r in result["candidates"] if r["model"] == "a-too-small")[
        "capability_verified"
    ]
    assert result["selected_target"] is None


def test_same_variant_must_satisfy_all_requirements():
    split = model(
        variants=[
            {"task": "text_generation", "inputs": ["image"], "outputs": ["text"]},
            {"task": "image_generation", "inputs": ["text"], "outputs": ["image"]},
        ]
    )
    result = match_models(query(inputs=["image"], outputs=["image"]), snapshot([split]), None)
    assert result["candidates"] == []


def test_native_json_output_does_not_require_text_format_feature():
    native = model(variants=[{"task": "decisions", "inputs": ["text"], "outputs": ["json"]}])
    result = match_models(query(task="decisions", outputs=["json"]), snapshot([native]), None)
    assert len(result["candidates"]) == 1
    assert "features_unverified" not in result["candidates"][0]["reasons"]


def test_json_and_tools_must_be_supported_by_the_same_variant():
    split = model(
        variants=[
            {"task": "text_generation", "inputs": ["text"], "outputs": ["json"]},
            {
                "task": "text_generation",
                "inputs": ["text"],
                "outputs": ["text"],
                "features": ["tools"],
            },
        ]
    )
    result = match_models(query(outputs=["json"], features=["tools"]), snapshot([split]), None)
    assert result["recommended_model"] is None
    assert "features_unverified" in result["candidates"][0]["reasons"]
    split["variants"][1]["features"].append("json_output")
    assert (
        match_models(query(outputs=["json"], features=["tools"]), snapshot([split]), None)[
            "recommended_model"
        ]
        is not None
    )


def test_retired_catalog_cannot_be_resurrected_by_registered_metadata():
    data = snapshot([model(status="retired")])
    data["models"] = [
        {
            "provider": "new-provider",
            "model": "new-model",
            "capabilities": ["text_generation"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        }
    ]
    assert match_models(query(), data, None)["candidates"] == []


def test_legacy_registry_union_does_not_prove_cross_family_variant():
    data = snapshot([])
    data["models"] = [
        {
            "provider": "new-provider",
            "model": "hybrid",
            "capabilities": ["tts", "image_generation"],
            "input_modalities": ["text"],
            "output_modalities": ["audio", "image"],
        }
    ]
    assert match_models(query(task="tts", outputs=["image"]), data, None)["candidates"] == []


def test_current_safe_task_can_recommend_and_unknown_stays_blocked(tmp_path):
    board, _ = dashboard(tmp_path)
    result = board.advice(query(route_task=task()))
    assert result["decision"] == "recommend"
    assert result["selected_target"]["target_id"] == "demo-ready"
    result = board.advice(query())
    assert result["decision"] == "ask_user"
    assert all(not row["safe"] for row in result["candidates"])
    assert any("task_cost_not_checked" in row["reasons"] for row in result["candidates"])


@pytest.mark.parametrize(
    "change",
    [
        {"inputs": []},
        {"outputs": ["text", "text"]},
        {"task": True},
        {"extra": "unknown"},
        {"route_task": {"mode": "match"}},
        {"route_task": {**task(), "options": {"json_schema": {"type": "object"}}}},
        {"task": "vision", "route_task": task()},
        {"outputs": ["image"], "route_task": task()},
        {"route_task": {**task(), "options": {"response_format": "json_object"}}},
        {
            "route_task": {
                **task(),
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "hello"},
                                {"type": "image", "data": "inline"},
                            ],
                        }
                    ]
                },
            }
        },
    ],
)
def test_invalid_or_mismatched_requests_fail_closed(change):
    with pytest.raises(ValueError):
        validate_match(query(**change))


def test_verified_deadline_then_headroom_and_stable_identity_order(tmp_path):
    board, _ = dashboard(tmp_path)
    data = board.snapshot()
    route = board.advice(task())
    base = copy.deepcopy(route["selected_target"])
    candidates = []
    for identity, seconds, headroom in [
        ("late", 80, 0.9),
        ("early", 30, 0.1),
        ("roomy", 80, 0.95),
        ("interval", 1, 1),
    ]:
        r = copy.deepcopy(base)
        r.update(target_id=identity, model=identity)
        r["ranking"].update(
            value_loss_seconds=seconds,
            value_loss_basis=("renewal_interval" if identity == "interval" else "credit_expiry"),
            observed_headroom=headroom,
        )
        candidates.append(r)
    route["candidates"] = candidates
    data["targets"] = [
        {**data["targets"][0], "target_id": r["target_id"], "model": r["model"]} for r in candidates
    ]
    data["models"] = data["targets"]
    result = match_models(query(), data, route)
    assert [r["model"] for r in result["candidates"]] == ["early", "roomy", "late", "interval"]


def test_model_id_is_scoped_by_provider_and_does_not_copy_account_balance(tmp_path):
    board, _ = dashboard(tmp_path)
    data, route = board.snapshot(), board.advice(task())
    ready = route["selected_target"]
    data["provider_inventory"] = snapshot([model(ready["model"])])["provider_inventory"]
    result = match_models(query(), data, route)
    other = next(r for r in result["candidates"] if r["provider"] == "new-provider")
    assert not other["safe"] and other["remaining"] is None


def test_catalog_only_provider_with_actual_text_still_lists_blocked_candidates(tmp_path):
    board, _ = dashboard(tmp_path)
    board._provider_inventory = lambda: snapshot([model()])["provider_inventory"]
    result = board.advice(query(provider="new-provider", route_task=task(provider="new-provider")))
    assert result["decision"] == "ask_user"
    assert "route_not_configured" in result["candidates"][0]["reasons"]


def test_family_adapter_features_do_not_leak_between_ocr_and_code(tmp_path):
    from dataclasses import replace

    from test_family_gateway import catalog
    from test_gateway import HMAC_KEY, NOW, target

    from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
    from quota_broker.gateway import Gateway
    from quota_broker.registry import Registry

    features = set(PACKAGED_FAMILY_ADAPTERS["mistral_fim"].supported_features)
    features.update(PACKAGED_FAMILY_ADAPTERS["mistral_ocr"].supported_features)
    registry = catalog(profile="mistral_fim", capability="code_completion")
    spec = registry.resolve("fixture-embed", "mistral")
    spec = replace(
        spec,
        capabilities=("code_completion", "ocr"),
        features=tuple(sorted(features)),
        family_adapters=(("ocr", "mistral_ocr"),),
    )
    gateway = Gateway(
        tmp_path / "fixture.sqlite",
        (target("fixture", "mistral", "fixture-embed"),),
        HMAC_KEY,
        lambda _: None,
        clock=lambda: NOW,
        registry=Registry({("mistral", "fixture-embed"): spec}),
    )
    metadata = gateway._model_metadata("mistral", "fixture-embed")
    variants = {v["task"]: v for v in metadata["variants"]}
    assert "document" in variants["ocr"]["inputs"]
    assert variants["code_completion"]["inputs"] == ["text"]
    assert "document_input" not in variants["code_completion"]["features"]


def test_match_uses_same_advice_evidence_for_balance_and_source(tmp_path):
    board, _ = dashboard(tmp_path)
    data, route = board.snapshot(), board.advice(task())
    route["source"] = {"kind": "demo", "as_of": "same-advice-proof"}
    for row in data["targets"]:
        row["observations"] = [{"remaining": 999999}]
    result = match_models(query(), data, route)
    selected = next(row for row in result["candidates"] if row["safe"])
    original = next(row for row in route["candidates"] if row["target_id"] == selected["target_id"])
    assert selected["remaining"] == original["remaining"]
    assert result["source"] == route["source"]


def test_account_grant_verified_and_quota_summary_ranking(tmp_path):
    from test_dashboard_accounts import account_document, write_accounts
    from test_dashboard_reviewed import load_board

    from quota_broker.dashboard_accounts import CreditAccounts

    board, *_ = load_board(tmp_path)
    doc = account_document()
    doc["accounts"][0]["balance"] = "200.00"
    doc["accounts"][0]["grants"][0]["amount"] = "200.00"
    doc["accounts"][0]["grants"][0]["remaining"] = "200.00"
    doc["accounts"][0]["grants"][0]["expires"] = {"value": "2026-12-03", "precision": "date"}
    doc["accounts"][0]["model_limits"] = [
        {
            "model": "gpt-5.4-mini",
            "requests_per_minute": 500,
            "tokens_per_minute": 500000,
            "images_per_minute": None,
            "batch_queue_tokens_per_day": 5000000,
        }
    ]
    board.accounts = CreditAccounts(write_accounts(tmp_path, doc))
    res = board.advice(
        {"mode": "match", "task": "text_generation", "inputs": ["text"], "outputs": ["text"]}
    )
    assert res["execution_allowed"] is False and res["paid_allowed"] is False
    openai_cand = next(
        c for c in res["candidates"] if c["provider"] == "openai" and c["model"] == "gpt-5.4-mini"
    )
    assert openai_cand["access_stage"] == "account_grant_verified"
    assert openai_cand["free_plan_verified"] is True
    assert openai_cand["free_plan_basis"] == "credit_grant"
    assert openai_cand["safe"] is False
    assert "200.00 USD" in openai_cand["quota_summary"]["headline"]
    assert "2026-12-03" in openai_cand["quota_summary"]["reset_or_expiry"]
    catalog_only = next(c for c in res["candidates"] if c["access_stage"] == "catalog_only")
    assert res["candidates"].index(openai_cand) < res["candidates"].index(catalog_only)


def test_quota_summary_uses_runtime_credit_holds_before_account_snapshot():
    from quota_broker.task_matching import _build_quota_summary

    target = {
        "provider_limits": [],
        "credit_expires_at": "2026-12-02T10:00:00Z",
        "observations": [
            {
                "metric": "credits_usd_micros",
                "window": "budget",
                "current": True,
                "remaining": 200000000,
                "effective_remaining": 199000000,
            }
        ],
    }
    account = {
        "account": {
            "balance": "200.00",
            "current_balance": "200.00",
            "evidence": {"current": True},
        },
        "model_limit": None,
        "grant": {"expires": {"value": "2026-12-03", "precision": "date"}},
    }
    summary = _build_quota_summary("openai", "fixture", [target], account, None)
    assert "199 USD" in summary["headline"]
    assert "200.00" not in summary["headline"]
    assert "500" not in summary["limits_text"]
    assert "Broker" in summary["headline"]


def test_unknown_quota_summary_never_substitutes_limit_or_zero_used():
    from quota_broker.task_matching import _build_quota_summary

    target = {
        "reviewed_account": {"derived_remaining": {"limit": 10000, "metric": "neurons"}},
        "observations": [],
        "provider_limits": [],
    }
    summary = _build_quota_summary("cloudflare", "fixture", [target], None, None)
    assert "剩餘未知" in summary["headline"]
    assert "剩餘 10000" not in summary["headline"]
    monthly = {
        "reviewed_account": {"display_facts": [{"kind": "monthly_allowance_usd", "value": 10}]}
    }
    summary = _build_quota_summary("mistral", "fixture", [monthly], None, None)
    assert "已用 未知" in summary["headline"]
    assert "1,300,000" not in summary["sharing_scope"]


def test_stale_positive_account_grant_is_not_verified_qualification():
    from quota_broker.task_matching import _account_grant_info

    acc = {
        "provider": "openai",
        "qualification": "free_trial",
        "balance": "200.00",
        "current_balance": None,
        "balance_state": "stale",
        "evidence": {"current": False},
        "grants": [{"state": "available", "expires": {"value": "2026-12-03", "precision": "date"}}],
    }
    assert _account_grant_info("openai", "fixture", {"openai": acc}, "2026-11-11T00:00:00Z") == (
        False,
        None,
    )
