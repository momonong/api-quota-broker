"""Official-schema fixtures only: parser and shared policy, never provider GETs."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from quota_broker.discovery import DiscoveryError
from quota_broker.discovery_parsers import (
    MODEL_FIELDS,
    ParserError,
    ocrspace_snapshot,
    parse_models,
)
from quota_broker.discovery_sources import validate_snapshot

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def openrouter_model(model="vendor/model", *, pricing=None, inputs=None, outputs=None):
    return {
        "id": model,
        "name": "discard-human-name",
        "description": "discard-description",
        "architecture": {
            "input_modalities": ["text"] if inputs is None else inputs,
            "output_modalities": ["text"] if outputs is None else outputs,
        },
        "context_length": 8192,
        "top_provider": {"max_completion_tokens": 2048},
        "pricing": {"prompt": "0", "completion": "0", "request": "0"}
        if pricing is None
        else pricing,
        "supported_parameters": ["tools", "reasoning", "response_format", "structured_outputs"],
    }


FIXTURES = {
    "nvidia": {"object": "list", "data": [{"id": "nvidia/nemotron-fixture"}]},
    "groq": {
        "object": "list",
        "data": [
            {
                "id": "whisper-fixture",
                "active": True,
                "context_window": 448,
                "max_completion_tokens": 448,
            }
        ],
    },
    "mistral": {
        "object": "list",
        "data": [
            {
                "id": "mistral-fixture",
                "capabilities": {
                    "completion_chat": True,
                    "vision": True,
                    "function_calling": True,
                    "reasoning": True,
                    "audio": True,
                    "ocr": True,
                },
                "max_context_length": 8192,
            }
        ],
    },
    "google": {
        "models": [
            {
                "name": "models/gemini-fixture",
                "inputTokenLimit": 8192,
                "outputTokenLimit": 2048,
                "supportedGenerationMethods": ["generateContent", "countTokens", "embedContent"],
                "thinking": True,
            }
        ]
    },
    "cloudflare": {
        "success": True,
        "errors": [],
        "messages": ["discard-message"],
        "result": [
            {
                "id": "discard-uuid",
                "name": "@cf/meta/fixture-model",
                "task": {"name": "Text Generation"},
                "description": "discard-description",
            }
        ],
        "result_info": {"page": 1, "total_pages": 1, "total_count": 1},
    },
    "openrouter": {
        "data": [openrouter_model()],
        "total_count": 1,
        "links": {"next": None, "prev": None},
    },
    "ocrspace": {},
}


@pytest.mark.parametrize("provider", FIXTURES)
def test_all_seven_parse_through_actual_snapshot_policy(provider):
    raw = copy.deepcopy(FIXTURES[provider])
    before = copy.deepcopy(raw)
    parsed = parse_models(provider, raw, NOW, output_modalities="all")
    checked = validate_snapshot(parsed, NOW)
    assert raw == before
    assert checked == parsed
    assert parsed["provider"] == provider and parsed["complete"] is True
    assert all(set(row) == set(MODEL_FIELDS) for row in parsed["models"])
    assert all(row["status"] == "listed" for row in parsed["models"])
    encoded = json.dumps(parsed)
    assert "discard-" not in encoded
    assert not any(field in parsed for field in ("account_availability", "live_result"))
    if provider not in {"openrouter", "ocrspace"}:
        assert all(
            row["free_eligibility"] == "unknown" and row["free_source"] is None
            for row in parsed["models"]
        )


def test_nvidia_and_groq_model_names_never_imply_protocol_or_capability():
    for provider in ("nvidia", "groq"):
        result = parse_models(
            provider,
            {
                "data": [
                    {
                        "id": "provider/chat-vision-riva-embedding",
                        "capabilities": {"completion_chat": True},
                    },
                    {"id": "provider/speech-model", "active": False},
                ]
            },
            NOW,
        )
        assert {row["capability"] for row in result["models"]} == {"unknown"}
        assert all(row["protocol"] is None and row["endpoint"] is None for row in result["models"])
        if provider == "groq":
            assert "inactive" in result["models"][1]["features"]
        assert all(row["status"] == "listed" for row in result["models"])


def test_mistral_capability_flags_keep_each_explicit_family():
    capabilities = {
        "completion_chat": True,
        "completion_fim": True,
        "ocr": True,
        "classification": True,
        "moderation": True,
        "audio_transcription": True,
        "audio_transcription_realtime": True,
        "audio_speech": True,
        "vision": True,
        "function_calling": True,
        "reasoning": True,
        "audio": True,
        "fine_tuning": True,
    }
    result = parse_models(
        "mistral",
        {
            "data": [
                {
                    "id": "opaque-model-id",
                    "capabilities": capabilities,
                    "max_context_length": 8192,
                    "archived": True,
                    "deprecation": (NOW - timedelta(days=1)).isoformat(),
                }
            ]
        },
        NOW,
    )
    assert {row["capability"] for row in result["models"]} == {
        "text_generation",
        "code_completion",
        "ocr",
        "classification",
        "moderation",
        "audio_transcription",
        "realtime_audio_transcription",
        "tts",
    }
    assert all(
        "inactive" in row["features"] and "deprecated" in row["features"]
        for row in result["models"]
    )
    chat = next(row for row in result["models"] if row["capability"] == "text_generation")
    assert {"vision", "image_input", "audio_input", "tool_calling", "reasoning"} <= set(
        chat["features"]
    )
    assert (
        chat["protocol"] == "openai_inference"
        and chat["endpoint"] == "https://api.mistral.ai/v1/chat/completions"
    )
    assert all(row["free_eligibility"] == "unknown" for row in result["models"])
    validate_snapshot(result, NOW)


def test_mistral_unknown_flags_and_future_deprecation_preserve_gap():
    result = parse_models(
        "mistral",
        {
            "data": [
                {
                    "id": "opaque-model",
                    "capabilities": {"completion_chat": True, "future_science": True},
                    "deprecation": (NOW + timedelta(days=1)).isoformat(),
                }
            ]
        },
        NOW,
    )
    assert {row["capability"] for row in result["models"]} == {"text_generation", "unknown"}
    unknown = next(row for row in result["models"] if row["capability"] == "unknown")
    assert "unrecognized_capability" in unknown["features"]
    assert (
        "deprecation_scheduled" in unknown["features"] and "deprecated" not in unknown["features"]
    )
    assert "future_science" not in json.dumps(result)


def test_mistral_absent_or_wrong_capability_metadata_does_not_guess_embedding():
    result = parse_models(
        "mistral",
        {
            "data": [
                {"id": "mistral-embed"},
                {"id": "some-model", "capabilities": {"completion_chat": "true"}},
            ]
        },
        NOW,
    )
    assert len(result["models"]) == 2
    assert all(row["capability"] == "unknown" for row in result["models"])


def test_google_methods_and_predict_gap_are_not_model_name_inference():
    result = parse_models(
        "google",
        {
            "models": [
                {
                    "name": "models/opaque-method-model",
                    "supportedGenerationMethods": ["generateContent", "embedContent"],
                },
                {"name": "models/imagen-sounding-name", "supportedGenerationMethods": ["predict"]},
                {"name": "models/tts-sounding-name"},
            ]
        },
        NOW,
    )
    known = [row for row in result["models"] if row["model"] == "opaque-method-model"]
    assert {row["capability"] for row in known} == {"text_generation", "embedding"}
    assert {row["protocol"] for row in known} == {"gemini_inference", "gemini_embeddings"}
    assert {
        row["capability"] for row in result["models"] if row["model"] != "opaque-method-model"
    } == {"unknown"}
    predict = next(row for row in result["models"] if row["model"] == "imagen-sounding-name")
    assert predict["features"] == ["predict"] and predict["endpoint"] is None
    validate_snapshot(result, NOW)


@pytest.mark.parametrize(
    "task,family",
    [
        ("Text Generation", "text_generation"),
        ("Text Embeddings", "embedding"),
        ("Text Classification", "classification"),
        ("Text-to-Image", "image_generation"),
        ("Text-to-Speech", "tts"),
        ("Automatic Speech Recognition", "audio_transcription"),
        ("Translation", "translation"),
        ("Future Task", "unknown"),
    ],
)
def test_cloudflare_task_schema_and_canonical_identity(task, family):
    result = parse_models(
        "cloudflare",
        {
            "success": True,
            "result": [
                {
                    "id": "uuid-is-not-model-selector",
                    "name": "@cf/vendor/opaque",
                    "task": {"name": task},
                }
            ],
        },
        NOW,
    )
    row = result["models"][0]
    assert row["model"] == "@cf/vendor/opaque" and row["capability"] == family
    assert row["free_eligibility"] == "unknown"
    assert result["complete"] is False  # This endpoint is paginated; missing info is a gap.
    validate_snapshot(result, NOW)


@pytest.mark.parametrize(
    "pricing,expected",
    [
        ({"prompt": "0", "completion": "0", "request": "0"}, "free"),
        ({"prompt": "0.000", "completion": "0e-20", "image": "0"}, "free"),
        ({"prompt": "0", "completion": "0", "request": "0.0001"}, "paid"),
        ({"prompt": "0", "completion": "0", "image": "0.01"}, "paid"),
        ({"prompt": "0", "completion": "0", "audio": "1e-10000"}, "paid"),
        ({"prompt": "0", "completion": "-1"}, "unknown"),
        ({"prompt": "-1", "completion": "0.1"}, "paid"),
        ({"prompt": "0", "completion": "NaN"}, "unknown"),
        ({"prompt": "0", "completion": "Infinity"}, "unknown"),
        ({"prompt": "0", "completion": "-Infinity"}, "unknown"),
        ({"prompt": "0", "completion": "0", "future_price": "dynamic"}, "unknown"),
        ({"prompt": "0", "completion": False}, "unknown"),
        ({"prompt": "0"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_openrouter_full_pricing_decimal_conservative(pricing, expected):
    result = parse_models(
        "openrouter",
        {"data": [openrouter_model("vendor/model:free", pricing=pricing)]},
        NOW,
        output_modalities="all",
    )
    row = result["models"][0]
    assert row["free_eligibility"] == expected
    assert (row["free_source"] is None) == (expected == "unknown")
    validate_snapshot(result, NOW)


def test_openrouter_explicit_modalities_keep_all_families_and_supported_features():
    item = openrouter_model(
        inputs=["text", "image", "audio", "video"],
        outputs=[
            "text",
            "image",
            "audio",
            "video",
            "embeddings",
            "rerank",
            "speech",
            "transcription",
            "decisions",
        ],
    )
    result = parse_models("openrouter", {"data": [item]}, NOW, output_modalities="all")
    assert {row["capability"] for row in result["models"]} == {
        "text_generation",
        "image_generation",
        "audio_generation",
        "video_generation",
        "embedding",
        "rerank",
        "tts",
        "audio_transcription",
        "decisions",
    }
    assert all(
        {
            "image_input",
            "audio_input",
            "video_input",
            "vision",
            "tool_calling",
            "reasoning",
            "json_output",
        }
        <= set(row["features"])
        for row in result["models"]
    )
    embedding = next(row for row in result["models"] if row["capability"] == "embedding")
    assert embedding["endpoint"] == "https://openrouter.ai/api/v1/embeddings"
    assert embedding["protocol"] == "openai_embeddings"
    validate_snapshot(result, NOW)


def test_openrouter_unknown_output_modality_remains_visible_without_raw_message():
    item = openrouter_model(outputs=["text", "future_output"])
    result = parse_models("openrouter", {"data": [item]}, NOW, output_modalities="all")
    assert {row["capability"] for row in result["models"]} == {"text_generation", "unknown"}
    assert (
        "unrecognized_output_modality"
        in next(row for row in result["models"] if row["capability"] == "unknown")["features"]
    )
    assert "future_output" not in json.dumps(result)


def test_openrouter_legacy_modality_is_explicit_metadata():
    item = openrouter_model()
    item["architecture"] = {"modality": "text+image->text"}
    result = parse_models("openrouter", {"data": [item]}, NOW)
    row = result["models"][0]
    assert row["capability"] == "text_generation" and "vision" in row["features"]


def test_openrouter_default_text_filter_can_never_claim_complete_catalog():
    result = parse_models("openrouter", copy.deepcopy(FIXTURES["openrouter"]), NOW)
    assert result["complete"] is False and result["source"] == "https://openrouter.ai/api/v1/models"
    complete = parse_models(
        "openrouter", copy.deepcopy(FIXTURES["openrouter"]), NOW, output_modalities="all"
    )
    assert complete["complete"] is True
    assert complete["source"] == "https://openrouter.ai/api/v1/models?output_modalities=all"


@pytest.mark.parametrize(
    "pagination",
    [
        {"nextPageToken": "next"},
        {"has_more": True},
        {"has_more": "false"},
        {"offset": 2},
        {"total_count": 2},
        {"total_count": True},
        {"links": {"next": "https://untrusted.example/next"}},
        {"links": {"prev": "previous-page"}},
        {"limit": 1},
        {"pagination": {"offset": 1}},
        {"pagination": "unknown"},
    ],
)
def test_openrouter_pagination_gap_makes_all_output_snapshot_partial(pagination):
    result = parse_models(
        "openrouter", {"data": [openrouter_model()], **pagination}, NOW, output_modalities="all"
    )
    assert result["complete"] is False and len(result["models"]) == 1
    assert "untrusted.example" not in json.dumps(result)
    validate_snapshot(result, NOW)


@pytest.mark.parametrize(
    "provider,key",
    [
        ("nvidia", "data"),
        ("groq", "data"),
        ("mistral", "data"),
        ("google", "models"),
        ("cloudflare", "result"),
        ("openrouter", "data"),
    ],
)
def test_empty_list_is_partial_and_does_not_mark_all_models_removed(provider, key):
    raw = {key: [], **({"success": True} if provider == "cloudflare" else {})}
    result = parse_models(provider, raw, NOW, output_modalities="all")
    assert result["models"] == [] and result["complete"] is False
    validate_snapshot(result, NOW)


@pytest.mark.parametrize("value", [None, 0, -1, True, "8192", 100_000_001, 1.5])
def test_invalid_optional_token_metadata_becomes_unknown(value):
    result = parse_models(
        "groq",
        {
            "data": [
                {
                    "id": "model",
                    "context_window": value,
                    "max_completion_tokens": value,
                }
            ]
        },
        NOW,
    )
    assert result["models"][0]["context_tokens"] is None
    assert result["models"][0]["max_output_tokens"] is None
    validate_snapshot(result, NOW)


@pytest.mark.parametrize(
    "provider,raw",
    [
        ("unknown", {}),
        ("nvidia", {}),
        ("nvidia", {"data": "not-a-list"}),
        ("nvidia", {"data": [None]}),
        ("nvidia", {"data": [{}]}),
        ("nvidia", {"data": [{"id": "https://evil.example/model"}]}),
        ("nvidia", {"data": [{"id": "control\nvalue"}]}),
        ("nvidia", {"data": [{"id": "duplicate"}, {"id": "duplicate"}]}),
        ("nvidia", {"error": {"message": "sensitive-error"}, "data": []}),
        ("google", {"models": [{"name": "models/foo?key=sensitive"}]}),
        ("cloudflare", {"success": False, "errors": ["sensitive-error"], "result": []}),
        ("cloudflare", {"result": []}),
        ("ocrspace", {"engines": [{"id": "invented"}]}),
    ],
)
def test_required_schema_failures_are_fixed_content_free_errors(provider, raw):
    with pytest.raises(DiscoveryError) as captured:
        parse_models(provider, raw, NOW)
    assert captured.value.code == "invalid_request"
    assert str(captured.value) == "invalid official model metadata"


def test_timestamp_requires_offset_and_normalizes_utc():
    with pytest.raises(DiscoveryError):
        parse_models("nvidia", FIXTURES["nvidia"], NOW.replace(tzinfo=None))
    assert parse_models("nvidia", FIXTURES["nvidia"], NOW)["checked_at"] == NOW.isoformat()


def test_ocr_static_document_facts_preserve_deprecated_engine_and_no_retirement():
    result = ocrspace_snapshot(NOW)
    assert [row["model"] for row in result["models"]] == [
        "ocr.space/engine1",
        "ocr.space/engine2",
        "ocr.space/engine3",
    ]
    assert result["models"][0]["features"] == [
        "deprecated",
        "image_input",
        "pdf_input",
        "text_output",
    ]
    assert all(
        row["status"] == "listed" and row["free_eligibility"] == "free" for row in result["models"]
    )
    assert all(row["free_source"] == "https://ocr.space/ocrapi" for row in result["models"])
    assert "handwriting" in result["models"][2]["features"]
    validate_snapshot(result, NOW)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"data": [{"id": "invalid\nprivate model value"}]}, "model_id_characters"),
        ({"data": [{"id": "https://provider.invalid/a"}]}, "model_id_url"),
        ({"data": [{"id": ""}]}, "model_id_empty"),
        ({"data": [{"id": "same"}, {"id": "same"}]}, "duplicate_model"),
        ({"data": {}}, "collection_schema"),
        ({"data": ["private raw text"]}, "item_schema"),
    ],
)
def test_parser_errors_offer_fixed_phase_reason_without_raw_values(raw, reason):
    with pytest.raises(ParserError) as caught:
        parse_models("nvidia", raw, NOW)
    assert caught.value.phase == "parse_models"
    assert caught.value.reason == reason
    assert caught.value.code == "invalid_request"
    assert str(caught.value) == "invalid official model metadata"


def test_contradictory_limits_refuse_snapshot_without_rewriting_or_dropping_model():
    model = openrouter_model("vendor/private-fixture")
    model["context_length"] = 1024
    model["top_provider"]["max_completion_tokens"] = 2048
    raw = {"data": [model]}
    before = copy.deepcopy(raw)
    with pytest.raises(ParserError) as caught:
        parse_models("openrouter", raw, NOW, output_modalities="all")
    assert caught.value.reason == "context_bound"
    assert raw == before
    assert "private-fixture" not in str(caught.value)


def test_unknown_parser_error_reason_cannot_reflect_raw_values():
    error = ParserError("provider-controlled raw text")
    assert error.reason == "schema"


@pytest.mark.parametrize(
    "provider,raw",
    [
        (
            "google",
            {
                "models": [
                    {
                        "name": "models/fixture",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/fixture",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            },
        ),
        ("openrouter", {"data": [openrouter_model(), openrouter_model()]}),
    ],
)
def test_repeated_model_entries_refuse_duplicate_identity_without_silent_dedup(provider, raw):
    with pytest.raises(ParserError) as caught:
        parse_models(provider, raw, NOW, output_modalities="all")
    assert caught.value.reason == "duplicate_model"


def test_protocol_profiles_match_packaged_typed_adapters_and_policy():
    from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS

    snapshots = [
        parse_models(
            "mistral",
            {
                "data": [
                    {
                        "id": "model",
                        "capabilities": {
                            "completion_chat": True,
                            "completion_fim": True,
                            "ocr": True,
                        },
                    }
                ]
            },
            NOW,
        ),
        parse_models(
            "google",
            {
                "models": [
                    {
                        "name": "models/model",
                        "supportedGenerationMethods": ["generateContent", "embedContent"],
                    }
                ]
            },
            NOW,
        ),
        parse_models(
            "openrouter",
            {"data": [openrouter_model(outputs=["text", "image", "embeddings"])]},
            NOW,
            output_modalities="all",
        ),
        ocrspace_snapshot(NOW),
    ]
    for snapshot in snapshots:
        validate_snapshot(snapshot, NOW)
        for row in snapshot["models"]:
            assert row["protocol"] in PACKAGED_FAMILY_ADAPTERS
            adapter = PACKAGED_FAMILY_ADAPTERS[row["protocol"]]
            assert row["capability"] in adapter.supported_capabilities
            assert row["endpoint"].startswith(
                next(iter(adapter.allowed_origins(snapshot["provider"]))) + "/"
            )
    image = next(row for row in snapshots[2]["models"] if row["capability"] == "image_generation")
    assert image["endpoint"] == "https://openrouter.ai/api/v1/images"


def test_cloudflare_task_family_does_not_prove_model_wire_schema():
    snapshot = parse_models(
        "cloudflare",
        {
            "success": True,
            "result": [
                {"name": "@cf/vendor/model", "task": {"name": "Automatic Speech Recognition"}}
            ],
        },
        NOW,
    )
    row = snapshot["models"][0]
    assert row["capability"] == "audio_transcription"
    assert row["protocol"] is None
    assert "protocol_schema_unknown" in row["features"]
    validate_snapshot(snapshot, NOW)


def test_mistral_embedding_looking_name_is_not_protocol_evidence():
    snapshot = parse_models("mistral", {"data": [{"id": "mistral-embed", "capabilities": {}}]}, NOW)
    assert snapshot["models"][0]["capability"] == "unknown"
    assert snapshot["models"][0]["protocol"] is None
    validate_snapshot(snapshot, NOW)


@pytest.mark.parametrize(
    "value,detail",
    [
        (None, "type"),
        ("", "empty"),
        ("a" * 257, "length"),
        ("https://provider.invalid/a", "url"),
        ("org/id\x00", "characters"),
        ("org/id\n", "characters"),
    ],
)
def test_model_identity_diagnostics_distinguish_fixed_rejection_rules(value, detail):
    with pytest.raises(DiscoveryError) as error:
        parse_models("openrouter", {"data": [{"id": value}]}, NOW)
    assert error.value.phase == "parse_models"
    assert error.value.reason == "model_id_" + detail
    assert error.value.detail == detail
    assert str(error.value) == "invalid official model metadata"


def test_model_identity_diagnostic_detail_never_reflects_unknown_values():
    error = ParserError("model_id", "provider-controlled-raw-message")
    assert error.reason == "model_id" and error.detail is None
