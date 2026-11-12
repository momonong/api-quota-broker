"""Official live account counters remain separate from local holds and old costs."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from test_dashboard import dashboard
from test_gateway import NOW

URL = "https://openrouter.ai/api/v1/key"


def setup_board(tmp_path):
    board, source = dashboard(tmp_path)
    source.kind = "gateway"
    target = {
        **source.catalog()[0],
        "target_id": "or-live",
        "provider": "openrouter",
        "model": "fixture:free",
        "shared_concurrency_scope": "openrouter:fixture-account",
    }
    observation = {
        "metric": "requests",
        "window": "day",
        "remaining": 49,
        "limit": 50,
        "effective_remaining": 48,
        "local_observation_hold": 1,
        "as_of": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=15)).isoformat(),
        "reset_at": (NOW + timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat(),
        "provenance": "official",
        "confidence": "provider_reported",
        "source": "adapter:openrouter_key_account",
    }
    diagnostic = {
        **source.diagnostics()["targets"][0],
        "target_id": "or-live",
        "provider": "openrouter",
        "model": "fixture:free",
        "quota_observations": [observation],
    }
    catalog, diagnostics = [target], {"or-live": diagnostic}
    source.catalog = lambda: deepcopy(catalog)
    source.diagnostics = lambda: {
        "as_of": NOW.isoformat(),
        "targets": deepcopy(list(diagnostics.values())),
    }
    historical = NOW - timedelta(hours=1)
    report = {
        "schema_version": 1,
        "kind": "provider_metadata_observations",
        "models": [],
        "openrouter": {
            "source": URL,
            "checked_at": historical.isoformat(),
            "daily": {"used": 0, "limit": 50, "remaining": 50},
            "usage": {"total_usd": "1.23", "daily_usd": "0.25"},
        },
    }
    file = tmp_path / "provider-observations.json"
    file.write_text(json.dumps(report))
    file.chmod(0o600)
    board.provider_metadata_file = file
    return board, observation, catalog, diagnostics, historical


def test_live_counter_replaces_stale_kpi_and_usage_counter_without_counting_holds(tmp_path):
    board, _observation, _catalog, _diagnostics, historical = setup_board(tmp_path)
    snapshot = board.snapshot()
    counter = snapshot["provider_metadata"]["openrouter"]
    assert counter["state"] == "current"
    assert counter["daily"] == {"used": 1, "limit": 50, "remaining": 49}
    assert counter["checked_at"] == NOW.isoformat()
    assert snapshot["targets"][0]["observations"][0]["source"] == URL
    report = board.usage_report({})["provider_usage"]
    provider = next(row for row in report["providers"] if row["provider"] == "openrouter")
    daily = next(row for row in provider["metrics"] if row["metric"] == "free_model_daily_requests")
    assert daily["remaining"] == 49 and daily["used"] == 1 and daily["current"] is True
    assert daily["as_of"] == NOW.isoformat()
    costs = [row for row in provider["metrics"] if row["unit"] == "USD"]
    assert costs
    assert all(row["as_of"] == historical.isoformat() and row["current"] is False for row in costs)


def test_expired_runtime_counter_retains_original_observation_without_refreshing(tmp_path):
    board, observation, _catalog, _diagnostics, _historical = setup_board(tmp_path)
    observation["as_of"] = (NOW - timedelta(minutes=16)).isoformat()
    observation["valid_until"] = (NOW - timedelta(minutes=1)).isoformat()
    counter = board.snapshot()["provider_metadata"]["openrouter"]
    assert counter["state"] == "stale" and counter["daily"] is None
    assert counter["last_observed_daily"]["remaining"] == 49
    assert counter["checked_at"] == observation["as_of"]


@pytest.mark.parametrize(
    "change",
    [
        {"source": "adapter:some_other_source"},
        {"provenance": "observed"},
        {"confidence": "administrator_verified"},
    ],
)
def test_only_fixed_official_adapter_can_replace_file_counter(tmp_path, change):
    board, observation, _catalog, _diagnostics, historical = setup_board(tmp_path)
    observation.update(change)
    counter = board.snapshot()["provider_metadata"]["openrouter"]
    assert counter["checked_at"] == historical.isoformat()
    assert counter["daily"] is None
    if change.get("source"):
        assert board.snapshot()["targets"][0]["observations"][0]["source"] is None


@pytest.mark.parametrize("different_scope", [False, True])
def test_conflicting_latest_or_multiple_account_counters_are_not_combined(
    tmp_path, different_scope
):
    board, observation, catalog, diagnostics, _historical = setup_board(tmp_path)
    catalog.append(
        {
            **catalog[0],
            "target_id": "second",
            "shared_concurrency_scope": "different-account"
            if different_scope
            else catalog[0]["shared_concurrency_scope"],
        }
    )
    diagnostics["second"] = {
        **diagnostics["or-live"],
        "target_id": "second",
        "quota_observations": [{**observation, "remaining": 40}],
    }
    counter = board.snapshot()["provider_metadata"]["openrouter"]
    assert counter["state"] == "unknown" and counter["daily"] is None
    assert counter["last_observed_daily"] is None


def test_latest_consistent_counter_wins_without_extending_freshness(tmp_path):
    board, observation, catalog, diagnostics, _historical = setup_board(tmp_path)
    catalog.append({**catalog[0], "target_id": "second"})
    diagnostics["second"] = {
        **diagnostics["or-live"],
        "target_id": "second",
        "quota_observations": [
            {**observation, "as_of": (NOW - timedelta(minutes=1)).isoformat(), "remaining": 50}
        ],
    }
    counter = board.snapshot()["provider_metadata"]["openrouter"]
    assert counter["daily"]["remaining"] == 49
    assert counter["valid_until"] == observation["valid_until"]
