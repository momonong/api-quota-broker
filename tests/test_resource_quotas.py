"""Trusted resource costs and atomic local/observed quota accounting; offline."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_broker import Clock, report, request, target
from test_family_gateway import catalog
from test_framework import observation

from quota_broker.config import (
    ConfigError,
    Quota,
    ResourceEstimate,
    load_gateway_config,
    parse_quota_facts,
    parse_resource_estimates,
)
from quota_broker.core import Broker, BrokerError, month_bounds, stamp
from quota_broker.registry import OpenAIChatAdapter, Registry
from quota_broker.routing import RoutingState

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def resource_target(clock, *quotas, **changes):
    return replace(target(clock, concurrent=12), quotas=tuple(quotas), **changes)


def reserve(broker, key, bounds, **changes):
    return broker.reserve(
        {**request(key, input_bound=0, output_max=1), "resource_bounds": bounds, **changes}
    )


def charges(result):
    return {row["metric"]: row["amount"] for row in result["charges"]}


@pytest.mark.parametrize(
    "metric", ["audio_seconds", "pages", "images", "conversions", "output_tokens", "total_tokens"]
)
def test_resources_are_not_tokens_or_guessed_requests(tmp_path, metric):
    clock = Clock(NOW)
    item = resource_target(
        clock, Quota("requests", "requests", 10, "day"), Quota(metric, metric, 10, "rolling_hour")
    )
    broker = Broker(tmp_path / "resources.sqlite", (item,), clock)
    with pytest.raises(BrokerError, match="explicit trusted resource bound"):
        reserve(broker, "missing", {})
    first = reserve(broker, "first", {metric: 7})
    assert charges(first) == {"requests": 1, metric: 7}
    assert (
        first["route_current"]
        and broker.reserve(
            {**request("first", input_bound=0, output_max=1), "resource_bounds": {metric: 7}}
        )
        == first
    )
    broker.dispatch(first["reservation_id"])
    with pytest.raises(BrokerError, match="every actual quota metric"):
        broker.report(report(first, "partial", usage={"requests": 1}))
    assert broker.status(first["reservation_id"])["state"] == "dispatched"
    actual = broker.report(report(first, "actual", usage={"requests": 1, metric: 3}))
    assert actual["state"] == "completed" and charges(actual)[metric] == 3
    assert reserve(broker, "next", {metric: 7})["state"] == "reserved"


def test_missing_token_resource_is_unknown_not_zero_or_envelope_output(tmp_path):
    clock = Clock(NOW)
    for metric in ("input_tokens", "output_tokens", "total_tokens"):
        broker = Broker(
            tmp_path / (metric + ".sqlite"),
            (resource_target(clock, Quota(metric, metric, 100, "day")),),
            clock,
        )
        with pytest.raises(BrokerError, match="explicit trusted resource bound"):
            reserve(broker, "absent", {})
    with pytest.raises(BrokerError, match="positive integer"):
        broker.reserve(request("legacy-zero", input_bound=0))
    for bad in ({"pages": True}, {"pages": -1}, {"pages": 10**13}, {"unknown": 1}):
        with pytest.raises(BrokerError, match="invalid trusted"):
            reserve(broker, "bad", bad)


def test_shared_hour_caps_admit_one_competing_worker_across_targets(tmp_path):
    clock = Clock(NOW)
    quota = Quota("shared-audio-hour", "audio_seconds", 60, "rolling_hour")
    one = resource_target(clock, quota, id="one")
    two = replace(one, id="two", model="gemini-3.5-flash-lite")
    broker = Broker(tmp_path / "shared.sqlite", (one, two), clock)
    barrier = threading.Barrier(8)

    def compete(number):
        barrier.wait()
        try:
            return reserve(broker, f"worker-{number}", {"audio_seconds": 60})["state"]
        except BrokerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(compete, range(8)))
    assert outcomes.count("reserved") == 1 and outcomes.count("unavailable") == 7
    with sqlite3.connect(broker.db) as con:
        row = con.execute("SELECT id FROM reservations WHERE state='reserved'").fetchone()
    broker.dispatch(row[0])
    broker.report(
        {
            "reservation_id": row[0],
            "report_key": "actual",
            "state": "completed",
            "usage": {"audio_seconds": 60},
        }
    )
    clock.advance(seconds=3599)
    with pytest.raises(BrokerError) as error:
        reserve(broker, "too-soon", {"audio_seconds": 1})
    assert error.value.wait_until == stamp(NOW + timedelta(hours=1))
    clock.advance(seconds=1)
    assert reserve(broker, "after-hour", {"audio_seconds": 60})["state"] == "reserved"


def test_month_boundary_dispatch_competes_atomically_and_preserves_old_charge(tmp_path):
    clock = Clock(datetime(2026, 10, 1, 6, 59, 50, tzinfo=UTC))  # Pacific Sep30.
    quota = Quota("shared-pages-month", "pages", 5, "month", "America/Los_Angeles")
    broker = Broker(tmp_path / "month.sqlite", (resource_target(clock, quota),), clock)
    old = reserve(broker, "old-month", {"pages": 5})
    clock.advance(seconds=11)
    new = reserve(broker, "new-month", {"pages": 5})
    broker.dispatch(new["reservation_id"])
    with pytest.raises(BrokerError, match="quota changed before dispatch"):
        broker.dispatch(old["reservation_id"])
    unchanged = broker.status(old["reservation_id"])
    assert unchanged["state"] == "reserved" and charges(unchanged)["pages"] == 5
    assert unchanged["charges"][0]["day_start"] == stamp(datetime(2026, 9, 1, 7, tzinfo=UTC))
    assert broker.status(new["reservation_id"])["charges"][0]["day_start"] == stamp(
        datetime(2026, 10, 1, 7, tzinfo=UTC)
    )


def test_calendar_month_is_not_fixed_30_days_and_handles_dst_year_wrap():
    assert month_bounds(datetime(2024, 2, 29, 12, tzinfo=UTC), "UTC") == (
        datetime(2024, 2, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert month_bounds(datetime(2026, 3, 20, tzinfo=UTC), "America/Los_Angeles") == (
        datetime(2026, 3, 1, 8, tzinfo=UTC),
        datetime(2026, 4, 1, 7, tzinfo=UTC),
    )
    assert month_bounds(datetime(2026, 12, 31, tzinfo=UTC), "UTC")[1] == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_unknown_audio_preserves_ledger_and_restart_does_not_release_it(tmp_path):
    clock = Clock(NOW)
    item = resource_target(
        clock, Quota("audio-day", "audio_seconds", 60, "day"), concurrency_limit=1
    )
    broker = Broker(tmp_path / "unknown.sqlite", (item,), clock)
    first = reserve(broker, "unknown-audio", {"audio_seconds": 40})
    broker.dispatch(first["reservation_id"])
    broker.report(report(first, "unknown", state="unknown"))
    clock.advance(minutes=2)
    restarted = Broker(broker.db, (item,), clock)
    assert charges(restarted.status(first["reservation_id"])) == {"audio_seconds": 40}
    with pytest.raises(BrokerError):
        reserve(restarted, "replay", {"audio_seconds": 1})
    reconciled = restarted.report(report(first, "reconcile", usage={"audio_seconds": 20}))
    assert reconciled["state"] == "completed" and charges(reconciled) == {"audio_seconds": 20}


def test_trusted_estimates_are_dated_scoped_and_take_max_not_reduce_local_cost():
    clock = Clock(NOW)
    estimate = ResourceEstimate(
        "pages",
        10,
        1000,
        50,
        "trusted_operator",
        NOW - timedelta(seconds=1),
        NOW + timedelta(minutes=1),
    )
    item = resource_target(
        clock, Quota("pages", "pages", 100, "month"), resource_estimates=(estimate,)
    )
    assert item.resource_costs({"pages": 3}, 999, 50, NOW) == {"pages": 10}
    assert item.resource_costs({"pages": 11}, 999, 50, NOW) == {"pages": 11}
    assert item.resource_costs({}, 999, 50, NOW) == {"pages": 10}
    for size, output, now in (
        (1001, 50, NOW),
        (999, 51, NOW),
        (999, 50, NOW + timedelta(minutes=1)),
        (999, 50, NOW - timedelta(seconds=2)),
    ):
        assert item.resource_costs({}, size, output, now) == {}
    raw = estimate.view()
    assert parse_resource_estimates([raw]) == (estimate,)
    for changed in (
        {"amount": True},
        {"metric": "unsupported"},
        {"source": "provider_guess"},
        {"expires_at": stamp(NOW - timedelta(seconds=2))},
        {"max_input_bytes": 0},
        {"raw": "fixture private"},
    ):
        with pytest.raises(ConfigError):
            parse_resource_estimates([{**raw, **changed}])
    with pytest.raises(ConfigError, match="duplicate"):
        parse_resource_estimates([raw, raw])


def test_config_supports_resource_facts_hour_month_and_only_nonempty_snapshot_estimates(tmp_path):
    document = json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())
    item = document["targets"][0]
    item["local_safety_caps"].extend(
        [
            {
                "bucket": "pages-month",
                "metric": "pages",
                "limit": 100,
                "window": "month",
                "timezone": "America/Los_Angeles",
            },
            {
                "bucket": "audio-hour",
                "metric": "audio_seconds",
                "limit": 100,
                "window": "rolling_hour",
            },
        ]
    )
    estimate = ResourceEstimate(
        "pages", 10, 1000, 50, "trusted_operator", NOW, NOW + timedelta(minutes=1)
    )
    item["resource_estimates"] = [estimate.view()]
    unknown = {
        "value": None,
        "provenance": "unknown",
        "as_of": None,
        "source": None,
        "scope": None,
        "valid_until": None,
    }
    item["provider_quota_facts"] = [
        {
            "metric": "audio_seconds",
            "window": "rolling_hour",
            "limit": unknown,
            "remaining": unknown,
        }
    ]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [item]}))
    loaded = load_gateway_config(config)[0]
    assert loaded.resource_estimates == (estimate,)
    assert loaded.provider_quota_facts[0].metric == "audio_seconds"
    assert "resource_estimates" in json.loads(Broker._snapshot(loaded))
    assert "resource_estimates" not in json.loads(
        Broker._snapshot(replace(loaded, resource_estimates=()))
    )
    with pytest.raises(ConfigError):
        parse_quota_facts([{**item["provider_quota_facts"][0], "metric": "invented"}])


def test_prepared_resources_persist_separately_without_changing_legacy_identity(tmp_path):
    clock = Clock(NOW)
    item = resource_target(
        clock, Quota("requests", "requests", 10, "day"), Quota("input", "input_tokens", 100, "day")
    )
    broker = Broker(tmp_path / "identity.sqlite", (item,), clock)
    legacy = broker.reserve(request("legacy", input_bound=10))
    prepared = reserve(broker, "typed", {"input_tokens": 20}, input_token_bound=20)
    with sqlite3.connect(broker.db) as con:
        rows = con.execute(
            "SELECT request_key,reserve_request,target_snapshot FROM reservations ORDER BY request_key"
        ).fetchall()
    assert rows[0][0] == "legacy" and rows[0][1] is None
    assert rows[0][2] == rows[1][2] == Broker._snapshot(item)
    assert json.loads(rows[1][1])["resource_bounds"] == {"requests": 1, "input_tokens": 20}
    assert legacy["route_current"] and prepared["route_current"]
    assert broker.dispatch(prepared["reservation_id"])["state"] == "dispatched"


def test_family_selection_uses_registered_adapter_contract(tmp_path):
    class FixtureFamilyAdapter(OpenAIChatAdapter):
        supported_capabilities = frozenset({"transcription"})

    baseline = Registry.builtin().resolve("gemini-2.5-flash-lite", "google")
    spec = replace(baseline, adapter="fixture_resource", capabilities=("transcription",))
    registry = Registry(
        {("google", spec.model): spec}, adapters={"fixture_resource": FixtureFamilyAdapter()}
    )
    clock = Clock(NOW)
    item = resource_target(clock, Quota("audio", "audio_seconds", 100, "day"), model_info=spec)
    broker = Broker(tmp_path / "family.sqlite", (item,), clock, registry)
    assert (
        reserve(broker, "audio", {"audio_seconds": 10}, capability="transcription")["state"]
        == "reserved"
    )
    with pytest.raises(BrokerError):
        reserve(broker, "unknown-family", {"audio_seconds": 10}, capability="invented")


def test_observed_resource_holds_are_explicit_independent_and_preserve_unknown(tmp_path):
    clock = Clock(NOW)
    broker = Broker(
        tmp_path / "observations.sqlite",
        (resource_target(clock, Quota("requests", "requests", 10, "day")),),
        clock,
    )
    routing = RoutingState(broker.db)
    for metric, window in (
        ("audio_seconds", "rolling_hour"),
        ("pages", "month"),
        ("output_tokens", "rolling_minute"),
        ("tokens", "rolling_minute"),
    ):
        routing.observe("scope", observation(100, metric=metric, window=window, at=NOW), NOW)
    first = reserve(broker, "held", {"audio_seconds": 10})
    with broker._tx() as con, pytest.raises(ValueError, match="explicit resource bound"):
        routing.hold(con, "scope", first["reservation_id"], 0, 1, None, NOW, resource_bounds={})
    with broker._tx() as con:
        routing.hold(
            con,
            "scope",
            first["reservation_id"],
            0,
            1,
            None,
            NOW,
            resource_bounds={
                "audio_seconds": 10,
                "pages": 3,
                "output_tokens": 20,
                "total_tokens": 25,
            },
        )
    broker.dispatch(first["reservation_id"])
    broker.report(report(first, "unknown", state="unknown"))
    routing.reset_health(first["target_id"])
    with broker._tx() as con:
        observations = {row["metric"]: row for row in routing.observations(con, "scope", NOW)}
        assert observations["audio_seconds"]["effective_remaining"] == 90
        assert observations["pages"]["effective_remaining"] == 97
        assert observations["output_tokens"]["effective_remaining"] == 80
        assert observations["tokens"]["effective_remaining"] == 75
    assert broker.status(first["reservation_id"])["state"] == "unknown"


def test_selected_target_estimates_fill_missing_resource_and_expiry_fences_dispatch(tmp_path):
    clock = Clock(NOW)
    estimate = ResourceEstimate(
        "audio_seconds",
        40,
        1000,
        1,
        "trusted_operator",
        NOW - timedelta(seconds=1),
        NOW + timedelta(seconds=5),
    )
    one = resource_target(
        clock,
        Quota("audio-one", "audio_seconds", 100, "day"),
        id="one",
        resource_estimates=(estimate,),
    )
    two = replace(
        one,
        id="two",
        quotas=(Quota("audio-two", "audio_seconds", 100, "day"),),
        resource_estimates=(replace(estimate, amount=70),),
    )
    broker = Broker(tmp_path / "scoped-estimate.sqlite", (one, two), clock)
    first = reserve(broker, "estimated-one", {}, input_bytes=100)
    second = reserve(broker, "estimated-two", {}, input_bytes=100, exclude_target_ids=["one"])
    assert charges(first) == {"audio_seconds": 40} and charges(second) == {"audio_seconds": 70}
    with sqlite3.connect(broker.db) as con:
        saved = json.loads(
            con.execute(
                "SELECT reserve_request FROM reservations WHERE id=?", (first["reservation_id"],)
            ).fetchone()[0]
        )
    assert saved["input_bytes"] == 100 and saved["resource_estimates_valid_until"] == stamp(
        NOW + timedelta(seconds=5)
    )
    clock.advance(seconds=5)
    with pytest.raises(BrokerError, match="estimate expired before dispatch"):
        broker.dispatch(first["reservation_id"])
    assert broker.status(first["reservation_id"])["state"] == "reserved"
    assert charges(broker.status(first["reservation_id"])) == {"audio_seconds": 40}
    with pytest.raises(BrokerError, match="explicit trusted resource bound"):
        reserve(broker, "new-expired", {}, input_bytes=100)


def test_adequate_local_resource_proof_does_not_depend_on_unused_operator_estimate(tmp_path):
    clock = Clock(NOW)
    estimate = ResourceEstimate(
        "pages",
        5,
        1000,
        1,
        "trusted_operator",
        NOW - timedelta(seconds=1),
        NOW + timedelta(seconds=2),
    )
    item = resource_target(
        clock, Quota("pages", "pages", 100, "month"), resource_estimates=(estimate,)
    )
    broker = Broker(tmp_path / "local-proof.sqlite", (item,), clock)
    first = reserve(broker, "local-proof", {"pages": 10}, input_bytes=100)
    clock.advance(seconds=3)
    assert broker.dispatch(first["reservation_id"])["state"] == "dispatched"
    for invalid in (-1, True, 10**13):
        with pytest.raises(BrokerError, match="invalid trusted input size"):
            reserve(broker, "invalid", {}, input_bytes=invalid)


def test_non_token_typed_zero_output_bound_reserves_and_dispatches_without_fake_tokens(tmp_path):
    clock = Clock(NOW)
    estimate = ResourceEstimate(
        "audio_seconds",
        10,
        1000,
        0,
        "trusted_operator",
        NOW - timedelta(seconds=1),
        NOW + timedelta(minutes=1),
    )
    item = resource_target(
        clock, Quota("audio", "audio_seconds", 100, "day"), resource_estimates=(estimate,)
    )
    broker = Broker(tmp_path / "zero-output.sqlite", (item,), clock)
    first = reserve(broker, "non-token-zero", {}, max_output_tokens=0, input_bytes=100)
    assert charges(first) == {"audio_seconds": 10}
    assert broker.dispatch(first["reservation_id"])["state"] == "dispatched"
    with sqlite3.connect(broker.db) as con:
        prepared = json.loads(
            con.execute(
                "SELECT reserve_request FROM reservations WHERE id=?", (first["reservation_id"],)
            ).fetchone()[0]
        )
    assert prepared["max_output_tokens"] == 0 and "output_tokens" not in prepared["resource_bounds"]
    for bounds in ({"output_tokens": 0}, {"output_tokens": 5}):
        with pytest.raises(BrokerError, match="positive integer"):
            reserve(broker, "token-zero", bounds, max_output_tokens=0)
    with pytest.raises(BrokerError, match="positive integer"):
        broker.reserve(request("legacy-zero-output", output_max=0))


def test_nonempty_family_adapter_mapping_changes_snapshot_and_fences_unsent_dispatch(tmp_path):
    clock = Clock(NOW)
    base = Registry.builtin().resolve("gemini-2.5-flash-lite", "google")
    fields = {**base.__dict__, "family_adapters": ()}
    item = resource_target(
        clock, Quota("requests", "requests", 10, "day"), model_info=SimpleNamespace(**fields)
    )
    original_snapshot = Broker._snapshot(item)
    assert "family_adapters" not in json.loads(original_snapshot)
    mapped = replace(
        item,
        model_info=SimpleNamespace(
            **{**fields, "family_adapters": (("text_generation", "gemini_inference"),)}
        ),
    )
    assert json.loads(Broker._snapshot(mapped))["family_adapters"] == [
        ["text_generation", "gemini_inference"]
    ]
    broker = Broker(tmp_path / "mapping.sqlite", (mapped,), clock)
    first = broker.reserve(request("mapping-fence"))
    changed = replace(
        mapped,
        model_info=SimpleNamespace(
            **{**fields, "family_adapters": (("text_generation", "another_fixture_profile"),)}
        ),
    )
    restarted = Broker(broker.db, (changed,), clock)
    assert not restarted.status(first["reservation_id"])["route_current"]
    with pytest.raises(BrokerError, match="route or quota changed"):
        restarted.dispatch(first["reservation_id"])
    assert restarted.status(first["reservation_id"])["state"] == "reserved"
    assert Broker._snapshot(replace(item, model_info=base)) == original_snapshot


def zero_limit_registry():
    original = catalog()
    spec = original.resolve("fixture-embed", "mistral")
    return Registry(
        {("mistral", "fixture-embed"): replace(spec, context_tokens=0, max_output_tokens=0)}
    )


def test_non_token_zero_model_limits_config_and_context_are_not_fake_one(tmp_path):
    registry = zero_limit_registry()
    original = json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())
    item = original["targets"][0]
    item.update(
        provider="mistral",
        model="fixture-embed",
        max_output_tokens=0,
        enabled=True,
        free_eligible=True,
        verified_at=stamp(NOW - timedelta(minutes=1)),
        expires_at=stamp(NOW + timedelta(hours=1)),
        provider_quota_facts=[],
    )
    config = tmp_path / "zero-token-config.json"
    config.write_text(json.dumps({"targets": [item]}))
    loaded = load_gateway_config(config, registry=registry)[0]
    assert loaded.max_output_tokens == 0 and loaded.model_info.context_tokens == 0
    broker = Broker(tmp_path / "zero-token.sqlite", (loaded,), lambda: NOW, registry)
    plan = reserve(
        broker,
        "embedding-with-no-token-context",
        {"input_tokens": 100},
        capability="embedding",
        input_token_bound=100,
        max_output_tokens=0,
    )
    assert (
        plan["state"] == "reserved"
        and broker.dispatch(plan["reservation_id"])["state"] == "dispatched"
    )
    with pytest.raises(BrokerError):
        broker.reserve({**request("legacy-context"), "capability": "embedding"})
    item["max_output_tokens"] = True
    config.write_text(json.dumps({"targets": [item]}))
    with pytest.raises(ConfigError, match="invalid max output"):
        load_gateway_config(config, registry=registry)


def test_legacy_token_model_target_zero_stays_invalid(tmp_path):
    document = json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())
    document["targets"][0]["max_output_tokens"] = 0
    config = tmp_path / "invalid-token-config.json"
    config.write_text(json.dumps(document))
    with pytest.raises(ConfigError, match="invalid max output"):
        load_gateway_config(config)


def test_zero_token_model_context_cannot_be_bypassed_by_empty_resource_map(tmp_path):
    clock = Clock(NOW)
    baseline = Registry.builtin().resolve("gemini-2.5-flash-lite", "google")
    spec = replace(baseline, context_tokens=0, max_output_tokens=0)
    registry = Registry({("google", spec.model): spec})
    item = resource_target(
        clock, Quota("requests", "requests", 10, "day"), max_output_tokens=0, model_info=spec
    )
    broker = Broker(tmp_path / "bad-token-zero.sqlite", (item,), clock, registry)
    with pytest.raises(BrokerError):
        reserve(broker, "zero-token-model", {}, max_output_tokens=0)
