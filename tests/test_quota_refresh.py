"""Quota refreshes must not turn incomplete knowledge into spend authority."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.dashboard_provider_metadata import quota_projection, validate_report

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("quota_refresh", ROOT / "scripts/quota_refresh.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)
BASE = {
    "schema_version": 1,
    "kind": "provider_metadata_observations",
    "models": [],
    "openrouter": None,
}


def test_collect_reports_all_providers_and_never_infers_credit_from_key_limit():
    calls = []

    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return json.dumps(
            {
                "data": {
                    "limit_remaining": 99,
                    "free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0},
                }
            }
        ).encode()

    result = refresh.collect(
        BASE, resolver=lambda name: "fixture-value", get=get, clock=lambda: NOW
    )
    assert len(calls) == 1 and calls[0][0] == refresh.KEY_URL
    assert len(result["refresh"]["providers"]) == 11
    assert result["openrouter"]["daily"]["remaining"] == 0
    assert "99" not in json.dumps(result)
    assert "fixture-value" not in json.dumps(result)
    validate_report(json.dumps(result).encode(), NOW)


def test_failed_refresh_preserves_original_observation_time():
    base = {
        **BASE,
        "openrouter": {
            "source": refresh.KEY_URL,
            "checked_at": (NOW - timedelta(days=1)).isoformat(),
            "daily": {"used": 3, "limit": 50, "remaining": 47},
        },
    }

    def fail(*args):
        raise RuntimeError("private value must not leak")

    result = refresh.collect(base, resolver=fail, get=fail, clock=lambda: NOW)
    assert result["openrouter"] == base["openrouter"]
    assert quota_projection(result, NOW)["daily"] is None
    assert result["refresh"]["providers"]["openrouter"]["status"] == "credential_unavailable"
    assert "private value" not in json.dumps(result)


def test_crossing_midnight_does_not_publish_wrong_day_remaining():
    ticks = iter([NOW.replace(hour=23, minute=59, second=59), NOW + timedelta(days=1, hours=-10)])
    result = refresh.collect(
        BASE,
        resolver=lambda name: "fixture",
        get=lambda *args: (
            b'{"data":{"free_model_daily_requests":{"used":0,"limit":50,"remaining":50}}}'
        ),
        clock=lambda: next(ticks),
    )
    assert result["openrouter"]["daily"] is None


def test_metadata_rejects_injected_refresh_urls_and_future_dates():
    report = refresh.collect(
        BASE, resolver=lambda name: "fixture", get=lambda *args: b'{"data":{}}', clock=lambda: NOW
    )
    report["refresh"]["providers"]["openrouter"]["url"] = "https://evil.invalid/"
    with pytest.raises(ValueError):
        validate_report(json.dumps(report).encode(), NOW)


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"data":{},"data":{}}',
        b"not-json",
        b'{"data":{"free_model_daily_requests":{"used":0,"limit":5,"remaining":6}}}',
    ],
)
def test_malformed_response_cannot_publish_new_evidence(raw):
    result = refresh.collect(
        BASE, resolver=lambda name: "fixture", get=lambda *args: raw, clock=lambda: NOW
    )
    assert result["openrouter"] is None
    assert result["refresh"]["providers"]["openrouter"]["status"] == "query_failed"


def test_data_only_package_keeps_app_and_rejects_qualification_changes(tmp_path):
    import base64
    import uuid

    import hp_quota_client as publisher

    folder = tmp_path / "metadata"
    folder.mkdir(mode=0o700)
    files = []
    for name in publisher.PATHS:
        raw = b"unchanged fixture"
        if name.endswith("dashboard_provider_metadata.py"):
            raw = b"REFRESH_SCHEMA_VERSION = 2"
        if name == "metadata/accounts.json":
            raw = b'{"schema_version":1,"accounts":[]}'
        if name == "metadata/provider-observations.json":
            raw = json.dumps(BASE).encode()
        files.append(
            {
                "path": name,
                "size": len(raw),
                "sha256": publisher.digest(raw),
                "content_base64": base64.b64encode(raw).decode(),
            }
        )
        if name.startswith("metadata/"):
            p = folder / name.split("/")[1]
            p.write_bytes(raw)
            p.chmod(0o600)
    base = {
        "schema": "hp-quota-release-v1",
        "operation_id": str(uuid.uuid4()),
        "release_id": "quota-approved",
        "expected_current": "older",
        "expected_manifest_sha256": "0" * 64,
        "files": files,
    }
    (folder / "provider-observations.json").write_text(
        json.dumps(
            refresh.collect(
                BASE,
                resolver=lambda _: "fixture",
                get=lambda *args: (
                    b'{"data":{"free_model_daily_requests":{"used":0,"limit":50,"remaining":50}}}'
                ),
                clock=lambda: NOW - timedelta(days=1),
            )
        )
    )
    result = refresh.data_package(base, folder, str(uuid.uuid4()))
    assert [f for f in result["files"] if f["path"].startswith("app/")] == [
        f for f in files if f["path"].startswith("app/")
    ]
    assert result["expected_manifest_sha256"] == publisher.validate(base)["manifest_sha256"]
    # A second generation cannot restore an older observation or alter a value
    # at the same instant using a different UTC spelling.
    latest = json.loads((folder / "provider-observations.json").read_text())
    changed = json.loads(json.dumps(latest))
    changed["openrouter"]["checked_at"] = changed["openrouter"]["checked_at"].replace("+00:00", "Z")
    changed["openrouter"]["daily"] = {"used": 1, "limit": 50, "remaining": 49}
    (folder / "provider-observations.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="counter_evidence_regressed"):
        refresh.data_package(result, folder, str(uuid.uuid4()))
    (folder / "provider-observations.json").write_text(json.dumps(BASE))
    with pytest.raises(ValueError, match="counter_evidence_regressed"):
        refresh.data_package(result, folder, str(uuid.uuid4()))
    (folder / "provider-observations.json").write_text(json.dumps(latest))
    old_app = json.loads(json.dumps(base))
    reader = next(
        f for f in old_app["files"] if f["path"].endswith("dashboard_provider_metadata.py")
    )
    raw = b"legacy reader"
    reader.update(
        size=len(raw), sha256=publisher.digest(raw), content_base64=base64.b64encode(raw).decode()
    )
    with pytest.raises(ValueError, match="active_app_requires_reviewed_upgrade"):
        refresh.data_package(old_app, folder, str(uuid.uuid4()))
    (folder / "gateway.json").write_bytes(b"changed qualification")
    with pytest.raises(ValueError, match="qualification_or_configuration_changed"):
        refresh.data_package(base, folder, str(uuid.uuid4()))


def test_missing_new_counter_preserves_history_but_suppresses_current():
    base = {
        **BASE,
        "openrouter": {
            "source": refresh.KEY_URL,
            "checked_at": (NOW - timedelta(minutes=1)).isoformat(),
            "daily": {"used": 0, "limit": 50, "remaining": 50},
        },
    }
    report = refresh.collect(
        base, resolver=lambda _: "fixture", get=lambda *args: b'{"data":{}}', clock=lambda: NOW
    )
    projected = quota_projection(report, NOW)
    assert report["openrouter"] == base["openrouter"]
    assert projected["daily"] is None
    assert projected["last_observed_daily"]["remaining"] == 50
    assert projected["checked_at"] == base["openrouter"]["checked_at"]


def test_inconsistent_success_status_is_rejected():
    report = refresh.collect(
        BASE, resolver=lambda _: "fixture", get=lambda *args: b'{"data":{}}', clock=lambda: NOW
    )
    report["refresh"]["providers"]["openrouter"]["status"] = "updated"
    with pytest.raises(ValueError, match="updated_counter_missing"):
        validate_report(json.dumps(report).encode(), NOW)


def test_probe_all_records_all_11_providers_and_openrouter_usage():
    from quota_broker.dashboard_provider_metadata import refresh_projection

    def fake_resolver(name):
        if name in ("CEREBRAS_API_KEY", "SAMBANOVA_API_KEY"):
            raise RuntimeError("credential_unavailable")
        if name == "CLOUDFLARE_ACCOUNT_ID":
            return "0123456789abcdef0123456789abcdef"
        return f"secret-for-{name}"

    def fake_get(url, headers, timeout):
        if url == refresh.KEY_URL:
            return (
                b'{"data":{"is_free_tier":true,"usage":0,"usage_daily":0,"usage_weekly":0,'
                b'"usage_monthly":0,"byok_usage":0,"byok_usage_daily":0,"byok_usage_weekly":0,'
                b'"byok_usage_monthly":0,"free_model_daily_requests":{"used":0,"limit":50,"remaining":50}}}'
            )
        if "generativelanguage.googleapis.com" in url:
            return b'{"models":[{"name":"models/gemini-2.5-flash","supportedGenerationMethods":["generateContent"],"inputTokenLimit":1048576,"outputTokenLimit":65536}]}'
        if "cloudflare.com" in url:
            return b'{"success":true,"result":[{"name":"@cf/meta/llama-3.2-1b-instruct","task":{"name":"Text Generation"}}],"result_info":{"page":1,"per_page":100,"count":1,"total_count":1}}'
        if "openrouter.ai/api/v1/models" in url:
            return b'{"data":[{"id":"openrouter/free","context_length":4096,"architecture":{"input_modalities":["text"],"output_modalities":["text"]},"supported_parameters":[]}]}'
        return b'{"data":[{"id":"openai/gpt-oss-20b"},{"id":"ministral-3b-latest"},{"id":"nvidia/riva-translate-4b-instruct-v2"}]}'

    def fake_http_probe(url, headers, timeout=15, *, method="GET", body=None):
        if url == refresh.PROBE_URLS["openai_models"]:
            return 200, json.dumps({"data": [{"id": f"m-{i}"} for i in range(96)]}).encode()
        if url == refresh.PROBE_URLS["openai_costs"]:
            return 403, b'{"error":{"message":"forbidden"}}'
        if url == refresh.PROBE_URLS["cloudflare_graphql"]:
            return 200, b'{"errors":[{"message":"not authorized"}]}'
        if url == refresh.PROBE_URLS["huggingface_whoami"]:
            return 200, b'{"type":"user","isPro":false}'
        return 404, b"{}"

    report = refresh.collect(
        BASE,
        resolver=fake_resolver,
        get=fake_get,
        http_probe=fake_http_probe,
        clock=lambda: NOW,
        probe_all=True,
    )
    validated = validate_report(json.dumps(report).encode(), NOW)
    projected_quota = quota_projection(validated, NOW)
    projected_refresh = refresh_projection(validated)
    assert projected_quota["usage"]["total_usd"] == "0"
    assert projected_quota["usage"]["is_free_tier"] is True
    assert set(projected_refresh["providers"]) == set(refresh.REFRESH_SOURCES)
    assert projected_refresh["providers"]["openai"]["verified_models"] == 96
    assert projected_refresh["providers"]["openai"]["queried"] is True
    # Listing models cannot establish a ChatGPT subscription or credit grant.
    assert "account_plan" not in projected_refresh["providers"]["openai"]
    assert projected_refresh["providers"]["huggingface"]["account_plan"] == "free"
    assert projected_refresh["providers"]["cerebras"]["status"] == "credential_unavailable"
    assert projected_refresh["providers"]["sambanova"]["status"] == "credential_unavailable"
