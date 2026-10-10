"""Official protocol fixtures: no provider IO, credentials, or account queries."""

import base64
import io
import json
import struct
import wave
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from quota_broker.families import FamilyError, FamilyRegistry, MediaLimits
from quota_broker.family_adapters import (
    PACKAGED_FAMILY_ADAPTERS,
    UNSUPPORTED_PROTOCOL_GAPS,
    FamilyAdapter,
)
from quota_broker.gateway_providers import ProviderError
from quota_broker.registry import Registry

FAMILIES = FamilyRegistry.builtin()
LIMITS = MediaLimits()
ACCOUNT = "a" * 32
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + b"\0" * 17


def media(kind="image", mime="image/png", raw=PNG):
    return {"type": kind, "mime_type": mime, "data": base64.b64encode(raw).decode()}


def audio_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 8001)
    return output.getvalue()


WAV = audio_bytes()
AUDIO = media("audio", "audio/wav", WAV)
CHAT = {
    "choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
}
EMBED = {"data": [{"index": 0, "embedding": [0.1, -0.2]}]}
GOOGLE = {
    "candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
}


def cf(result):
    return {"success": True, "errors": [], "result": result}


def profile(name):
    adapter = PACKAGED_FAMILY_ADAPTERS[name]
    assert isinstance(adapter, FamilyAdapter)
    return adapter


def specification(name, provider, capability):
    adapter = profile(name)
    template = adapter.endpoints[provider]
    model = (
        "@cf/fixture-model"
        if provider == "cloudflare"
        else "ocr.space/engine2"
        if provider == "ocrspace"
        else "fixture-model"
    )
    origin = "https://" + urlsplit(template).netloc
    base = Registry.builtin().resolve("nvidia/nemotron-3.5-lightning-30b-a3b")
    return replace(
        base,
        provider=provider,
        model=model,
        origin=origin,
        endpoint_template=template,
        adapter=name,
        capability=capability,
        capabilities=(capability,),
        features=tuple(sorted(adapter.supported_features)),
        max_output_tokens=200,
    )


def task(capability, value, options=None):
    maximum = 100 if FAMILIES.uses_output_tokens(capability) else 0
    prepared = FAMILIES.prepare(capability, value, options or {}, LIMITS, maximum)
    return {
        "capability": capability,
        "input": prepared.input,
        "options": prepared.options,
        "max_output_tokens": maximum,
        "requirements": {"features": list(prepared.required_features)},
    }


def text_input():
    return {"messages": [{"role": "user", "content": "hello"}]}


