import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from quota_broker.core import stamp
from quota_broker.discovery import DiscoveryError, DiscoveryStore
from quota_broker.discovery_sources import (
    candidate_bundle,
    fetch_public_snapshot,
    validate_attestation,
    validate_snapshot,
)
from quota_broker.registry import Registry

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def model(**changes):
    return {
        "model": "test/new-model",
        "capability": "text_generation",
        "hosting": "hosted",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "protocol": "openai_chat",
        "free_eligibility": "free",
        "free_source": "https://openrouter.ai/api/v1/models",
        "status": "listed",
        "context_tokens": 8192,
        "max_output_tokens": 1024,
        "features": ["text", "text_generation"],
        **changes,
    }


def snapshot(**changes):
    return {
        "schema_version": 1,
        "provider": "openrouter",
        "source": "https://openrouter.ai/api/v1/models",
        "checked_at": stamp(NOW),
        "complete": False,
        "models": [model()],
        **changes,
    }


def attest(**changes):
    return {
        "provider": "openrouter",
        "model": "test/new-model",
        "capability": "text_generation",
        "account_scope": "account-opaque",
        "account_availability": "allowed",
        "account_checked_at": stamp(NOW),
        "account_valid_until": stamp(NOW + timedelta(minutes=5)),
        "live_result": "unverified",
        "live_checked_at": None,
        "receipt_id": None,
        **changes,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://openrouter.ai/api/v1/models",
        "https://openrouter.ai.evil.invalid/models",
        "https://user:password@openrouter.ai/api/v1/models",
        "https://openrouter.ai:443/api/v1/models",
        "https://openrouter.ai/api/v1/models?key=private",
        "https://openrouter.ai/api/v1/models#secret",
        "https://openrouter.ai/../models",
        "https://openrouter.ai/%2e%2e/models",
    ],
)
def test_sources_reject_urls_that_can_leak_or_change_destination(url):
    with pytest.raises(DiscoveryError):
        validate_snapshot(snapshot(source=url), NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"token": "private"},
        {"account_availability": "allowed"},
        {"complete": "true"},
        {"checked_at": stamp(NOW + timedelta(seconds=1))},
        {"checked_at": "2026-10-03T00:00:00"},
        {"provider": "arbitrary"},
        {"schema_version": True},
        {"complete": True, "models": []},
    ],
)
def test_snapshot_cannot_attest_an_account_or_accept_an_untrusted_schema(change):
    with pytest.raises(DiscoveryError):
        validate_snapshot(snapshot(**change), NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "gsk_abcdefghijklmnop"},
        {"model": "test\nsecret"},
        {"free_eligibility": "free", "free_source": None},
        {"status": "active"},
        {"protocol": "download:execute/secret"},
        {"context_tokens": True},
        {"max_output_tokens": 8193},
        {"features": ["arbitrary free text"]},
        {"endpoint": "https://example.invalid/chat"},
        {"endpoint": "https://openrouter.ai/api/v1/chat?key=secret"},
    ],
)
def test_model_metadata_is_bounded_and_requires_explicit_free_evidence(change):
    with pytest.raises(DiscoveryError):
        validate_snapshot(snapshot(models=[model(**change)]), NOW)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "type"),
        ("   ", "empty"),
        ("x" * 257, "length"),
        ("name\x7f", "characters"),
        ("https://example.invalid/model", "url"),
        ("gsk_abcdefghijklmnop", "secret_pattern"),
        ("AIzaabcdefghijklmnop", "secret_pattern"),
        ("-----BEGIN PRIVATE KEY-----", "secret_pattern"),
    ],
)
def test_model_identity_failures_have_content_free_reasons(value, reason):
    with pytest.raises(DiscoveryError) as exc:
        validate_snapshot(snapshot(models=[model(model=value)]), NOW)
    assert exc.value.phase == "validate_evidence"
    assert exc.value.reason == "model_id_" + reason


def test_opaque_model_identity_does_not_broaden_account_or_receipt_identity():
    name = 'vendor/Model "preview" (中文) %?v=1..2'
    assert (
        validate_snapshot(snapshot(models=[model(model=name)]), NOW)["models"][0]["model"] == name
    )
    assert validate_attestation(attest(model=name), NOW)["model"] == name
    with pytest.raises(DiscoveryError):
        validate_attestation(attest(account_scope=name), NOW)
    with pytest.raises(DiscoveryError):
        validate_attestation(
            attest(live_result="passed", live_checked_at=stamp(NOW), receipt_id=name), NOW
        )


def test_one_model_can_have_separate_operation_families():
    normalized = validate_snapshot(
        snapshot(models=[model(), model(capability="embedding", protocol=None)]), NOW
    )
    assert len(normalized["models"]) == 2
    with pytest.raises(DiscoveryError):
        validate_snapshot(snapshot(models=[model(), model()]), NOW)
    assert validate_snapshot(snapshot(models=[]), NOW)["models"] == []
    assert validate_snapshot(
        snapshot(source="https://openrouter.ai/api/v1/models?output_modalities=all"), NOW
    )


