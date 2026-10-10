"""Provider model evidence is independent of the requested model and private body."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from quota_broker.config import Quota, Target
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import safe_response_diagnostics


def metadata(provider, body, model=None, status=200, private=()):
    return safe_response_diagnostics(
        provider,
        status,
        {},
        json.dumps(body).encode(),
        requested_model=model,
        sensitive_values=private,
    )


@pytest.mark.parametrize(
    "provider,model,field,reported",
    [
        (
            "nvidia",
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "model",
            "nvidia/nemotron-3.5-lightning-30b-a3b",
        ),
        ("groq", "openai/gpt-oss-20b", "model", "openai/gpt-oss-20b"),
        ("mistral", "ministral-3b-latest", "model", "ministral-3b-2410"),
        ("mistral", "ministral-3b-latest", "model", "ministral-3-3b-2512"),
        ("openrouter", "liquid/lfm-2.5-2.6b:free", "model", "liquid/lfm-2.5-2.6b"),
        ("google", "gemini-3.5-flash-lite", "modelVersion", "gemini-3.5-flash-lite-001"),
    ],
)
def test_documented_fields_retain_provider_claim(provider, model, field, reported):
    result = metadata(provider, {field: reported}, model)
    assert result["provider_reported_model"] == reported
    assert result["provider_model_basis"] == "provider_response"
    assert result["provider_model_field"] == field


@pytest.mark.parametrize(
    "provider", ["nvidia", "groq", "mistral", "openrouter", "google", "cloudflare", "ocrspace"]
)
def test_missing_field_never_uses_requested_model(provider):
    result = metadata(provider, {}, "requested-model")
    assert result["provider_reported_model"] is None
    assert result["provider_model_basis"] == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        True,
        1,
        None,
        {},
        [],
        "",
        "arbitrary private prose",
        "https://bad.invalid/model",
        "gsk_abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
        "bad\nmodel",
        "a" * 257,
    ],
)
def test_untrusted_or_secret_shaped_identity_is_not_saved(value):
    result = metadata("groq", {"model": value}, "openai/gpt-oss-20b")
    assert result["provider_reported_model"] is None


def test_known_different_model_remains_the_provider_claim():
    result = metadata("groq", {"model": "openai/gpt-oss-120b"}, "openai/gpt-oss-20b")
    assert result["provider_reported_model"] == "openai/gpt-oss-120b"


def test_unknown_alias_and_secret_or_prompt_reflection_stay_unknown():
    assert (
        metadata("mistral", {"model": "ministral-3b-private-body"}, "ministral-3b-latest")[
            "provider_reported_model"
        ]
        is None
    )
    for private in ("OPENAI/GPT-OSS", "openai/gpt-oss-20b"):
        result = metadata(
            "groq", {"model": "openai/gpt-oss-20b"}, "openai/gpt-oss-20b", private=(private,)
        )
        assert result["provider_model_status"] == "redacted"
        assert private not in json.dumps(result)


def test_error_wrong_field_and_undocumented_fields_are_unknown():
    assert (
        metadata("groq", {"model": "openai/gpt-oss-20b"}, status=429)["provider_reported_model"]
        is None
    )
    assert metadata("google", {"model": "gemini-3.5-flash-lite"})["provider_reported_model"] is None
    assert (
        metadata("cloudflare", {"result": {"model": "@cf/meta/llama-3.2-1b-instruct"}})[
            "provider_reported_model"
        ]
        is None
    )
    assert metadata("ocrspace", {"OCREngine": 2})["provider_reported_model"] is None


def test_duplicate_fields_and_invalid_envelope_are_unknown():
    for raw in (
        b'{"model":"openai/gpt-oss-20b","model":"openai/gpt-oss-120b"}',
        b"[]",
        b"not-json",
    ):
        result = safe_response_diagnostics(
            "groq", 200, {}, raw, requested_model="openai/gpt-oss-20b"
        )
        assert result["provider_reported_model"] is None


def test_normal_gateway_sqlite_restart_records_alias_without_private_content(tmp_path):
    now = datetime(2026, 10, 4, tzinfo=UTC)
    target = Target(
        id="fixture-mistral",
        provider="mistral",
        model="ministral-3b-latest",
        account_id="fixture-account",
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=now,
        expires_at=now + timedelta(hours=1),
        quotas=(Quota("fixture-requests", "requests", 2, "day"),),
        concurrency_limit=1,
        max_output_tokens=64,
        priority=0,
        source="fixture",
        secret_ref="FIXTURE",
    )
    db = tmp_path / "ledger.sqlite"
    calls = []

    def transport(*args):
        calls.append(1)
        return (
            200,
            {},
            json.dumps(
                {
                    "model": "ministral-3b-2410",
                    "choices": [
                        {"message": {"content": "private result"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                }
            ).encode(),
        )

    def gateway():
        return Gateway(
            db,
            (target,),
            b"fixture-digest-key-more-than-32-bytes",
            lambda _: "fixture-secret-key",
            transport,
            clock=lambda: now,
        )

    task = {
        "request_key": "new-model-fixture",
        "capability": "text_generation",
        "input": "private prompt",
        "max_output_tokens": 64,
        "max_attempts": 1,
    }
    result = gateway().run(task)
    assert result["state"] == "completed"
    status = gateway().run(task)
    assert len(calls) == 1
    assert status["attempts"][0]["diagnostics"]["provider_reported_model"] == "ministral-3b-2410"
    with sqlite3.connect(db) as con:
        saved = json.loads(
            con.execute("SELECT diagnostics_json FROM gateway_attempts").fetchone()[0]
        )
    assert saved["provider_model_field"] == "model"
    for private in (b"private prompt", b"private result", b"fixture-secret-key"):
        assert private not in db.read_bytes()


@pytest.mark.parametrize("finish", ["STOP", "MAX_TOKENS", "SAFETY", None, {}, []])
def test_google_finish_is_fixed_metadata(finish):
    r = metadata(
        "google",
        {"modelVersion": "gemini-3.5-flash-lite", "candidates": [{"finishReason": finish}]},
        "gemini-3.5-flash-lite",
    )
    assert r["provider_finish_reason"] == (finish if isinstance(finish, str) else None)


def test_google_duplicate_finish_is_unknown():
    r = safe_response_diagnostics(
        "google",
        200,
        {},
        b'{"candidates":[{"finishReason":"STOP","finishReason":"MAX_TOKENS"}]}',
        requested_model="gemini-3.5-flash-lite",
    )
    assert r["provider_finish_reason"] is None