# Each row exercises the serializer AND its actual typed result contract.
CASES = [
    ("openai_inference", p, "text_generation", text_input(), {}, CHAT)
    for p in ("nvidia", "groq", "mistral", "openrouter")
] + [
    (
        "openai_embeddings",
        "openrouter",
        "embedding",
        {"texts": ["hello"]},
        {"dimensions": 2},
        EMBED,
    ),
    ("nvidia_gte_embeddings", "nvidia", "embedding", {"texts": ["hello"]}, {}, EMBED),
    (
        "nvidia_embeddings",
        "nvidia",
        "embedding",
        {"texts": ["hello"]},
        {"input_type": "query"},
        EMBED,
    ),
    ("mistral_embeddings", "mistral", "embedding", {"texts": ["hello"]}, {"dimensions": 2}, EMBED),
    (
        "nvidia_rerank",
        "nvidia",
        "rerank",
        {"query": "q", "documents": ["a", "b"]},
        {},
        {"rankings": [{"index": 1, "logit": 0.9}]},
    ),
    (
        "mistral_fim",
        "mistral",
        "code_completion",
        text_input(),
        {"suffix": "tail", "seed": 7},
        CHAT,
    ),
    (
        "mistral_ocr",
        "mistral",
        "ocr",
        {"document": media()},
        {},
        {"pages": [{"index": 0, "markdown": "page"}], "usage_info": {"pages_processed": 1}},
    ),
    (
        "groq_audio_transcription",
        "groq",
        "audio_transcription",
        {"audio": AUDIO},
        {"language": "en"},
        {"text": "recognized", "language": "en", "duration": 1.01},
    ),
    (
        "groq_audio_translation",
        "groq",
        "audio_translation",
        {"audio": AUDIO},
        {"target_language": "en"},
        {"text": "translated"},
    ),
    (
        "groq_tts",
        "groq",
        "tts",
        {"text": "speak"},
        {"voice": "fixture-voice", "format": "wav"},
        WAV,
    ),
    (
        "mistral_audio_transcription",
        "mistral",
        "audio_transcription",
        {"audio": AUDIO},
        {"language": "en"},
        {
            "text": "recognized",
            "usage": {
                "prompt_audio_seconds": 1.001,
                "prompt_tokens": 4,
                "completion_tokens": 635,
                "total_tokens": 3264,
            },
        },
    ),
    (
        "mistral_tts",
        "mistral",
        "tts",
        {"text": "speak"},
        {"voice": "fixture-voice", "format": "wav"},
        {"audio_data": AUDIO["data"]},
    ),
    (
        "mistral_classification",
        "mistral",
        "classification",
        {"texts": ["hello"]},
        {},
        {
            "id": "cls-fixture",
            "model": "fixture-model",
            "results": [{"safety": {"scores": {"benign": 87, "other": -1.2}}}],
        },
    ),
    (
        "mistral_moderation",
        "mistral",
        "moderation",
        {"texts": ["hello"]},
        {},
        {"results": [{"categories": {"unsafe": False}, "category_scores": {"unsafe": 0.1}}]},
    ),
    ("gemini_inference", "google", "text_generation", text_input(), {"temperature": 0.2}, GOOGLE),
    (
        "gemini_inference",
        "google",
        "vision",
        {"messages": [{"role": "user", "content": [media()]}]},
        {},
        GOOGLE,
    ),
    (
        "gemini_inference",
        "google",
        "audio_transcription",
        {"audio": AUDIO},
        {"language": "en"},
        GOOGLE,
    ),
    (
        "gemini_inference",
        "google",
        "image_generation",
        {"prompt": "picture"},
        {},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": media()["data"]}}
                        ]
                    }
                }
            ]
        },
    ),
    (
        "gemini_inference",
        "google",
        "tts",
        {"text": "speak"},
        {"voice": "fixture-voice", "format": "wav"},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/L16;codec=pcm;rate=24000",
                                    "data": base64.b64encode(b"\0\0" * 240).decode(),
                                }
                            }
                        ]
                    }
                }
            ]
        },
    ),
    (
        "gemini_embeddings",
        "google",
        "embedding",
        {"texts": ["hello"]},
        {"dimensions": 2},
        {"embedding": {"values": [0.1, -0.2]}},
    ),
    (
        "cloudflare_text",
        "cloudflare",
        "text_generation",
        text_input(),
        {"response_format": "json_object"},
        cf({"response": "answer"}),
    ),
    (
        "cloudflare_embeddings",
        "cloudflare",
        "embedding",
        {"texts": ["hello"]},
        {},
        cf({"data": [[0.1, -0.2]]}),
    ),
    (
        "cloudflare_translation",
        "cloudflare",
        "translation",
        {"text": "hello"},
        {"source_language": "en", "target_language": "fr"},
        cf({"translated_text": "bonjour"}),
    ),
    (
        "cloudflare_audio_transcription",
        "cloudflare",
        "audio_transcription",
        {"audio": AUDIO},
        {"language": "en", "prompt": "context"},
        cf({"text": "recognized"}),
    ),
    (
        "cloudflare_audio_translation",
        "cloudflare",
        "audio_translation",
        {"audio": AUDIO},
        {"target_language": "en"},
        cf({"text": "translated"}),
    ),
    (
        "cloudflare_tts",
        "cloudflare",
        "tts",
        {"text": "speak"},
        {"format": "mp3"},
        b"ID3" + b"\0" * 20,
    ),
    (
        "cloudflare_image_generation",
        "cloudflare",
        "image_generation",
        {"prompt": "picture"},
        {"size": "512x512", "n": 1},
        PNG,
    ),
    (
        "cloudflare_classification",
        "cloudflare",
        "classification",
        {"texts": ["hello"]},
        {},
        cf([{"label": "positive", "score": 0.9}]),
    ),
    (
        "openrouter_image_generation",
        "openrouter",
        "image_generation",
        {"prompt": "picture"},
        {"n": 1, "format": "png", "size": "512x512"},
        {"data": [{"b64_json": media()["data"], "media_type": "image/png"}]},
    ),
    (
        "ocrspace_inference",
        "ocrspace",
        "ocr",
        {"document": media()},
        {"language": "eng", "detect_orientation": True, "scale": True, "is_table": True},
        {
            "IsErroredOnProcessing": False,
            "OCRExitCode": 1,
            "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "page"}],
        },
    ),
]