@pytest.mark.parametrize(
    "change",
    [
        {"secret_ref": "private"},
        {"account_scope": "Bearer private"},
        {"account_availability": "ready"},
        {"account_valid_until": stamp(NOW)},
        {"account_valid_until": stamp(NOW + timedelta(days=31))},
        {"live_result": "passed"},
        {"live_checked_at": stamp(NOW)},
        {"live_result": "passed", "live_checked_at": stamp(NOW), "receipt_id": None},
    ],
)
def test_attestation_requires_separate_time_scope_and_receipt(change):
    with pytest.raises(DiscoveryError):
        validate_attestation(attest(**change), NOW)


def test_public_refresh_candidate_is_disabled_and_never_copies_existing_bindings(tmp_path):
    registry = Registry.builtin()
    store = DiscoveryStore(tmp_path / "catalog.sqlite", registry, clock=lambda: NOW)
    before = dict(registry.models)
    store.refresh(snapshot())
    bundle = candidate_bundle(store.coverage()["records"], registry)
    assert not bundle["activation_allowed"]
    assert registry.models == before
    candidates = bundle["target_candidates"]
    target = next(c for c in candidates if c["model"] == "test/new-model")
    assert target["disabled"] and not target["free_eligible"] and not target["billing_enabled"]
    assert "account_access_unconfirmed" in target["blocking_reasons"]
    assert "secret_ref" not in target and "account_scope" not in target and "quotas" not in target
    registered = Registry.from_manifest(bundle["registry_manifest"])
    assert registered.resolve("test/new-model", "openrouter").adapter == "openai_chat"
    with sqlite3.connect(store.db) as con:
        assert con.execute("select count(*) from discovery_accounts").fetchone()[0] == 0


def test_unknown_paid_retired_and_unsupported_stay_explicit_disabled_gaps(tmp_path):
    store = DiscoveryStore(tmp_path / "catalog.sqlite", Registry.builtin(), clock=lambda: NOW)
    store.refresh(
        snapshot(
            models=[
                model(free_eligibility="paid"),
                model(model="future", capability="science", protocol=None),
            ]
        )
    )
    bundle = candidate_bundle(store.coverage(provider="openrouter")["records"], store.registry)
    assert all(c["disabled"] for c in bundle["target_candidates"])
    gap = next(x for x in bundle["gaps"] if x["model"] == "future")
    assert "registry_contract_incomplete" in gap["reasons"]


def curl_output(body, *, status=200, code=0):
    from quota_broker.bounded_curl import MARKER, TIMINGS

    metrics = {"http_code": str(status), "exitcode": code, **dict.fromkeys(TIMINGS, 0.01)}
    return (
        f"HTTP/2 {status}\r\nContent-Type: application/json\r\n\r\n".encode()
        + body
        + MARKER
        + json.dumps(metrics).encode()
    )


def test_public_fetch_is_fixed_anonymous_bounded_and_drops_raw_metadata(monkeypatch):
    from quota_broker import discovery_parsers as parsers
    from quota_broker import discovery_sources as sources

    calls = []
    raw = {"data": [], "description": "discard this raw text"}

    def run(config, timeout, **limits):
        calls.append((config.decode(), timeout, limits))
        return 0, curl_output(json.dumps(raw).encode())

    monkeypatch.setattr(sources, "run_curl", run)
    monkeypatch.setattr(parsers, "parse_models", lambda *_args, **_kwargs: snapshot())
    result = fetch_public_snapshot("openrouter", output_modalities="all", clock=lambda: NOW)
    assert len(calls) == 1 and calls[0][1] == 20
    config = calls[0][0]
    assert 'request = "GET"' in config and '?output_modalities=all"' in config
    assert 'retry = "0"' in config and 'max-redirs = "0"' in config
    assert "Authorization" not in config and "data-binary" not in config
    assert calls[0][2]["deadline_grace"] == 0
    assert "discard" not in json.dumps(result)
    with pytest.raises(DiscoveryError):
        fetch_public_snapshot("groq")
    assert len(calls) == 1


@pytest.mark.parametrize("scenario", ["redirect", "oversize", "invalid_json", "timeout"])
def test_public_fetch_failure_never_imports_or_prints_raw_error(monkeypatch, scenario, capsys):
    from quota_broker import discovery_sources as sources

    def run(*_args, **_kwargs):
        body = b"private-provider-body" if scenario == "invalid_json" else b"x" * 33
        status = 302 if scenario == "redirect" else 200
        code = 28 if scenario == "timeout" else 0
        return code, curl_output(body, status=status, code=code)

    monkeypatch.setattr(sources, "run_curl", run)
    monkeypatch.setattr(sources, "MAX_SNAPSHOT_BYTES", 32)
    with pytest.raises(DiscoveryError) as exc:
        fetch_public_snapshot("nvidia")
    assert "private-provider-body" not in str(exc.value)
    assert capsys.readouterr().out == ""


def test_delayed_chunked_body_hits_the_total_deadline_and_cannot_be_imported(monkeypatch):
    import io
    from types import SimpleNamespace

    from quota_broker import bounded_curl as curl

    class Child:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.killed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, **_kwargs):
            return 0

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def register(self, *_args):
            pass

        def get_map(self):
            return {1: True}

        def select(self, timeout):
            return [(SimpleNamespace(fd=1), 1)]

    child = Child()
    times = iter((0, 19.99, 20.01))
    monkeypatch.setattr(curl.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(curl.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(curl.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(curl.os, "read", lambda *_args: b'HTTP/2 200\r\n\r\n{"data":[')
    with pytest.raises(DiscoveryError):
        fetch_public_snapshot("nvidia")
    assert child.killed
