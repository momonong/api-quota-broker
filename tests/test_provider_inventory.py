"""Official list metadata cannot grant account access or infer capabilities."""

from datetime import UTC, datetime

import pytest

from quota_broker.discovery_parsers import parse_models
from quota_broker.discovery_sources import PUBLIC_URLS, validate_snapshot

NOW = datetime(2026, 11, 16, tzinfo=UTC)


@pytest.mark.parametrize("provider", ["openai", "sambanova", "cerebras", "huggingface"])
def test_extra_provider_catalog_keeps_every_identity_without_granting_access(provider):
    raw = {"data": [{"id": "vendor/coding-vision"}, {"id": "vendor/other"}]}
    snapshot = validate_snapshot(parse_models(provider, raw, NOW), NOW)
    assert {row["model"] for row in snapshot["models"]} == {"vendor/coding-vision", "vendor/other"}
    assert all(row["capability"] == "unknown" for row in snapshot["models"])
    assert all(row["free_eligibility"] == "unknown" for row in snapshot["models"])
    assert all(row["endpoint"] is None for row in snapshot["models"])
    # HF router lists chat models, not all Hub inference modalities.
    assert snapshot["complete"] is (provider != "huggingface")


@pytest.mark.parametrize("provider", ["openai", "sambanova", "cerebras", "huggingface"])
def test_extra_provider_pagination_cannot_claim_complete(provider):
    snapshot = parse_models(provider, {"data": [{"id": "one"}], "has_more": True}, NOW)
    assert snapshot["complete"] is False


def test_anonymous_catalog_endpoints_are_fixed_and_exclude_private_openai():
    assert PUBLIC_URLS[("huggingface", "all")] == "https://router.huggingface.co/v1/models"
    assert PUBLIC_URLS[("cerebras", "all")] == "https://api.cerebras.ai/public/v1/models"
    assert PUBLIC_URLS[("sambanova", "all")] == "https://api.sambanova.ai/v1/models"
    assert not any(provider == "openai" for provider, _ in PUBLIC_URLS)


def test_nvidia_explicit_metadata_preserves_features_without_name_guessing():
    row = parse_models(
        "nvidia",
        {
            "data": [
                {
                    "id": "vendor/model",
                    "context_window": 1048576,
                    "supports_thinking": True,
                    "supports_tools": True,
                }
            ]
        },
        NOW,
    )["models"][0]
    assert row["context_tokens"] == 1048576
    assert row["features"] == ["reasoning", "tool_calling"]
    assert row["capability"] == "unknown"


@pytest.mark.parametrize(
    ("model_id", "provider", "context"),
    [
        ("z-ai/glm-5.3-flash", "nvidia", 1048576),
        ("@cf/google/gemma-4-26b-a4b-it", "cloudflare", 256000),
    ],
)
def test_reviewed_current_models_have_pinned_official_profiles(model_id, provider, context):
    from quota_broker.catalog import MODELS

    model = MODELS[model_id]
    assert model.provider == provider
    assert model.context_tokens == context
    assert model.verified_at == "2026-11-16"
    assert "local output cap" in model.use_restrictions