@pytest.mark.parametrize("name,provider,capability,value,options,response", CASES)
def test_documented_profiles_typed_round_trip(name, provider, capability, value, options, response):
    adapter = profile(name)
    spec = specification(name, provider, capability)
    data = task(capability, value, options)
    original = json.dumps(data, sort_keys=True)
    adapter.admit_task(spec, data)
    request = adapter.request_task(spec, ACCOUNT, "fixture-auth", data)
    assert request.url == adapter.endpoint_for(spec, capability, ACCOUNT)
    assert request.url.startswith(spec.origin + "/")
    assert json.dumps(data, sort_keys=True) == original
    assert request.body and request.content_type
    if provider == "google":
        assert request.headers["x-goog-api-key"] == "fixture-auth"
    elif provider == "ocrspace":
        assert "Authorization" not in request.headers
        assert b"fixture-auth" in request.body
    else:
        assert request.headers["Authorization"] == "Bearer fixture-auth"
    raw = response if isinstance(response, bytes) else json.dumps(response).encode()
    parsed = adapter.interpret_task(spec, capability, 200, {"x-request-id": "req-fixture"}, raw)
    assert parsed.content is not None
    FAMILIES.validate_result(capability, parsed.content, LIMITS)
    assert parsed.request_id == (
        response.get("id", "req-fixture") if isinstance(response, dict) else "req-fixture"
    )
    assert (
        "audio_seconds" not in parsed.usage
    )  # Fractional/local durations never become actual integer use.
    if name == "mistral_ocr":
        assert parsed.usage == {"pages": 1}
    if name == "gemini_inference" and capability == "audio_transcription":
        assert "generationConfig" not in request.payload
    if name == "mistral_fim":
        assert request.payload["random_seed"] == 7 and request.payload["suffix"] == "tail"
    if name == "mistral_embeddings":
        assert request.payload["output_dimension"] == 2 and "dimensions" not in request.payload
    if name == "openrouter_image_generation":
        assert request.payload["output_format"] == "png" and "format" not in request.payload


@pytest.mark.parametrize("name", sorted(PACKAGED_FAMILY_ADAPTERS))
def test_profile_admission_is_explicit_and_output_bounded(name):
    adapter = profile(name)
    capability = next(iter(adapter.supported_capabilities))
    provider = next(iter(adapter.endpoints))
    spec = specification(name, provider, capability)
    assert adapter.supported_capabilities <= adapter.supported_features
    for maximum in (True, -1, 201, 0 if adapter.uses_output_tokens(capability) else 1):
        with pytest.raises(FamilyError):
            adapter.admit_task(
                spec,
                {
                    "capability": capability,
                    "input": {},
                    "options": {},
                    "max_output_tokens": maximum,
                },
            )
    for options in ({"unknown": 1}, {"stream": True}, {"realtime": True}):
        with pytest.raises(FamilyError):
            adapter.admit_task(
                spec,
                {
                    "capability": capability,
                    "input": {},
                    "options": options,
                    "max_output_tokens": 1 if adapter.uses_output_tokens(capability) else 0,
                },
            )
    with pytest.raises(FamilyError):
        adapter.admit_task(
            replace(spec, capabilities=("unknown",), capability="unknown"),
            {"capability": capability},
        )
    with pytest.raises(FamilyError):
        adapter.endpoint_for(replace(spec, origin="https://attacker.invalid"), capability, ACCOUNT)
    with pytest.raises(FamilyError):
        adapter.endpoint_for(
            replace(spec, model="https://attacker.invalid/injected"), capability, ACCOUNT
        )


@pytest.mark.parametrize(
    "name,provider,capability,value,options",
    [
        ("nvidia_embeddings", "nvidia", "embedding", {"texts": ["hi"]}, {}),
        ("gemini_embeddings", "google", "embedding", {"texts": ["a", "b"]}, {}),
        ("cloudflare_classification", "cloudflare", "classification", {"texts": ["a", "b"]}, {}),
        ("groq_tts", "groq", "tts", {"text": "hello"}, {}),
        (
            "groq_audio_translation",
            "groq",
            "audio_translation",
            {"audio": AUDIO},
            {"target_language": "fr"},
        ),
        ("gemini_inference", "google", "image_generation", {"prompt": "hi"}, {"format": "png"}),
    ],
)
def test_known_schema_gaps_reject_before_request(name, provider, capability, value, options):
    with pytest.raises(FamilyError):
        profile(name).admit_task(
            specification(name, provider, capability), task(capability, value, options)
        )


