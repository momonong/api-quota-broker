"""The plan cannot execute; explicit sanitized local fixtures use real policy."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quota_broker.discovery_sources import validate_snapshot

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "provider_metadata_plan.py"
SPEC = importlib.util.spec_from_file_location("provider_metadata_plan", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def write_fixture(tmp_path, raw):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_fixed_plan_bounds_and_no_execution_or_secret_resolution(monkeypatch, capsys):
    def prohibited(*args, **kwargs):
        raise AssertionError("network or credential access attempted")

    import os
    import socket
    import subprocess

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(os, "getenv", prohibited)
    monkeypatch.setattr(Path, "open", prohibited)
    assert plan.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry_run" and result["execution_authorized"] is False
    assert result["max_requests"] == 15 and result["max_wall_seconds"] == 240
    assert result["theoretical_page_ceiling_seconds"] == 300
    assert result["page_cap_can_end_partial"] is True
    requests = [request for provider in result["providers"] for request in provider["requests"]]
    assert sum(request["max_requests"] for request in requests) == 15
    assert sum(request["max_wall_seconds"] for request in requests) == 300
    assert all(
        request["method"] == "GET" and 1 <= request["max_pages"] <= 5 for request in requests
    )
    transport = result["future_transport_contract"]
    assert transport["per_request_total_seconds"] == 20 and transport["deadline_grace"] == 0
    for field in ("follow_redirects", "custom_user_agent", "custom_ip", "custom_proxy"):
        assert transport[field] is False
    assert transport["retry_count"] == 0
    credentials = result["future_credential_steps"]
    assert credentials["project"] == "api-quota-broker" and credentials["config"] == "dev"
    assert credentials["token_count"] == 1 and credentials["token_ttl_seconds"] == 300
    assert credentials["minimum_remaining_seconds_before_batch"] == 270
    assert credentials["renew"] is False and credentials["executed_by_this_tool"] is False
    assert credentials["persist_secrets"] is False and credentials["output_account_id"] is False
    assert set(result["declared_credential_refs"]) == set(plan.PROVIDERS)
    assert all(
        set(request["authentication"]) == {"credential_ref", "header", "scheme"}
        for request in requests
        if request["authentication"] is not None
    )


def test_exact_official_urls_pagination_and_ocr_gap():
    providers = {provider["provider"]: provider for provider in plan.build_plan()["providers"]}
    assert providers["nvidia"]["requests"][0]["url"] == "https://integrate.api.nvidia.com/v1/models"
    assert providers["nvidia"]["requests"][0]["authentication"] is None
    google = providers["google"]["requests"][0]
    assert google["url"] == "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
    assert google["pagination"]["request_parameter"] == "pageToken"
    assert google["pagination"]["token_storage"] == "memory_only"
    assert google["pagination"]["tokens_in_output"] is False
    cf = providers["cloudflare"]["requests"][0]
    assert (
        cf["url"]
        == "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search?page=1&per_page=100&include_deprecated=true"
    )
    assert cf["account_id_ref"] == "CLOUDFLARE_ACCOUNT_ID"
    assert cf["pagination"]["server_max_per_page"] is None
    assert cf["pagination"]["complete_requires_exhaustion_evidence"] is True
    router = providers["openrouter"]["requests"]
    assert router[0]["url"] == "https://openrouter.ai/api/v1/models?output_modalities=all"
    assert router[1]["url"] == "https://openrouter.ai/api/v1/key"
    assert router[1]["authentication"]["credential_ref"] == "OPENROUTER_API_KEY"
    assert providers["ocrspace"]["requests"] == []
    assert providers["ocrspace"]["gap"] == "docs_only_no_approved_get_model_or_account_metadata_api"


@pytest.mark.parametrize(
    "option", ["--live", "--url", "--account-id", "--api-key", "--proxy", "--execute"]
)
def test_no_live_or_secret_or_custom_destination_cli_option(option, capsys):
    with pytest.raises(SystemExit) as caught:
        plan.main([option, "private-raw-value"])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert "private-raw-value" not in output.out + output.err
    assert json.loads(output.out)["reason"] == "options"


@pytest.mark.parametrize(
    "provider,raw",
    [
        ("nvidia", {"data": [{"id": "vendor/model"}]}),
        ("groq", {"data": [{"id": "vendor/model", "active": True}]}),
        ("mistral", {"data": [{"id": "vendor/model", "capabilities": {"completion_chat": True}}]}),
        (
            "google",
            {
                "models": [
                    {"name": "models/fixture", "supportedGenerationMethods": ["generateContent"]}
                ]
            },
        ),
        (
            "cloudflare",
            {
                "success": True,
                "result": [{"name": "@cf/vendor/model", "task": {"name": "Text Generation"}}],
            },
        ),
        (
            "openrouter",
            {
                "data": [
                    {
                        "id": "vendor/model",
                        "architecture": {"output_modalities": ["text"]},
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            },
        ),
        ("ocrspace", {}),
    ],
)
def test_model_fixture_all_seven_pass_production_policy(provider, raw, tmp_path, capsys):
    path = write_fixture(tmp_path, raw)
    assert plan.main(["--fixture", provider, str(path), "--checked-at", NOW.isoformat()]) == 0
    result = json.loads(capsys.readouterr().out)
    assert validate_snapshot(result, NOW) == result
    assert result["provider"] == provider


def test_fixture_policy_gate_is_called_and_failure_is_content_free(tmp_path, monkeypatch, capsys):
    from quota_broker.discovery import DiscoveryError

    def reject(*args):
        raise DiscoveryError("invalid_request", "private policy data")

    monkeypatch.setattr(plan, "validate_snapshot", reject)
    path = write_fixture(tmp_path, {"data": [{"id": "valid"}]})
    assert plan.main(["--fixture", "nvidia", str(path)]) == 2
    result = capsys.readouterr()
    assert "private" not in result.out + result.err
    assert json.loads(result.out)["reason"] == "snapshot_policy"


@pytest.mark.parametrize(
    "raw",
    [
        '{"data": [], "data": [{"id": "x"}]}',
        '{"data": [{"id": "x", "description": "Bearer synthetic-secret"}]}',
        '{"data": [{"id": "x", "description": "\\u0042earer synthetic-secret"}]}',
        '{"data": [{"id": "x"}], "api_key": "synthetic-no-prefix"}',
        '{"data": [], "value": "-----BEGIN PRIVATE KEY-----"}',
        '{"data": [], "value": NaN}',
        '{"data": ["private-invalid-body"]}',
        "{",
    ],
)
def test_raw_fixture_and_secret_patterns_never_reach_output(raw, tmp_path, capsys):
    path = tmp_path / "fixture.json"
    path.write_text(raw, encoding="utf-8")
    assert plan.main(["--fixture", "nvidia", str(path)]) == 2
    result = capsys.readouterr()
    assert "synthetic" not in result.out + result.err
    assert "private-invalid-body" not in result.out + result.err
    assert json.loads(result.out)["error"] == "invalid_request"


def test_fixture_file_bound_symlink_and_sensitive_paths(tmp_path, capsys):
    path = tmp_path / "fixture.json"
    path.write_bytes(b"x" * (plan.MAX_SNAPSHOT_BYTES + 1))
    assert plan.main(["--fixture", "nvidia", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "fixture_size"
    link = tmp_path / "symlink.json"
    link.symlink_to(path)
    assert plan.main(["--fixture", "nvidia", str(link)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "fixture_path"
    assert plan.main(["--fixture", "nvidia", str(tmp_path / "credentials.json")]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "fixture_path"


def key_fixture(daily=None):
    return {
        "data": {
            "label": "discard-label",
            "creator_user_id": "discard-account-id",
            "is_free_tier": False,
            "usage": 100.5,
            "limit": 200,
            "limit_remaining": 99.5,
            **({} if daily is None else {"free_model_daily_requests": daily}),
        }
    }


def test_openrouter_key_daily_requests_are_separate_from_credit_values(tmp_path, capsys):
    daily = {"used": 12, "limit": 50, "remaining": 38}
    path = write_fixture(tmp_path, key_fixture(daily))
    assert (
        plan.main(
            [
                "--fixture",
                "openrouter",
                str(path),
                "--fixture-kind",
                "key",
                "--checked-at",
                NOW.isoformat(),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "https://openrouter.ai/api/v1/key"
    assert result["free_model_daily_requests"] == daily
    assert result["credits"]["limit"] == "200" and result["credits"]["limit_remaining"] == "99.5"
    assert result["credits"]["usage"] == "100.5"
    assert result["account_availability"] == "unknown" and result["live_result"] == "unknown"
    assert "discard-" not in json.dumps(result) and "is_free_tier" not in result


def test_key_missing_daily_counter_never_uses_credits_or_free_tier_as_request_count():
    result = plan.parse_openrouter_key(key_fixture(), NOW)
    assert result["free_model_daily_requests"] is None
    # Exempt accounts may record more requests than their reported tier ceiling.
    result = plan.parse_openrouter_key(key_fixture({"used": 60, "limit": 50, "remaining": 0}), NOW)
    assert result["free_model_daily_requests"]["used"] == 60


@pytest.mark.parametrize(
    "daily",
    [
        {"used": 12, "limit": 50, "remaining": 99},
        {"used": True, "limit": 50, "remaining": 49},
        {"used": -1, "limit": 50, "remaining": 51},
        {"used": 12, "limit": 50},
        {"used": "12", "limit": 50, "remaining": 38},
        {"used": 12.0, "limit": 50, "remaining": 38},
        {"used": 12, "limit": 50, "remaining": 38, "private": "discard"},
    ],
)
def test_key_daily_counter_strict_consistency_and_integer_schema(daily):
    with pytest.raises(plan.PlanError) as caught:
        plan.parse_openrouter_key(key_fixture(daily), NOW)
    assert caught.value.reason == "quota_counter"


@pytest.mark.parametrize("value", ["NaN", "Infinity", -1, True, {}, "private-raw-value"])
def test_key_credit_metadata_is_finite_and_nonnegative(value):
    raw = key_fixture()
    raw["data"]["limit_remaining"] = value
    with pytest.raises(plan.PlanError) as caught:
        plan.parse_openrouter_key(raw, NOW)
    assert caught.value.reason == "credits" and "private" not in str(caught.value)


def test_safe_options_and_aware_fixture_time(tmp_path, capsys):
    path = write_fixture(tmp_path, {})
    for args in (
        ["--fixture", "unknown", str(path)],
        ["--fixture", "groq", str(path), "--fixture-kind", "key"],
        ["--checked-at", NOW.isoformat()],
        ["--fixture", "ocrspace", str(path), "--checked-at", "not-a-time"],
        ["--fixture", "ocrspace", str(path), "--checked-at", "2026-10-03T00:00:00"],
    ):
        assert plan.main(args) == 2
        assert json.loads(capsys.readouterr().out)["error"] == "invalid_request"
