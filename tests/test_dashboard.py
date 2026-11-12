"""Advice uses the real offline Gateway and never generation/secret I/O."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from test_gateway import NOW, task


def dashboard(tmp_path):
    from quota_broker.dashboard import Dashboard
    from quota_broker.dashboard_demo import create_demo

    source = create_demo(tmp_path, clock=lambda: NOW)
    return Dashboard(source, clock=lambda: NOW), source


def test_current_complete_evidence_recommends_without_any_execution(tmp_path):
    board, source = dashboard(tmp_path)
    result = board.advice(task(text="合成測試"))
    assert result["decision"] == "recommend"
    assert result["selected_target"]["target_id"] == "demo-ready"
    assert result["execution_allowed"] is False and result["paid_allowed"] is False
    assert result["source"]["kind"] == "demo"
    for _ in range(2):
        board.snapshot()
        board.advice(task())
    with sqlite3.connect(source.gateway.db) as con:
        for table in ("reservations", "gateway_tasks", "gateway_attempts", "observed_holds"):
            assert con.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.6, 0.6),
        (0.0, 0.0),
        (1.0, 1.0),
        (None, None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_advice_preserves_finite_headroom_fraction_without_widening_counters(
    tmp_path, value, expected
):
    board, source = dashboard(tmp_path)
    plan = source.explain(task())
    plan["candidates"][0]["ranking_factors"]["observed_headroom"] = value
    source.explain = lambda _: plan
    result = board.advice(task())
    assert result["candidates"][0]["ranking"]["observed_headroom"] == expected


@pytest.mark.parametrize(
    "change", ["unknown", "stale", "partial", "insufficient", "paid", "inventory"]
)
def test_uncertain_or_paid_evidence_requires_user(tmp_path, change):
    board, source = dashboard(tmp_path)
    gw = source.gateway
    ready = gw.targets[0]
    if change == "paid":
        gw.targets = (replace(ready, billing_enabled=True), *gw.targets[1:])
        gw.broker.targets = gw.targets
    elif change == "inventory":
        gw.secret_inventory = None
    else:
        facts = list(ready.provider_quota_facts)
        if change == "partial":
            facts = facts[:1]
        else:
            remaining = facts[-1].remaining
            remaining = replace(
                remaining,
                value=None
                if change == "unknown"
                else 0
                if change == "insufficient"
                else remaining.value,
                valid_until=NOW - timedelta(seconds=1)
                if change == "stale"
                else remaining.valid_until,
            )
            facts[-1] = replace(facts[-1], remaining=remaining)
        gw.targets = (replace(ready, provider_quota_facts=tuple(facts)), *gw.targets[1:])
        gw.broker.targets = gw.targets
    result = board.advice(task(provider="google"))
    assert result["decision"] == "ask_user"
    assert result["selected_target"] is None
    assert result["reasons"]
    assert result["execution_allowed"] is False


def test_projection_keeps_windows_units_unknown_and_drops_private_fields(tmp_path):
    board, _source = dashboard(tmp_path)
    result = board.snapshot()
    ready = result["targets"][0]
    assert {(q["metric"], q["window"]) for q in ready["local_caps"]} == {
        ("requests", "rolling_minute"),
        ("requests", "day"),
        ("input_tokens", "rolling_minute"),
    }
    assert any(q["remaining"] is None for row in result["targets"] for q in row["observations"])
    encoded = json.dumps(result)
    for forbidden in (
        "secret_ref",
        "required_names",
        "missing_names",
        "account_id",
        "endpoint",
        "account_source",
    ):
        assert forbidden not in encoded
    assert "total_quota" not in result


def test_candidate_order_preserved_even_when_top_is_blocked(tmp_path):
    board, source = dashboard(tmp_path)
    result = board.advice(task())
    expected = source.gateway.explain(task())["candidates"]
    assert [r["target_id"] for r in result["candidates"]] == [r["target_id"] for r in expected]


def test_unsupported_source_shape_fails_closed(tmp_path):
    from quota_broker.dashboard import DashboardError

    board, source = dashboard(tmp_path)
    source.catalog = lambda: {"secret": "must-never-reflect"}
    with pytest.raises(DashboardError, match="source_unavailable"):
        board.snapshot()


def test_changed_diagnostics_blocks_old_eligible_plan(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.diagnostics

    def changed():
        result = original()
        result["targets"][0].update(state="blocked", reasons=["disabled"])
        return result

    source.diagnostics = changed
    assert board.advice(task(provider="google"))["decision"] == "ask_user"


@pytest.mark.parametrize("where", ["diagnostics", "explain"])
def test_identity_mismatch_fails_closed(tmp_path, where):
    from quota_broker.dashboard import DashboardError

    board, source = dashboard(tmp_path)
    original = getattr(source, where)

    def changed(*args):
        result = original(*args)
        result["targets" if where == "diagnostics" else "candidates"][0]["model"] = (
            "different-model"
        )
        return result

    setattr(source, where, changed)
    with pytest.raises(DashboardError):
        board.advice(task())


@pytest.mark.parametrize(
    "metric,remaining,expected",
    [
        ("output_tokens", 15, "ask_user"),
        ("total_tokens", 16, "ask_user"),
        ("tokens", 16, "ask_user"),
        ("output_tokens", 16, "recommend"),
        ("total_tokens", 10000, "recommend"),
        ("tokens", 10000, "recommend"),
    ],
)
def test_all_observed_metrics_use_correct_task_cost(tmp_path, metric, remaining, expected):
    board, source = dashboard(tmp_path)
    original = source.explain

    def changed(raw):
        plan = original(raw)
        row = next(r for r in plan["candidates"] if r["target_id"] == "demo-ready")
        row["quota_observations"].append(
            {
                **row["quota_observations"][0],
                "metric": metric,
                "remaining": remaining,
                "effective_remaining": remaining,
            }
        )
        return plan

    source.explain = changed
    assert board.advice(task(provider="google"))["decision"] == expected


def test_contradictory_duplicate_or_stale_evidence_cannot_be_overwritten(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.explain

    def changed(raw):
        plan = original(raw)
        row = next(r for r in plan["candidates"] if r["target_id"] == "demo-ready")
        row["quota_observations"].insert(
            0, {**row["quota_observations"][0], "remaining": 0, "effective_remaining": 0}
        )
        return plan

    source.explain = changed
    assert board.advice(task(provider="google"))["decision"] == "ask_user"


def test_caller_neuron_bound_cannot_replace_verified_estimate(tmp_path):
    board, source = dashboard(tmp_path)
    original = source.explain

    def changed(raw):
        plan = original(raw)
        row = next(r for r in plan["candidates"] if r["target_id"] == "demo-ready")
        row["effective_neuron_bound"] = 1
        row["neuron_estimate"] = None
        row["quota_observations"].append(
            {
                **row["quota_observations"][0],
                "metric": "neurons",
                "remaining": 100,
                "effective_remaining": 100,
            }
        )
        return plan

    source.explain = changed
    assert board.advice(task(provider="google"))["decision"] == "ask_user"


def test_catalog_local_window_usage_includes_holds_and_never_expires_or_writes(tmp_path):
    from quota_broker.dashboard_demo import create_demo

    current = [NOW]
    source = create_demo(tmp_path, clock=lambda: current[0])
    gw = source.gateway
    gw.broker.reserve(
        {
            "request_key": "held-for-catalog",
            "capability": "text_generation",
            "provider": "google",
            "input_token_bound": 300,
            "max_output_tokens": 16,
        }
    )
    before = (tmp_path / "dashboard-demo.db").read_bytes()
    initial = gw.catalog()[0]["local_ledger_usage"]
    assert [(q["metric"], q["window"], q["used"]) for q in initial] == [
        ("requests", "rolling_minute", 1),
        ("requests", "day", 1),
        ("input_tokens", "rolling_minute", 300),
    ]
    assert all(
        q["basis"] == "local_ledger_current_window_including_holds" and "bucket" not in q
        for q in initial
    )
    assert (tmp_path / "dashboard-demo.db").read_bytes() == before
    current[0] = NOW + timedelta(seconds=60)
    later = gw.catalog()[0]["local_ledger_usage"]
    assert [q["used"] for q in later] == [0, 1, 0]
    with sqlite3.connect(gw.db) as con:
        assert con.execute("SELECT state FROM reservations").fetchone()[0] == "reserved"
    assert (tmp_path / "dashboard-demo.db").read_bytes() == before


@pytest.mark.parametrize("provider", ["eighth", "groq"])
def test_instance_registry_constraints_use_actual_gateway_authority(tmp_path, provider):
    from test_registry import manifest

    from quota_broker.registry import Registry

    board, source = dashboard(tmp_path)
    document = manifest("registered-fixture-model")
    if provider == "groq":
        spec = Registry.builtin().resolve("openai/gpt-oss-20b", provider)
        document["providers"][0].update(
            id=provider, origin=spec.origin, endpoint=spec.endpoint_template
        )
        document["models"][0]["provider"] = provider
    registry = Registry.from_manifest(document)
    custom = replace(
        source.gateway.targets[0],
        id="custom-fixture",
        provider=provider,
        model="registered-fixture-model",
        model_info=registry.resolve("registered-fixture-model", provider),
    )
    source.gateway.targets = source.gateway.broker.targets = (custom,)
    source.gateway.registry = source.gateway.broker.registry = registry
    raw = task(provider=provider, model="registered-fixture-model")
    assert source.gateway.explain(raw)["selected_target_id"] == "custom-fixture"
    assert board.advice(raw)["selected_target"]["target_id"] == "custom-fixture"


def test_unreviewed_limit_sources_and_scopes_never_leak_to_dashboard(tmp_path):
    board, source = dashboard(tmp_path)
    source.kind = "gateway"
    original = source.catalog

    def catalog():
        rows = original()
        for fact in rows[0]["provider_quota_facts"]:
            fact["limit"]["source"] = "https://example.invalid/?token=synthetic-marker"
            fact["limit"]["scope"] = "private-project-123"
        return rows

    source.catalog = catalog
    encoded = json.dumps(board.snapshot())
    assert "synthetic-marker" not in encoded
    assert "private-project-123" not in encoded


def test_explicit_credit_capacity_does_not_require_local_token_caps(tmp_path):
    board, source = dashboard(tmp_path)
    original_catalog = source.catalog()
    original_diag = source.diagnostics()
    original_plan = source.explain(task(provider="google"))
    ready = original_catalog[0]
    identity = ready["target_id"]
    ready["required_capacity"] = [{"metric": "credits_usd_micros", "window": "budget"}]
    credit = {
        "metric": "credits_usd_micros",
        "window": "budget",
        "remaining": 200000000,
        "effective_remaining": 199999900,
        "local_observation_hold": 100,
        "as_of": (NOW - timedelta(seconds=1)).isoformat(),
        "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
        "provenance": "observed",
        "confidence": "administrator_verified",
    }
    next(q for q in original_diag["targets"] if q["target_id"] == identity)[
        "quota_observations"
    ] = [credit]
    candidate = next(q for q in original_plan["candidates"] if q["target_id"] == identity)
    candidate.update(
        eligible=True,
        reasons=[],
        quota_observations=[credit],
        resource_cost_bounds={"credits_usd_micros": 100},
    )
    source.catalog = lambda: original_catalog
    source.diagnostics = lambda: original_diag
    source.explain = lambda _: original_plan
    result = board.advice(task(provider="google"))
    assert result["decision"] == "recommend"
    assert result["selected_target"]["target_id"] == identity
    projected = board.snapshot()["targets"][0]
    assert projected["required_capacity"] == ready["required_capacity"]
    # A missing cost never becomes a zero-cost request.
    candidate["resource_cost_bounds"] = {}
    stopped = board.advice(task(provider="google"))
    assert stopped["decision"] == "ask_user"
    assert "quota_cost_unknown:credits_usd_micros" in stopped["candidates"][0]["reasons"]


@pytest.mark.parametrize(
    "provider,profile,model",
    [
        ("openai", "openai_responses", "gpt-6-luna"),
        ("openrouter", "openai_inference", "liquid/lfm-2.5-2.6b:free"),
    ],
)
def test_shared_text_routes_recommend_same_typed_contract_without_execution(
    tmp_path, provider, profile, model
):
    from test_family_gateway import catalog
    from test_gateway import HMAC_KEY, target

    from quota_broker.config import Capacity, ResourceEstimate, SecretInventory
    from quota_broker.dashboard import Dashboard, GatewaySource
    from quota_broker.gateway import Gateway
    from quota_broker.registry import Registry

    registry = (
        Registry.builtin()
        if provider == "openrouter"
        else catalog(provider, profile, "text_generation", model)
    )
    metric, window = (
        ("credits_usd_micros", "budget") if provider == "openai" else ("requests", "day")
    )
    item = replace(
        target(provider, provider, model),
        model_info=registry.resolve(model, provider),
        quota_basis="local_safety_cap",
        required_capacity=((metric, window),),
        resource_estimates=(
            ResourceEstimate(
                metric,
                100,
                4096,
                64,
                "trusted_operator",
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=10),
            ),
        )
        if provider == "openai"
        else (),
        capacity=Capacity(kind="one_time_gift", credit_expires_at=NOW + timedelta(hours=1))
        if provider == "openai"
        else Capacity(),
    )

    def forbidden(*_):
        raise AssertionError("advice must never resolve credentials or call provider")

    gateway = Gateway(
        tmp_path / "shared-dashboard.sqlite3",
        (item,),
        HMAC_KEY,
        forbidden,
        registry=registry,
        clock=lambda: NOW,
        family_transport=forbidden,
        secret_inventory=SecretInventory(
            frozenset({provider.upper()}),
            NOW - timedelta(seconds=1),
            NOW + timedelta(minutes=10),
            "fixture",
            "fixture",
        ),
    )
    gateway.observe_quota(
        item.id,
        {
            "metric": metric,
            "window": window,
            "remaining": 200000000 if provider == "openai" else 49,
            "limit": 200000000 if provider == "openai" else 50,
            "as_of": (NOW - timedelta(seconds=1)).isoformat(),
            "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
            "reset_at": None,
            "provenance": "observed",
            "source": "trusted_operator",
            "confidence": "administrator_verified",
        },
    )
    source = GatewaySource(gateway)
    seen = []

    def explain(raw):
        seen.append(raw)
        return gateway.explain(raw)

    source.explain = explain
    board = Dashboard(source, clock=lambda: NOW)
    result = board.advice(
        {
            "request_key": "shared-advice",
            "capability": "text_generation",
            "input": "Compute 17 + 25.",
            "max_output_tokens": 32,
        }
    )
    assert result["decision"] == "recommend", [
        q["reasons"] for q in gateway.diagnostics()["targets"]
    ]
    assert result["selected_target"]["provider"] == provider
    assert seen[0]["input"] == {"messages": [{"role": "user", "content": "Compute 17 + 25."}]}
    assert result["execution_allowed"] is False
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM gateway_attempts").fetchone()[0] == 0