@pytest.mark.parametrize(
    "raw", [b'{"choices":[],"choices":[]}', b'{"choices":NaN}', b"[]", b"{}", b"not json"]
)
def test_response_errors_are_fixed_and_do_not_reflect_body(raw):
    adapter = profile("openai_inference")
    spec = specification("openai_inference", "groq", "text_generation")
    with pytest.raises(ProviderError) as error:
        adapter.interpret_task(spec, "text_generation", 200, {}, raw)
    assert str(error.value) == "invalid_family_response"
    assert adapter.interpret_task(spec, "text_generation", 403, {}, raw).content is None


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 6},
        {"prompt_tokens": 2, "input_tokens": 3},
        {"total_tokens": True},
        {"total_tokens": -1},
        {"total_tokens": 1.5},
    ],
)
def test_conflicting_or_invalid_usage_fails_closed(usage):
    response = dict(CHAT, usage=usage)
    with pytest.raises(ProviderError):
        profile("openai_inference").interpret_task(
            specification("openai_inference", "groq", "text_generation"),
            "text_generation",
            200,
            {},
            json.dumps(response).encode(),
        )


def test_missing_usage_never_fabricated():
    response = {"choices": CHAT["choices"]}
    result = profile("openai_inference").interpret_task(
        specification("openai_inference", "groq", "text_generation"),
        "text_generation",
        200,
        {"x-request-id": "sk-untrusted"},
        json.dumps(response).encode(),
    )
    assert result.usage == {} and result.request_id is None


def test_registry_does_not_override_legacy_profiles_and_documents_gaps():
    assert (
        not {"openai_chat", "gemini_generate_content", "cloudflare_workers_ai"}
        & PACKAGED_FAMILY_ADAPTERS.keys()
    )
    assert "mistral_classification" in PACKAGED_FAMILY_ADAPTERS
    assert "mistral_classification" not in UNSUPPORTED_PROTOCOL_GAPS
    assert "ocrspace_overlay" not in UNSUPPORTED_PROTOCOL_GAPS
    assert "gemini_signed_tool_calls" not in UNSUPPORTED_PROTOCOL_GAPS
    assert "openrouter_audio_output" in UNSUPPORTED_PROTOCOL_GAPS


@pytest.mark.parametrize("provider", ["nvidia", "groq", "mistral", "openrouter"])
def test_full_tool_conversation_and_json_schema_preserved(provider):
    name = "openai_inference"
    spec = specification(name, provider, "text_generation")
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"hello"}'},
    }
    messages = [
        {"role": "user", "content": "lookup"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call-1", "content": '{"found":true}'},
    ]
    options = {
        "tools": [
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
        ],
        "tool_choice": "lookup",
        "json_schema": {"type": "object"},
        "reasoning_effort": "low",
        "temperature": 0.2,
    }
    request = profile(name).request_task(
        spec, ACCOUNT, "fixture-auth", task("text_generation", {"messages": messages}, options)
    )
    assert request.payload["messages"] == messages
    assert request.payload["tools"] == options["tools"]
    assert request.payload["response_format"]["json_schema"]["schema"] == options["json_schema"]
    assert request.payload["reasoning_effort"] == "low"
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                "finish_reason": "tool_calls",
            }
        ]
    }
    parsed = profile(name).interpret_task(
        spec, "text_generation", 200, {}, json.dumps(response).encode()
    )
    assert parsed.content["tool_calls"] == [call]
    FAMILIES.validate_result("text_generation", parsed.content, LIMITS)


def test_cloudflare_account_path_is_exact_and_no_urls_accepted():
    adapter = profile("cloudflare_embeddings")
    spec = specification("cloudflare_embeddings", "cloudflare", "embedding")
    with pytest.raises(FamilyError):
        adapter.endpoint_for(spec, "embedding", "a/../../other")
    assert adapter.endpoint_for(
        spec, "embedding", "{account_id}"
    ) == spec.endpoint_template.replace("{model}", spec.model)
    with pytest.raises(FamilyError):
        FAMILIES.prepare(
            "ocr",
            {"document": {"type": "document", "url": "https://attacker.invalid"}},
            {},
            LIMITS,
            0,
        )


def test_remote_image_output_is_rejected():
    raw = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "https://attacker.invalid"}}
                        ],
                    }
                }
            ]
        }
    ).encode()
    with pytest.raises(ProviderError):
        profile("openai_inference").interpret_task(
            specification("openai_inference", "openrouter", "text_generation"),
            "text_generation",
            200,
            {},
            raw,
        )


