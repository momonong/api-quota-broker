"""Pool proposal admission boundaries use the real deployed configuration parser."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.config import (
    CF_NEURON_FORMULA,
    ConfigError,
    cloudflare_neuron_upper_bound,
    load_gateway_config,
)
from quota_broker.gateway import Gateway

PATH = Path(__file__).resolve().parents[1] / "deploy/asus/provider_pool_plan.py"
SPEC = importlib.util.spec_from_file_location("asus_pool_plan", PATH)
assert SPEC and SPEC.loader
pool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pool)


def test_proposal_cannot_dispatch_even_with_secret_bindings(tmp_path):
    path = tmp_path / "disabled.json"
    path.write_text(json.dumps(pool.disabled_config()))
    targets = load_gateway_config(path)
    assert len(targets) == 7
    assert all(not t.available(datetime.now(UTC)) for t in targets)
    assert all(t.quota_basis == "local_safety_cap" for t in targets)
    assert all(t.capacity.kind == "unknown" for t in targets)
    assert all(
        f.remaining.value is None and f.limit.value is None
        for t in targets
        for f in t.provider_quota_facts
    )


def test_existing_groq_bucket_and_account_are_preserved():
    prior = json.loads((PATH.parent / "representative_e2e.disabled.json").read_text())["targets"][0]
    groq = next(t for t in pool.disabled_config()["targets"] if t["provider"] == "groq")
    for field in ("account_id", "secret_ref", "shared_concurrency_scope"):
        assert groq[field] == prior[field]
    assert {q["bucket"] for q in groq["local_safety_caps"]} == {
        q["bucket"] for q in prior["local_safety_caps"]
    }


def test_cloudflare_scope_is_resolved_in_memory_and_no_fabricated_neurons():
    cf = next(t for t in pool.disabled_config()["targets"] if t["provider"] == "cloudflare")
    assert cf["account_id_ref"] == "CLOUDFLARE_ACCOUNT_ID"
    assert "neuron_estimate" not in cf
    assert cf["capacity"]["refresh_seconds"] is None
    assert cf["provider_quota_facts"][-1]["remaining"] == pool.UNKNOWN


def test_probe_budget_ocr_capability_and_groq_once_reuse():
    plan = pool.plan()
    assert sum(p["initial_post_limit"] for p in plan["probes"]) == 6
    assert plan["old_once_claim_replay"] is False
    ocr = next(p for p in plan["probes"] if p["provider"] == "ocrspace")
    assert ocr["capability"] == "ocr"
    assert ocr["output_bound_basis"] == "legacy_ocr_sentinel_not_provider_tokens"
    assert all(not p["automatic_retry"] for p in plan["probes"])
    assert plan["new_token_creation"] == plan["authenticated_metadata_gets"] == 0


def test_mutating_one_evidence_does_not_change_other_buckets_or_future_proposals():
    config = pool.disabled_config()
    config["targets"][0]["provider_quota_facts"][0]["remaining"]["value"] = 0
    assert config["targets"][0]["provider_quota_facts"][1]["remaining"]["value"] is None
    assert (
        pool.disabled_config()["targets"][0]["provider_quota_facts"][0]["remaining"]["value"]
        is None
    )


def test_normal_caps_are_separate_configurable_estimates():
    probe, normal = pool.disabled_config(), pool.disabled_config(normal=True)
    for a, b in zip(probe["targets"], normal["targets"], strict=True):
        assert a["provider"] == b["provider"]
        assert a["enabled"] == b["enabled"] is False
        daily_a = next(
            q["limit"]
            for q in a["local_safety_caps"]
            if q["window"] == "day" and q["metric"] == "requests"
        )
        daily_b = next(
            q["limit"]
            for q in b["local_safety_caps"]
            if q["window"] == "day" and q["metric"] == "requests"
        )
        assert daily_b > daily_a
    ocr = next(t for t in normal["targets"] if t["provider"] == "ocrspace")
    assert next(q["limit"] for q in ocr["local_safety_caps"] if q["window"] == "month") == 20000


def test_formula_uses_published_integer_coefficients_and_ceil():
    assert cloudflare_neuron_upper_bound(1000000, 0) == 2457
    assert cloudflare_neuron_upper_bound(0, 1000000) == 18252
    assert cloudflare_neuron_upper_bound(4096, 256) == 15
    assert cloudflare_neuron_upper_bound(1, 0) == 1
    with pytest.raises(ConfigError):
        cloudflare_neuron_upper_bound(True, 1)


def test_formula_scope_and_underestimate_are_rejected_by_real_parser(tmp_path):
    config = pool.qualified_config(
        {"cloudflare"}, "2026-11-02T03:36:17Z", "2026-09-30T00:00:00Z", normal=True
    )
    target = config["targets"][0]
    assert target["neuron_estimate"]["source"] == CF_NEURON_FORMULA
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert load_gateway_config(path)[0].neuron_estimate.amount == 16
    target["neuron_estimate"]["amount"] = 14
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError):
        load_gateway_config(path)
    target["neuron_estimate"]["amount"] = 16
    target.update(provider="groq", model="openai/gpt-oss-20b")
    target.pop("account_id_ref")
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError):
        load_gateway_config(path)


def test_cf_usage_unknown_does_not_hold_concurrency_forever_and_resets_daily(tmp_path):
    config = pool.qualified_config(
        {"cloudflare"}, "2026-11-02T03:36:17Z", "2026-09-30T00:00:00Z", normal=True
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    targets = load_gateway_config(path)
    now = datetime.now(UTC) + timedelta(seconds=1)
    clock = [now]
    calls = []

    def transport(*args):
        calls.append(1)
        return (
            200,
            {},
            json.dumps(
                {
                    "success": True,
                    "result": {
                        "response": "fixture answer",
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                }
            ).encode(),
        )

    gateway = Gateway(
        tmp_path / "ledger.sqlite",
        targets,
        b"fixture-digest-key-more-than-32-bytes",
        lambda _: "fixture-public-scope",
        transport,
        clock=lambda: clock[0],
    )

    def task(key):
        return {
            "request_key": key,
            "input": "fixture prompt",
            "capability": "text_generation",
            "max_output_tokens": 64,
            "max_attempts": 1,
        }

    for key in ("first", "second"):
        result = gateway.run(task(key))
        assert result["state"] == "completed_usage_unknown"
    assert len(calls) == 2
    assert gateway.broker.status(gateway.status("first")["reservation_id"])["state"] == "unknown"
    clock[0] = now + timedelta(days=1)
    assert gateway.run(task("tomorrow"))["state"] == "completed_usage_unknown"
    assert len(calls) == 3
    assert gateway.run(task("first"))["state"] == "completed_usage_unknown"
    assert len(calls) == 3
