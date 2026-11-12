"""Account refresh imports counters, never infers balances or resets unknown holds."""

import json
import threading
from dataclasses import replace

import pytest
from test_hp_shared_runtime import module
from test_shared_credit_admission import setup_credit


def fixture(tmp_path):
    runtime = module()
    gateway, target, _calls, clock, _access = setup_credit(tmp_path)
    gateway.targets = (replace(target, provider="openrouter"),)
    observations = []
    gateway.observe_quota = lambda target, value: observations.append(value)
    response = {
        "data": {
            "is_free_tier": True,
            "limit_remaining": None,
            "free_model_daily_requests": {"used": 7, "limit": 50, "remaining": 43},
        }
    }
    return runtime, gateway, clock[0], response, observations


def test_refresh_uses_reported_remaining_and_original_reset_boundary(tmp_path):
    runtime, gateway, now, response, observations = fixture(tmp_path)
    runtime.refresh_openrouter(gateway, lambda _: "fixture-key", fetch=lambda _: response, now=now)
    assert observations[0]["remaining"] == 43
    assert observations[0]["source"] == "adapter:openrouter_key_account"
    assert observations[0]["as_of"] == now.isoformat()


@pytest.mark.parametrize(
    "bad", [None, {"used": 0, "limit": 50}, {"used": 7, "limit": 50, "remaining": 50}]
)
def test_counter_gap_never_becomes_full_capacity(tmp_path, bad):
    runtime, gateway, now, response, observations = fixture(tmp_path)
    response["data"]["free_model_daily_requests"] = bad
    with pytest.raises(ValueError):
        runtime.refresh_openrouter(
            gateway, lambda _: "fixture-key", fetch=lambda _: response, now=now
        )
    assert observations == []


def test_failed_refresh_stops_and_restart_does_not_retry(tmp_path, monkeypatch):
    runtime = module()
    calls = []

    def failure(*_):
        calls.append(1)
        raise TimeoutError("private upstream detail")

    monkeypatch.setattr(runtime, "refresh_openrouter", failure)
    for _ in range(2):
        runtime.refresh_loop(None, None, threading.Event(), tmp_path)
    assert calls == [1]
    marker = tmp_path / "metadata-refresh-stopped.json"
    assert json.loads(marker.read_text())["automatic_retry"] is False
    assert "private" not in marker.read_text()