@pytest.mark.parametrize(
    "name,provider,capability,body",
    [
        (
            "openai_inference",
            "groq",
            "text_generation",
            {"choices": [{"message": {"content": [None]}}]},
        ),
        ("openai_embeddings", "openrouter", "embedding", {"data": [None]}),
        ("mistral_ocr", "mistral", "ocr", {"pages": [None]}),
        ("nvidia_rerank", "nvidia", "rerank", {"rankings": [None]}),
        ("mistral_moderation", "mistral", "moderation", {"results": [None]}),
        (
            "gemini_inference",
            "google",
            "text_generation",
            {"candidates": [{"content": {"parts": [None]}}]},
        ),
        ("openrouter_image_generation", "openrouter", "image_generation", {"data": [None]}),
        (
            "ocrspace_inference",
            "ocrspace",
            "ocr",
            {"IsErroredOnProcessing": False, "OCRExitCode": 1, "ParsedResults": [None]},
        ),
    ],
)
def test_malformed_collection_members_have_fixed_errors(name, provider, capability, body):
    with pytest.raises(ProviderError, match="^invalid_family_response$"):
        profile(name).interpret_task(
            specification(name, provider, capability),
            capability,
            200,
            {},
            json.dumps(body).encode(),
        )


def test_mistral_chat_sampling_seed_maps_to_official_parameter():
    name = "openai_inference"
    request = profile(name).request_task(
        specification(name, "mistral", "text_generation"),
        ACCOUNT,
        "fixture-auth",
        task("text_generation", text_input(), {"seed": 7}),
    )
    assert request.payload["random_seed"] == 7
    assert "seed" not in request.payload


def test_google_tools_preserve_name_id_args_and_response_link():
    adapter = profile("gemini_inference")
    spec = specification("gemini_inference", "google", "text_generation")
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"hello"}'},
    }
    value = {
        "messages": [
            {"role": "system", "content": "instruction"},
            {"role": "user", "content": "lookup"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"found":true}'},
        ]
    }
    data = task(
        "text_generation",
        value,
        {
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "lookup",
            "json_schema": {"type": "object"},
        },
    )
    request = adapter.request_task(spec, ACCOUNT, "fixture-auth", data)
    assert request.payload["systemInstruction"] == {"parts": [{"text": "instruction"}]}
    assert request.payload["contents"][-1]["parts"] == [
        {"functionResponse": {"id": "call-1", "name": "lookup", "response": {"found": True}}}
    ]
    assert request.payload["contents"][-2]["parts"][-1] == {
        "functionCall": {"id": "call-1", "name": "lookup", "args": {"q": "hello"}}
    }
    assert request.payload["generationConfig"]["responseJsonSchema"] == {"type": "object"}
    assert request.payload["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == [
        "lookup"
    ]
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"id": "next-1", "name": "lookup", "args": {"q": "again"}}}
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }
    parsed = adapter.interpret_task(spec, "text_generation", 200, {}, json.dumps(response).encode())
    assert parsed.content["tool_calls"][0]["function"]["name"] == "lookup"
    FAMILIES.validate_result("text_generation", parsed.content, LIMITS)
    response["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = (
        "opaque-private-signature"
    )
    signed = adapter.interpret_task(spec, "text_generation", 200, {}, json.dumps(response).encode())
    assert signed.content["tool_calls"][0]["thought_signature"] == "opaque-private-signature"
    assert (
        signed.content["messages"][0]["provider_state"]["parts"]
        == response["candidates"][0]["content"]["parts"]
    )
    FAMILIES.validate_result("text_generation", signed.content, LIMITS)


@pytest.mark.parametrize("provider", ["nvidia", "groq", "mistral"])
def test_unproven_audio_input_schema_rejected_in_admission(provider):
    adapter = profile("openai_inference")
    spec = specification("openai_inference", provider, "text_generation")
    data = task("text_generation", {"messages": [{"role": "user", "content": [AUDIO]}]})
    with pytest.raises(FamilyError):
        adapter.admit_task(spec, data)


def test_openrouter_inline_audio_request_has_no_remote_url():
    adapter = profile("openai_inference")
    request = adapter.request_task(
        specification("openai_inference", "openrouter", "text_generation"),
        ACCOUNT,
        "fixture-auth",
        task("text_generation", {"messages": [{"role": "user", "content": [AUDIO]}]}),
    )
    assert request.payload["messages"][0]["content"] == [
        {"type": "input_audio", "input_audio": {"data": AUDIO["data"], "format": "wav"}}
    ]


def test_asr_has_no_hidden_local_actual_usage():
    adapter = profile("groq_audio_transcription")
    result = adapter.interpret_task(
        specification("groq_audio_transcription", "groq", "audio_transcription"),
        "audio_transcription",
        200,
        {},
        b'{"text":"ok","duration":1.01}',
    )
    assert result.usage == {}


