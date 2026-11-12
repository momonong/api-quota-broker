"""One account budget shared by all projects; no real keys or provider calls."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from test_family_gateway import catalog
from test_gateway import HMAC_KEY, NOW, target
from test_gateway_access import TOKENS, policy_file
from test_shared_provider_contracts import responses_body

from quota_broker.access import AccessPolicyFile, ProjectAccess
from quota_broker.config import Capacity, ResourceEstimate
from quota_broker.core import BrokerError
from quota_broker.gateway import Gateway, GatewayError

METRIC = "credits_usd_micros"


def setup_credit(tmp_path, *, scoped=False, remaining=200, unknown=False):
    registry = catalog("openai", "openai_responses", "text_generation", "gpt-6-luna")
    clock = [NOW]
    calls = []
    item = replace(
        target("openai", "openai", "gpt-6-luna"),
        model_info=registry.resolve("gpt-6-luna", "openai"),
        required_capacity=((METRIC, "budget"),),
        quota_basis="local_safety_cap",
        resource_estimates=(
            ResourceEstimate(
                METRIC,
                100,
                4096,
                64,
                "trusted_operator",
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=10),
            ),
        ),
        capacity=Capacity(kind="one_time_gift", credit_expires_at=NOW + timedelta(minutes=30)),
    )

    def transport(*args):
        calls.append(args)
        if unknown:
            raise TimeoutError("fixture outcome unknown")
        return 200, {}, json.dumps(responses_body()).encode()

    gateway = Gateway(
        tmp_path / "shared.sqlite3",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-secret",
        clock=lambda: clock[0],
        registry=registry,
        family_transport=transport,
    )
    observation = {
        "metric": METRIC,
        "window": "budget",
        "remaining": remaining,
        "limit": 200,
        "as_of": (NOW - timedelta(seconds=1)).isoformat(),
        "valid_until": (NOW + timedelta(minutes=20)).isoformat(),
        "reset_at": None,
        "provenance": "observed",
        "source": "trusted_operator",
        "confidence": "administrator_verified",
    }
    gateway.observe_quota(item.id, observation)
    access = ProjectAccess(gateway, AccessPolicyFile(policy_file(tmp_path))) if scoped else None
    return gateway, item, calls, clock, access


def body(key):
    return {
        "request_key": key,
        "capability": "text_generation",
        "max_output_tokens": 32,
        "input": {"messages": [{"role": "user", "content": "Compute 17 + 25."}]},
    }


def run_project(access, who, key):
    principal = access.policy.authenticate(TOKENS[who])
    _internal, data = access.claim(principal, "tasks", body(key))
    return access.gateway.run(data)


def test_shared_budget_is_not_copied_per_project_or_reported_as_actual_usd(tmp_path):
    gateway, _item, calls, _clock, access = setup_credit(tmp_path, scoped=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda who: run_project(access, who, "same-external-key"), ["a", "b"])
        )
    assert [r["state"] for r in results] == ["completed", "completed"]
    assert len(calls) == 2
    assert all(METRIC not in r["reported_resources"] for r in results)
    assert gateway.explain(body("third"))["selected_target_id"] is None
    with sqlite3.connect(gateway.db) as con:
        assert (
            con.execute(
                "SELECT sum(amount) FROM observed_holds WHERE metric=?", (METRIC,)
            ).fetchone()[0]
            == 200
        )
    run_project(access, "a", "same-external-key")
    assert len(calls) == 2


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "problem", ["missing", "stale", "unknown", "too_small", "expired_price", "expired_credit"]
)
def test_credit_gate_stops_every_gateway_entry_before_provider_io(tmp_path, scoped, problem):
    gateway, _item, calls, clock, access = setup_credit(tmp_path, scoped=scoped)
    with sqlite3.connect(gateway.db) as con:
        if problem == "missing":
            con.execute("DELETE FROM quota_observations")
        elif problem in {"unknown", "too_small"}:
            con.execute(
                "UPDATE quota_observations SET observation_json=json_set(observation_json,'$.remaining',?)",
                (None if problem == "unknown" else 99,),
            )
    if problem == "stale":
        clock[0] += timedelta(minutes=21)
    elif problem == "expired_price":
        clock[0] += timedelta(minutes=11)
    elif problem == "expired_credit":
        clock[0] += timedelta(minutes=31)
    assert gateway.explain(body("blocked"))["selected_target_id"] is None
    try:
        result = run_project(access, "a", "blocked") if scoped else gateway.run(body("blocked"))
        assert result["state"] != "completed"
    except (GatewayError, BrokerError):
        pass
    assert calls == []


def test_credit_observation_missing_after_reserve_fences_dispatch(tmp_path):
    gateway, _item, calls, _clock, _access = setup_credit(tmp_path)
    reservation = gateway.broker.reserve(
        {
            "request_key": "reserved",
            "capability": "text_generation",
            "input_token_bound": 100,
            "max_output_tokens": 32,
            "resource_bounds": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 32,
                "total_tokens": 132,
            },
            "input_bytes": 100,
        }
    )
    with sqlite3.connect(gateway.db) as con:
        con.execute("DELETE FROM quota_observations")
    with pytest.raises(BrokerError):
        gateway.broker.dispatch(reservation["reservation_id"])
    assert calls == []


def test_unknown_credit_hold_survives_restart_and_is_never_replayed(tmp_path):
    gateway, item, calls, clock, _access = setup_credit(tmp_path, unknown=True)
    result = gateway.run(body("timeout"))
    assert result["state"] == "unknown" and len(calls) == 1
    restarted = Gateway(
        gateway.db,
        (item,),
        HMAC_KEY,
        lambda _: "fixture-secret",
        clock=lambda: clock[0],
        registry=gateway.registry,
        family_transport=lambda *a: pytest.fail("replayed"),
    )
    assert restarted.run(body("timeout"))["state"] == "unknown"
    with sqlite3.connect(gateway.db) as con:
        observation = restarted._quota_observations(con, item, clock[0])[0]
    assert observation["effective_remaining"] == 100
    assert observation["local_observation_hold"] == 100


def test_price_expiry_after_reserve_cannot_use_old_budget_hold(tmp_path):
    gateway, item, _calls, clock, _access = setup_credit(tmp_path)
    item = replace(
        item,
        resource_estimates=(
            replace(item.resource_estimates[0], expires_at=NOW + timedelta(seconds=5)),
        ),
    )
    gateway.targets = gateway.broker.targets = (item,)
    reservation = gateway.broker.reserve(
        {
            "request_key": "price-race",
            "capability": "text_generation",
            "input_token_bound": 100,
            "max_output_tokens": 32,
            "resource_bounds": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 32,
                "total_tokens": 132,
            },
            "input_bytes": 100,
        }
    )
    clock[0] += timedelta(seconds=6)
    with pytest.raises(BrokerError, match="estimate expired"):
        gateway.broker.dispatch(reservation["reservation_id"])


def test_cost_envelope_rejects_oversized_input_before_io(tmp_path):
    gateway, _item, calls, _clock, _access = setup_credit(tmp_path)
    oversized = body("oversized")
    oversized["input"]["messages"][0]["content"] = "x" * 4097
    assert gateway.explain(oversized)["selected_target_id"] is None
    assert calls == []


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [{"metric": METRIC, "window": "unknown"}],
        [{"metric": "missing", "window": "budget"}],
        [{"metric": METRIC, "window": "budget"}, {"metric": METRIC, "window": "budget"}],
    ],
)
def test_invalid_capacity_dimensions_rejected(raw):
    from quota_broker.config import ConfigError, parse_required_capacity

    with pytest.raises(ConfigError):
        parse_required_capacity(raw)