@pytest.mark.parametrize("result", [None, {"request_id": "async-job"}, {"translated_text": 17}])
def test_cloudflare_async_or_unknown_translation_response_rejected(result):
    with pytest.raises(ProviderError, match="^invalid_family_response$"):
        profile("cloudflare_translation").interpret_task(
            specification("cloudflare_translation", "cloudflare", "translation"),
            "translation",
            200,
            {},
            json.dumps(cf(result)).encode(),
        )


def test_nvidia_schema_profiles_are_chosen_explicitly_not_guessed():
    adapter = profile("nvidia_gte_embeddings")
    spec = specification("nvidia_gte_embeddings", "nvidia", "embedding")
    request = adapter.request_task(
        spec, ACCOUNT, "fixture-auth", task("embedding", {"texts": ["hello"]})
    )
    assert "input_type" not in request.payload
    for options in ({"input_type": "query"}, {"dimensions": 2}):
        with pytest.raises(FamilyError):
            adapter.admit_task(spec, task("embedding", {"texts": ["hello"]}, options))


OVERLAY = {
    "HasOverlay": True,
    "Lines": [
        {
            "Words": [{"WordText": "hello", "Left": 1, "Top": 2, "Width": 30, "Height": 8}],
            "MaxHeight": 8,
            "MinTop": 2,
        }
    ],
    "Message": None,
}


def test_ocr_overlay_round_trip_preserves_coordinates():
    adapter = profile("ocrspace_inference")
    spec = specification("ocrspace_inference", "ocrspace", "ocr")
    request = adapter.request_task(
        spec, ACCOUNT, "fixture-auth", task("ocr", {"document": media()}, {"overlay": True})
    )
    assert b"isOverlayRequired" in request.body and b"true" in request.body
    body = {
        "IsErroredOnProcessing": False,
        "OCRExitCode": 1,
        "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "hello", "TextOverlay": OVERLAY}],
    }
    parsed = adapter.interpret_task(spec, "ocr", 200, {}, json.dumps(body).encode())
    overlay = parsed.content["pages"][0]["overlay"]
    assert overlay == {
        "has_overlay": True,
        "lines": [
            {
                "words": [{"text": "hello", "left": 1.0, "top": 2.0, "width": 30.0, "height": 8.0}],
                "max_height": 8.0,
                "min_top": 2.0,
            }
        ],
    }
    FAMILIES.validate_result("ocr", parsed.content, LIMITS)
    assert parsed.usage == {}  # Bounding boxes and number of pages are not actual quota counters.


@pytest.mark.parametrize(
    "overlay",
    [
        {"HasOverlay": False, "Lines": [], "Message": ""},
        {"HasOverlay": True, "Lines": OVERLAY["Lines"] * 100, "Message": "OCR overlay"},
    ],
)
def test_ocr_empty_or_many_overlay_lines_remain_bounded(overlay):
    body = {
        "IsErroredOnProcessing": False,
        "OCRExitCode": 1,
        "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "hello", "TextOverlay": overlay}],
    }
    parsed = profile("ocrspace_inference").interpret_task(
        specification("ocrspace_inference", "ocrspace", "ocr"),
        "ocr",
        200,
        {},
        json.dumps(body).encode(),
    )
    FAMILIES.validate_result("ocr", parsed.content, LIMITS)
    assert len(parsed.content["pages"][0]["overlay"]["lines"]) == len(overlay["Lines"])


@pytest.mark.parametrize(
    "overlay",
    [
        {"HasOverlay": "true", "Lines": []},
        {"HasOverlay": True, "Lines": [{}]},
        {"HasOverlay": True, "Lines": OVERLAY["Lines"] * 4097},
        {"HasOverlay": True, "Lines": [{"Words": [], "MaxHeight": -1, "MinTop": 0}]},
    ],
)
def test_invalid_ocr_overlay_rejected(overlay):
    body = {
        "IsErroredOnProcessing": False,
        "OCRExitCode": 1,
        "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "hello", "TextOverlay": overlay}],
    }
    with pytest.raises(ProviderError, match="^invalid_family_response$"):
        profile("ocrspace_inference").interpret_task(
            specification("ocrspace_inference", "ocrspace", "ocr"),
            "ocr",
            200,
            {},
            json.dumps(body).encode(),
        )


def google_continuation_result():
    parts = [
        {"text": "private thought", "thought": True, "thoughtSignature": "opaque-thought"},
        {"functionCall": {"name": "lookup"}, "thoughtSignature": "opaque-call-signature"},
        {"text": "ready", "thoughtSignature": "opaque-text-signature"},
        {"functionCall": {"id": "actual-id-2", "name": "second", "args": {"city": "Paris"}}},
    ]
    body = {
        "responseId": "response-fixture",
        "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
    }
    spec = specification("gemini_inference", "google", "text_generation")
    parsed = profile("gemini_inference").interpret_task(
        spec, "text_generation", 200, {}, json.dumps(body).encode()
    )
    return spec, parts, body, parsed


def test_google_signed_parts_and_missing_ids_continue_exactly():
    spec, parts, body, parsed = google_continuation_result()
    adapter = profile("gemini_inference")
    again = adapter.interpret_task(spec, "text_generation", 200, {}, json.dumps(body).encode())
    assert again.content == parsed.content  # Stable typed ID for a missing provider ID.
    assert parsed.content["text"] == "ready"
    calls = parsed.content["tool_calls"]
    assert calls[0]["provider_call_id"] is None
    assert calls[0]["function"]["arguments"] == "{}"
    assert calls[0]["thought_signature"] == "opaque-call-signature"
    assert calls[1]["provider_call_id"] == "actual-id-2"
    message = parsed.content["messages"][0]
    assert message["provider_state"] == {"provider": "google", "model": spec.model, "parts": parts}
    FAMILIES.validate_result("text_generation", parsed.content, LIMITS)
    messages = [{"role": "user", "content": "lookup"}, message] + [
        {"role": "tool", "tool_call_id": call["id"], "content": '{"found":true}'} for call in calls
    ]
    data = task("text_generation", {"messages": messages})
    assert "google_continuation" in data["requirements"]["features"]
    request = adapter.request_task(spec, ACCOUNT, "fixture-auth", data)
    assert request.payload["contents"][1]["parts"] == parts
    assert request.payload["contents"][2]["parts"] == [
        {"functionResponse": {"name": "lookup", "response": {"found": True}}}
    ]
    assert request.payload["contents"][3]["parts"] == [
        {"functionResponse": {"name": "second", "id": "actual-id-2", "response": {"found": True}}}
    ]


@pytest.mark.parametrize(
    "mutation", ["model", "provider", "text", "arguments", "provider_id", "signature"]
)
def test_google_state_cannot_cross_model_provider_or_shadow_visible_message(mutation):
    spec, _, _, parsed = google_continuation_result()
    message = json.loads(json.dumps(parsed.content["messages"][0]))
    if mutation in {"model", "provider"}:
        message["provider_state"][mutation] = (
            "different-model" if mutation == "model" else "mistral"
        )
    elif mutation == "text":
        message["content"] = "changed"
    elif mutation == "arguments":
        message["tool_calls"][0]["function"]["arguments"] = '{"changed":true}'
    elif mutation == "provider_id":
        message["tool_calls"][0]["provider_call_id"] = "fabricated-id"
    else:
        message["tool_calls"][0]["thought_signature"] = "changed"
    with pytest.raises(FamilyError):
        data = task("text_generation", {"messages": [message]})
        profile("gemini_inference").admit_task(spec, data)


def test_google_continuation_is_not_sent_to_another_provider():
    _, _, _, parsed = google_continuation_result()
    data = task("text_generation", {"messages": parsed.content["messages"]})
    with pytest.raises(FamilyError):
        profile("openai_inference").admit_task(
            specification("openai_inference", "mistral", "text_generation"), data
        )


@pytest.mark.parametrize(
    "part",
    [
        {"executableCode": {"code": "raise"}},
        {"text": "visible", "thoughtSignature": 12},
        {"functionCall": {"name": "lookup", "args": []}},
    ],
)
def test_google_unknown_executable_or_invalid_part_state_rejected(part):
    body = {"candidates": [{"content": {"parts": [part]}}]}
    with pytest.raises(ProviderError, match="^invalid_family_response$"):
        profile("gemini_inference").interpret_task(
            specification("gemini_inference", "google", "text_generation"),
            "text_generation",
            200,
            {},
            json.dumps(body).encode(),
        )


@pytest.mark.parametrize(
    "results",
    [
        [[{"scores": [87]}]],
        [{"target": {"scores": [87]}}],
        [{"target": {"scores": {"label": True}}}],
        [{"target": {"scores": {"label": float("inf")}}}],
    ],
)
def test_mistral_classification_requires_official_map_schema(results):
    body = {"id": "cls-fixture", "model": "fixture-model", "results": results}
    with pytest.raises(ProviderError, match="^invalid_family_response$"):
        profile("mistral_classification").interpret_task(
            specification("mistral_classification", "mistral", "classification"),
            "classification",
            200,
            {},
            json.dumps(body).encode(),
        )


def test_google_inline_signed_part_order_preserved_in_continuation():
    spec = specification("gemini_inference", "google", "text_generation")
    parts = [
        {"text": "before", "thoughtSignature": "sig-text"},
        {
            "inlineData": {"mimeType": "image/png", "data": media()["data"]},
            "thoughtSignature": "sig-image",
        },
        {"functionCall": {"name": "lookup", "args": {}}},
        {"text": "after"},
    ]
    raw = json.dumps({"candidates": [{"content": {"parts": parts}}]}).encode()
    adapter = profile("gemini_inference")
    parsed = adapter.interpret_task(spec, "text_generation", 200, {}, raw)
    normalized = FAMILIES.validate_result("text_generation", parsed.content, LIMITS)
    message = normalized["messages"][0]
    assert [part["type"] for part in message["content"]] == ["text", "image", "text"]
    assert message["content"][1]["thought_signature"] == "sig-image"
    data = task("text_generation", {"messages": [{"role": "user", "content": "lookup"}, message]})
    request = adapter.request_task(spec, ACCOUNT, "fixture-auth", data)
    assert request.payload["contents"][1]["parts"] == parts


@pytest.mark.parametrize(
    "model_id",
    [
        "vendor/model with space",
        'vendor/model("quoted")',
        "vendor/model%name",
        "供應者/模型",
        "vendor/model..version",
        "vendor/model?variant=one",
    ],
)
def test_catalog_identity_to_candidate_to_fixed_json_request(tmp_path, model_id):
    from datetime import UTC, datetime

    from quota_broker.discovery import DiscoveryStore
    from quota_broker.discovery_parsers import parse_models
    from quota_broker.discovery_sources import validate_snapshot

    checked = datetime(2026, 10, 3, tzinfo=UTC)
    raw = {
        "data": [
            {
                "id": model_id,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
                "context_length": 8192,
                "top_provider": {"max_completion_tokens": 200},
            }
        ]
    }
    snapshot = validate_snapshot(
        parse_models("openrouter", raw, checked, output_modalities="all"), checked
    )
    store = DiscoveryStore(tmp_path / "discovery.sqlite", Registry.builtin(), clock=lambda: checked)
    store.refresh(snapshot)
    candidate = store.lookup("openrouter", model_id, "text_generation")
    assert candidate["model"] == model_id
    assert candidate["adapter_support"] == "compatible_unregistered"
    registry = Registry.from_manifest(
        {
            "schema_version": 1,
            "providers": [
                {
                    "id": "openrouter",
                    "adapter": "openai_inference",
                    "origin": "https://openrouter.ai",
                    "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                }
            ],
            "models": [
                {
                    "provider": "openrouter",
                    "model": model_id,
                    "capability": "text_generation",
                    "context_tokens": 8192,
                    "max_output_tokens": 200,
                    "features": ["text", "text_generation"],
                }
            ],
        }
    )
    spec = registry.resolve(model_id, provider="openrouter")
    request = profile("openai_inference").request_task(
        spec, ACCOUNT, "fixture-auth", task("text_generation", text_input())
    )
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.payload["model"] == model_id
    assert json.loads(request.body)["model"] == model_id
    assert model_id not in request.url


@pytest.mark.parametrize(
    "provider,model_id",
    [
        ("google", "model/path"),
        ("google", "model?x=1"),
        ("google", "model with space"),
        ("cloudflare", "@cf/../other"),
        ("cloudflare", "@cf/vendor/../../other"),
        ("cloudflare", "@cf/vendor/model?x=1"),
        ("cloudflare", "@cf/vendor/model%2Fescape"),
    ],
)
def test_path_identity_stays_strict_when_body_identity_is_printable(provider, model_id):
    name = "gemini_inference" if provider == "google" else "cloudflare_text"
    spec = replace(specification(name, provider, "text_generation"), model=model_id)
    with pytest.raises(FamilyError):
        profile(name).endpoint_for(spec, "text_generation", ACCOUNT)


@pytest.mark.parametrize("response_text", ['{"value":NaN}', '{"value":1e999}', "not-json"])
def test_google_tool_response_invalid_json_rejects_during_admission(response_text):
    spec, _, _, parsed = google_continuation_result()
    call = parsed.content["tool_calls"][0]
    data = task(
        "text_generation",
        {
            "messages": [
                parsed.content["messages"][0],
                {"role": "tool", "tool_call_id": call["id"], "content": response_text},
            ]
        },
    )
    with pytest.raises(FamilyError):
        profile("gemini_inference").admit_task(spec, data)
