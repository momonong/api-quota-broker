"""Trusted registry and protocol boundaries, using only in-process HTTP fixtures."""

import io
import json
import urllib.error
import urllib.request
import urllib.response
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest

from quota_broker.catalog import MODELS
from quota_broker.gateway_providers import official_request
from quota_broker.registry import MAX_RESPONSE_BYTES, Registry, RegistryError


def manifest(model="eighth-chat"):
    return {
        "schema_version": 1,
        "providers": [
            {
                "id": "eighth",
                "adapter": "openai_chat",
                "origin": "https://eighth.example",
                "endpoint": "https://eighth.example/v1/chat/completions",
            }
        ],
        "models": [
            {
                "provider": "eighth",
                "model": model,
                "capability": "text_generation",
                "context_tokens": 8192,
                "max_output_tokens": 2048,
                "features": ["text", "json_output"],
                "input_parameters": ["input"],
                "output_parameters": ["max_output_tokens"],
            }
        ],
    }


def load(tmp_path: Path, document=None):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document if document is not None else manifest()))
    return Registry.load(path)


def request(registry, spec):
    return registry.request(spec, "account", "fixture-secret", "hello", 42, None, None)


def completion(usage=None, *, status=200):
    return status, json.dumps(
        {
            "id": "fixture-123",
            "choices": [{"message": {"content": "fixture answer"}}],
            "usage": usage
            if usage is not None
            else {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        }
    ).encode()


def test_eighth_provider_is_instance_local_and_duplicate_model_requires_provider(tmp_path):
    before = dict(MODELS)
    builtin = Registry.builtin()
    model_id = "openai/gpt-oss-20b"
    extended = load(tmp_path, manifest(model_id))
    assert len(extended.providers) == 8
    assert len(builtin.providers) == 7 and "eighth" not in builtin.providers
    assert MODELS == before
    assert builtin.resolve(model_id).provider == "groq"
    assert extended.resolve(model_id, "groq").adapter == "builtin:groq"
    spec = extended.resolve(model_id, "eighth")
    assert spec.adapter == "openai_chat"
    assert spec.features == ("text", "json_output")
    assert spec.input_parameters == ("input",)
    assert spec.output_parameters == ("max_output_tokens",)
    assert extended.models[("eighth", model_id)] == spec
    with pytest.raises(RegistryError, match="provider constraint"):
        extended.resolve(model_id)
    with pytest.raises(RegistryError, match="unrecognized"):
        builtin.resolve(model_id, "eighth")
    with pytest.raises(TypeError):
        extended.models[("eighth", model_id)] = spec


def test_openai_chat_request_and_interpret(tmp_path):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    assert url == "https://eighth.example/v1/chat/completions"
    assert headers == {"Authorization": "Bearer fixture-secret"}
    assert payload == {
        "model": "eighth-chat",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 42,
        "stream": False,
    }
    assert registry.interpret(spec, *completion()) == ("fixture answer", 2, 3, None, "fixture-123")
    assert registry.interpret(spec, *completion(status=429))[0] is None
    assert registry.interpret(spec, 200, b"malformed") == (None, None, None, None, None)


@pytest.mark.parametrize(
    "provider,known_model",
    [
        ("mistral", "ministral-3b-latest"),
        ("openrouter", "liquid/lfm-2.5-2.6b:free"),
        ("groq", "openai/gpt-oss-20b"),
        ("nvidia", "google/gemma-4-31b-it"),
    ],
)
def test_existing_openai_provider_accepts_new_model_at_pinned_endpoint(
    tmp_path, provider, known_model
):
    original = Registry.builtin().resolve(known_model, provider)
    document = manifest("new-chat-model")
    document["providers"][0].update(
        id=provider, origin=original.origin, endpoint=original.endpoint_template
    )
    document["models"][0]["provider"] = provider
    extended = load(tmp_path, document)
    assert extended.resolve(known_model, provider) == original
    added = extended.resolve("new-chat-model", provider)
    assert added.adapter == "openai_chat"
    url, _, payload = request(extended, added)
    assert url == original.endpoint_template and payload["model"] == "new-chat-model"
    assert extended.interpret(added, *completion())[1:3] == (2, 3)
    document["providers"][0].update(
        origin="https://other.example", endpoint="https://other.example/chat"
    )
    with pytest.raises(RegistryError, match="overridden"):
        load(tmp_path, document)


def test_builtin_tuple_cannot_be_overwritten(tmp_path):
    original = Registry.builtin().resolve("ministral-3b-latest", "mistral")
    document = manifest(original.model)
    document["providers"][0].update(
        id="mistral", origin=original.origin, endpoint=original.endpoint_template
    )
    document["models"][0]["provider"] = "mistral"
    with pytest.raises(RegistryError, match="duplicate provider/model"):
        load(tmp_path, document)


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.5-flash-lite",
        "@cf/meta/llama-3.2-1b-instruct",
        "ocr.space/engine2",
    ],
)
def test_non_openai_builtin_provider_cannot_use_generic_extension(tmp_path, model):
    original = Registry.builtin().resolve(model)
    document = manifest()
    document["providers"][0].update(
        id=original.provider,
        origin=original.origin,
        endpoint=original.endpoint_template.replace("{account_id}", "fixture"),
    )
    document["models"][0]["provider"] = original.provider
    with pytest.raises(RegistryError, match="overridden"):
        load(tmp_path, document)


def protocol_manifest(provider, *, fixed=False):
    if provider == "google":
        model = "gemini-fixture-new"
        original = Registry.builtin().resolve("gemini-3.5-flash-lite")
        adapter = "gemini_generate_content"
        path = "/v1beta/models/{model}:generateContent"
    else:
        model = "@cf/meta/fixture-new"
        original = Registry.builtin().resolve("@cf/meta/llama-3.2-1b-instruct")
        adapter = "cloudflare_workers_ai"
        path = "/client/v4/accounts/{account_id}/ai/run/{model}"
    document = manifest(model)
    document["providers"][0].update(
        id=provider,
        adapter=adapter,
        origin=original.origin,
        endpoint=original.origin + (path.replace("{model}", model) if fixed else path),
    )
    document["models"][0].update(provider=provider, features=["text"])
    return document, original


@pytest.mark.parametrize("provider", ["google", "cloudflare"])
@pytest.mark.parametrize("fixed", [False, True])
def test_existing_non_openai_provider_new_model_config_only(tmp_path, monkeypatch, provider, fixed):
    document, original = protocol_manifest(provider, fixed=fixed)
    registry = load(tmp_path, document)
    assert registry.resolve(original.model, provider) == original
    spec = registry.resolve(document["models"][0]["model"], provider)
    url, headers, payload = request(registry, spec)
    if provider == "google":
        assert (
            url
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-fixture-new:generateContent"
        )
        assert headers == {"x-goog-api-key": "fixture-secret"}
        assert payload == {
            "contents": [{"parts": [{"text": "hello"}]}],
            "generationConfig": {"maxOutputTokens": 42},
        }
        reply = {
            "candidates": [{"content": {"parts": [{"text": "fixture answer"}]}}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
        }
    else:
        assert (
            url
            == "https://api.cloudflare.com/client/v4/accounts/account/ai/run/@cf/meta/fixture-new"
        )
        assert headers == {"Authorization": "Bearer fixture-secret"}
        assert payload == {"prompt": "hello", "max_tokens": 42}
        reply = {
            "result": {
                "response": "fixture answer",
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "neurons": 1},
            }
        }
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, json.loads(req.data)))
            return FixtureResponse(json.dumps(reply).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    status, _, raw = registry.transport(spec, url, headers, payload, 2)
    result = registry.interpret(spec, status, raw)
    assert result[:3] == ("fixture answer", 2, 3)
    assert calls == [(url, payload)]


@pytest.mark.parametrize("provider", ["google", "cloudflare"])
def test_new_protocol_provider_origin_cannot_change(tmp_path, provider):
    document, _ = protocol_manifest(provider)
    document["providers"][0]["origin"] = "https://evil.example"
    document["providers"][0]["endpoint"] = document["providers"][0]["endpoint"].replace(
        "https://generativelanguage.googleapis.com"
        if provider == "google"
        else "https://api.cloudflare.com",
        "https://evil.example",
    )
    with pytest.raises(RegistryError, match="overridden"):
        load(tmp_path, document)


@pytest.mark.parametrize(
    "provider,path",
    [
        ("google", "/v1beta/models/{model}:predict"),
        ("google", "/v1beta/models/{account_id}:generateContent"),
        ("google", "/v1beta/models/{model}:generateContent?key=secret"),
        ("cloudflare", "/client/v4/accounts/fixture/ai/run/{model}"),
        ("cloudflare", "/client/v4/accounts/{account_id}/ai/run/{model}/extra"),
        ("cloudflare", "/client/v4/accounts/{account_id}/ai/run/{other}"),
        ("cloudflare", "/client/v4/accounts/{account_id}/ai/run/{model}?key=secret"),
    ],
)
def test_protocol_endpoint_template_cannot_inject_path_or_slots(tmp_path, provider, path):
    document, original = protocol_manifest(provider)
    document["providers"][0]["endpoint"] = original.origin + path
    with pytest.raises(RegistryError):
        load(tmp_path, document)


@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "../other"),
        ("google", "fixture%2Fother"),
        ("cloudflare", "@cf/../other"),
        ("cloudflare", "@cf/meta/../other"),
        ("cloudflare", "@cf/meta/fixture%2Fother"),
    ],
)
def test_model_slot_cannot_inject_path(tmp_path, provider, model):
    document, _ = protocol_manifest(provider)
    document["models"][0]["model"] = model
    with pytest.raises(RegistryError):
        load(tmp_path, document)


@pytest.mark.parametrize(
    "account", ["", "../other", "account/other", "account?key=secret", "account%2Fother"]
)
def test_cloudflare_account_slot_cannot_inject_path(tmp_path, monkeypatch, account):
    document, _ = protocol_manifest("cloudflare")
    registry = load(tmp_path, document)
    spec = registry.resolve("@cf/meta/fixture-new", "cloudflare")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: pytest.fail("network opened"))
    with pytest.raises(RegistryError):
        registry.request(spec, account, "fixture-secret", "hello", 42, None, None)
    with pytest.raises(RegistryError):
        registry.transport(
            spec,
            spec.endpoint_template.replace("{account_id}", account),
            {"Authorization": "Bearer fixture-secret"},
            {"prompt": "hello", "max_tokens": 42},
            2,
        )


def test_openai_json_output_request_and_gateway_payload_injection(tmp_path, monkeypatch):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = registry.request(
        spec,
        "account",
        "fixture-secret",
        "hello",
        42,
        None,
        None,
        response_format={"type": "json_object"},
    )
    assert payload["response_format"] == {"type": "json_object"}
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(json.loads(req.data))
            return FixtureResponse(completion()[1])

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    registry.transport(spec, url, headers, payload, 2)
    _, _, injected = request(registry, spec)
    injected["response_format"] = {"type": "json_object"}
    registry.transport(spec, url, headers, injected, 2)
    assert calls == [payload, payload]


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_schema"},
        {"type": "json_object", "endpoint": "https://evil.example"},
        None,
    ],
)
def test_openai_response_format_cannot_extend_unsupported_schema(
    tmp_path, monkeypatch, response_format
):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    payload["response_format"] = response_format
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: pytest.fail("network opened"))
    with pytest.raises(RegistryError):
        registry.transport(spec, url, headers, payload, 2)


def test_openai_json_output_requires_declared_model_feature(tmp_path, monkeypatch):
    document = manifest()
    document["models"][0]["features"] = ["text"]
    registry = load(tmp_path, document)
    spec = registry.resolve("eighth-chat")
    with pytest.raises(RegistryError, match="response format"):
        registry.request(
            spec,
            "account",
            "fixture-secret",
            "hello",
            42,
            None,
            None,
            response_format={"type": "json_object"},
        )
    url, headers, payload = request(registry, spec)
    payload["response_format"] = {"type": "json_object"}
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: pytest.fail("network opened"))
    with pytest.raises(RegistryError):
        registry.transport(spec, url, headers, payload, 2)


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": 2},
        {"completion_tokens": 3},
        {"prompt_tokens": 2, "completion_tokens": 3},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 6},
        {"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 4},
        {"prompt_tokens": -1, "completion_tokens": 3, "total_tokens": 2},
        {"prompt_tokens": 2, "completion_tokens": "3", "total_tokens": 5},
    ],
)
def test_generic_usage_unknown_when_missing_or_inconsistent(tmp_path, usage):
    registry = load(tmp_path)
    assert registry.interpret(registry.resolve("eighth-chat"), *completion(usage))[1:3] == (
        None,
        None,
    )


def test_duplicate_response_usage_does_not_settle(tmp_path):
    registry = load(tmp_path)
    result = registry.interpret(
        registry.resolve("eighth-chat"),
        200,
        b'{"usage":{"prompt_tokens":2,"prompt_tokens":3,"completion_tokens":3,"total_tokens":6}}',
    )
    assert result == (None, None, None, None, None)


@pytest.mark.parametrize("model", list(MODELS))
def test_builtin_request_compatibility(model):
    registry = Registry.builtin()
    spec = registry.resolve(model)
    source, target = ("en", "zh-TW") if spec.capability == "translation" else (None, None)
    assert registry.request(spec, "fixture", "fixture-secret", "hello", 1, source, target) == (
        official_request(
            spec.provider, model, "fixture", "fixture-secret", "hello", 1, source, target
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "http://eighth.example"),
        ("origin", "https://name:pass@eighth.example"),
        ("origin", "https://eighth.example/"),
        ("origin", "https://eighth.example?"),
        ("origin", "https://eighth.example#fragment"),
        ("origin", "https://eighth.example:99999"),
        ("endpoint", "https://eighth.example.evil/v1/chat/completions"),
        ("endpoint", "https://eighth.example:443/v1/chat/completions"),
        ("endpoint", "https://user@eighth.example/v1/chat/completions"),
        ("endpoint", "https://eighth.example/v1/chat/completions?redirect=evil"),
        ("endpoint", "https://eighth.example/v1/chat/completions#fragment"),
        ("endpoint", "https://eighth.example/{account_id}"),
        ("endpoint", "https://eighth.example\\evil/v1/chat/completions"),
        ("endpoint", "https://eighth.example/\nchat"),
    ],
)
def test_manifest_rejects_unsafe_origins_and_endpoints(tmp_path, field, value):
    document = manifest()
    document["providers"][0][field] = value
    with pytest.raises(RegistryError):
        load(tmp_path, document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d["providers"][0].update(adapter="downloaded-code"),
        lambda d: d["providers"][0].update(id="groq"),
        lambda d: d["providers"].append(d["providers"][0].copy()),
        lambda d: d["models"].append(d["models"][0].copy()),
        lambda d: d["models"][0].update(provider="missing"),
        lambda d: d["models"][0].update(capability="translation"),
        lambda d: d["models"][0].update(context_tokens=True),
        lambda d: d["models"][0].update(max_output_tokens=8193),
        lambda d: d["models"][0].update(features=["tools"]),
        lambda d: d["models"][0].update(input_parameters=["url"]),
        lambda d: d["models"][0].update(output_parameters=["max_completion_tokens"]),
        lambda d: d["models"][0].update(endpoint="https://evil.example/chat"),
        lambda d: d["models"][0].update(author=1),
        lambda d: d.update(models=[]),
    ],
)
def test_manifest_schema_fails_closed(tmp_path, mutation):
    document = manifest()
    mutation(document)
    with pytest.raises(RegistryError):
        load(tmp_path, document)


def test_duplicate_json_fields_rejected(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"schema_version":2,"schema_version":1,"providers":[],"models":[]}')
    with pytest.raises(RegistryError):
        Registry.load(path)


class EchoAdapter:
    """Fixture non-OpenAI protocol registered by application code, never downloaded."""

    def __init__(self):
        self.calls = []

    def request(self, spec, account_id, secret, content, max_output_tokens, source, target):
        return (
            spec.endpoint_template,
            {"x-echo-key": secret},
            {
                "echo": content,
                "limit": max_output_tokens,
            },
        )

    def transport(self, spec, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return 200, {}, json.dumps({"echoed": payload["echo"], "units": [2, 3]}).encode()

    def interpret(self, spec, status, raw):
        document = json.loads(raw)
        return document["echoed"], *document["units"], None, "echo-request"


def translation_task(source="zh-Hant", target="ja", content="未來翻譯" * 600):
    return {
        "capability": "translation",
        "input": content,
        "max_output_tokens": 42,
        "source_language": source,
        "target_language": target,
    }


def test_future_translation_adapter_owns_admission_without_riva_limits(tmp_path):
    class FutureTranslation(EchoAdapter):
        def __init__(self):
            super().__init__()
            self.admissions = []

        def admit(self, spec, task):
            self.admissions.append(task)
            if task["source_language"] != "zh-Hant" or task["target_language"] != "ja":
                raise ValueError("future adapter language pair rejected")

        def request(self, spec, account_id, secret, content, max_output_tokens, source, target):
            return (
                spec.endpoint_template,
                {"x-echo-key": secret},
                {
                    "echo": content,
                    "limit": max_output_tokens,
                    "source": source,
                    "target": target,
                },
            )

    document = manifest("future-translation")
    document["providers"][0].update(
        adapter="future_translation", endpoint="https://eighth.example/translate"
    )
    document["models"][0].update(
        capability="translation",
        features=["translation", "text"],
        input_parameters=["input", "source_language", "target_language"],
    )
    path = tmp_path / "translation-registry.json"
    path.write_text(json.dumps(document))
    adapter = FutureTranslation()
    registry = Registry.load(path, adapters={"future_translation": adapter})
    spec = registry.resolve("future-translation")
    task = translation_task()
    assert len(task["input"]) > 1952 and "en" not in {
        task["source_language"],
        task["target_language"],
    }
    registry.admit(spec, task)
    url, headers, payload = registry.request(
        spec,
        "account",
        "fixture-secret",
        task["input"],
        42,
        task["source_language"],
        task["target_language"],
    )
    status, _, raw = registry.transport(spec, url, headers, payload, 2)
    assert registry.interpret(spec, status, raw)[0] == task["input"]
    assert adapter.admissions == [task]
    with pytest.raises(ValueError, match="future adapter language"):
        registry.admit(spec, translation_task(source="en"))


def test_builtin_riva_admission_preserves_hosted_policy():
    registry = Registry.builtin()
    spec = registry.resolve("nvidia/riva-translate-4b-instruct-v2")
    registry.admit(spec, translation_task("en", "zh-tw", "x" * 1952))
    for task in (
        translation_task("zh-tw", "ja", "short"),
        translation_task("en", "unsupported", "short"),
        translation_task("en", "en", "short"),
        translation_task("en", "zh-tw", "x" * 1953),
    ):
        with pytest.raises(RegistryError):
            registry.admit(spec, task)
    # The builtin's text model is unaffected by Riva-only admission.
    registry.admit(
        registry.resolve("google/gemma-4-31b-it"),
        {
            "capability": "text_generation",
            "input": "x" * 3000,
        },
    )


def test_optional_adapter_admission_does_not_require_new_method(tmp_path):
    document = manifest()
    document["providers"][0]["adapter"] = "fixture_echo"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    registry = Registry.load(path, adapters={"fixture_echo": EchoAdapter()})
    spec = registry.resolve("eighth-chat")
    registry.admit(spec, {"capability": "text_generation", "input": "hello"})
    assert registry.quota_rejection(spec, 429, b'{"error":"quota"}', {}) is False
    assert (
        registry.quota_observations(
            spec, 429, b'{"error":"quota"}', {}, datetime(2026, 10, 2, tzinfo=UTC)
        )
        == []
    )
    with pytest.raises(RegistryError, match="capability mismatch"):
        registry.admit(spec, {"capability": "translation", "input": "hello"})


def quota_observation(now, *, source="fixture_quota_headers"):
    return {
        "metric": "requests",
        "window": "rolling_minute",
        "remaining": 4,
        "limit": 5,
        "as_of": now.isoformat(),
        "valid_until": (now + timedelta(seconds=30)).isoformat(),
        "reset_at": (now + timedelta(seconds=30)).isoformat(),
        "provenance": "observed",
        "source": source,
        "confidence": "provider_reported",
    }


def test_custom_adapter_quota_hooks_without_provider_switch(tmp_path):
    class QuotaEcho(EchoAdapter):
        def __init__(self):
            super().__init__()
            self.hook_calls = []

        def quota_rejection(self, spec, status, raw, headers, sensitive=()):
            self.hook_calls.append(("rejection", spec, status, raw, headers, sensitive))
            return status == 429 and raw == b'{"executed":false,"error":"quota"}'

        def quota_observations(self, spec, status, raw, headers, now, *, as_of=None):
            self.hook_calls.append(("observations", spec, status, raw, headers, now, as_of))
            return [quota_observation(as_of or now)]

    document = manifest()
    document["providers"][0]["adapter"] = "quota_echo"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    adapter = QuotaEcho()
    registry = Registry.load(path, adapters={"quota_echo": adapter})
    spec = registry.resolve("eighth-chat")
    raw = b'{"executed":false,"error":"quota"}'
    headers = {"x-fixture-quota": "4"}
    now = datetime(2026, 10, 2, tzinfo=UTC)
    as_of = now - timedelta(seconds=1)
    assert registry.quota_rejection(spec, 429, raw, headers, ("fixture-secret",)) is True
    assert registry.quota_rejection(spec, 429, b'{"executed":true}', headers) is False
    assert registry.quota_rejection(spec, 503, raw, headers) is False
    assert registry.quota_observations(spec, 429, raw, headers, now, as_of=as_of) == [
        quota_observation(as_of)
    ]
    assert adapter.hook_calls[0] == ("rejection", spec, 429, raw, headers, ("fixture-secret",))
    assert adapter.hook_calls[-1] == ("observations", spec, 429, raw, headers, now, as_of)


def test_generic_openai_has_no_builtin_quota_semantics_even_for_groq_id(tmp_path):
    original = Registry.builtin().resolve("openai/gpt-oss-20b", "groq")
    document = manifest("new-groq-model")
    document["providers"][0].update(
        id="groq", origin=original.origin, endpoint=original.endpoint_template
    )
    document["models"][0]["provider"] = "groq"
    registry = load(tmp_path, document)
    spec = registry.resolve("new-groq-model", "groq")
    raw = b'{"error":{"type":"rate_limit_error"}}'
    headers = {"Retry-After": "1", "x-ratelimit-remaining-requests": "0"}
    now = datetime(2026, 10, 2, tzinfo=UTC)
    assert registry.quota_rejection(spec, 429, raw, headers) is False
    assert registry.quota_observations(spec, 429, raw, headers, now) == []
    assert registry.quota_rejection(registry.resolve(original.model, "groq"), 429, raw, headers)


@pytest.mark.parametrize(
    "provider,model,headers,raw",
    [
        ("groq", "openai/gpt-oss-20b", {"Retry-After": "1"}, b'{"error":{}}'),
        ("google", "gemini-3.5-flash-lite", {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        ("cloudflare", "@cf/meta/llama-3.2-1b-instruct", {}, b'{"errors":[{"code":3036}]}'),
    ],
)
def test_builtin_quota_classifier_retains_explicit_nonexecution_rules(
    provider, model, headers, raw
):
    registry = Registry.builtin()
    spec = registry.resolve(model, provider)
    assert registry.quota_rejection(spec, 429, raw, headers)
    assert not registry.quota_rejection(spec, 503, raw, headers)
    assert not registry.quota_rejection(
        spec, 429, b'{"usage":{"prompt_tokens":1},"error":{}}', headers
    )


def test_builtin_classifier_rejects_sensitive_reflection():
    registry = Registry.builtin()
    spec = registry.resolve("openai/gpt-oss-20b", "groq")
    raw = json.dumps({"error": {"message": "敏感內容"}}).encode()
    assert not registry.quota_rejection(spec, 429, raw, {"Retry-After": "1"}, ("敏感內容",))
    assert not registry.quota_rejection(
        spec,
        429,
        b'{"error":{}}',
        {"Retry-After": "1", "x-reflection": "fixture-secret"},
        ("fixture-secret",),
    )


def test_builtin_groq_quota_observations_delegate_pure_parser(monkeypatch):
    from quota_broker import routing

    calls = []
    now = datetime(2026, 10, 2, tzinfo=UTC)
    as_of = now - timedelta(seconds=1)
    headers = {"x-ratelimit-remaining-requests": "4"}

    def parser(received, current, *, as_of=None):
        calls.append((received, current, as_of))
        return [quota_observation(as_of or current, source="groq_rate_limit_headers")]

    monkeypatch.setattr(routing, "groq_quota_observations", parser, raising=False)
    registry = Registry.builtin()
    result = registry.quota_observations(
        registry.resolve("openai/gpt-oss-20b", "groq"), 200, b"", headers, now, as_of=as_of
    )
    assert result == [quota_observation(as_of, source="groq_rate_limit_headers")]
    assert calls == [(headers, now, as_of)]
    assert (
        registry.quota_observations(registry.resolve("ministral-3b-latest"), 429, b"", headers, now)
        == []
    )


def test_builtin_groq_actual_parser_keeps_documented_dimensions():
    registry = Registry.builtin()
    spec = registry.resolve("openai/gpt-oss-20b", "groq")
    now = datetime(2026, 10, 2, tzinfo=UTC)
    headers = {
        "x-ratelimit-remaining-requests": "4",
        "x-ratelimit-limit-requests": "5",
        "x-ratelimit-reset-requests": "2h",
        "x-ratelimit-remaining-tokens": "100",
        "x-ratelimit-limit-tokens": "200",
        "x-ratelimit-reset-tokens": "30s",
    }
    observations = registry.quota_observations(spec, 200, b"", headers, now)
    assert [(item["metric"], item["window"], item["remaining"]) for item in observations] == [
        ("requests", "day", 4),
        ("tokens", "rolling_minute", 100),
    ]
    assert all(item["source"] == "groq_rate_limit_headers" for item in observations)
    assert all(item["as_of"] == now.isoformat() for item in observations)
    assert (
        registry.quota_observations(spec, 200, b"", {"x-ratelimit-remaining-tokens": "99"}, now)
        == []
    )


@pytest.mark.parametrize(
    "hook,value",
    [
        ("quota_rejection", 1),
        ("quota_rejection", "true"),
        ("quota_rejection", None),
        ("quota_observations", {}),
        ("quota_observations", [1]),
        ("quota_observations", None),
    ],
)
def test_adapter_quota_hook_return_types_fail_closed(tmp_path, hook, value):
    class BadQuota(EchoAdapter):
        pass

    setattr(BadQuota, hook, lambda *args, **kwargs: value)
    document = manifest()
    document["providers"][0]["adapter"] = "bad_quota"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    registry = Registry.load(path, adapters={"bad_quota": BadQuota()})
    spec = registry.resolve("eighth-chat")
    with pytest.raises(RegistryError, match="quota"):
        if hook == "quota_rejection":
            registry.quota_rejection(spec, 429, b"", {})
        else:
            registry.quota_observations(spec, 429, b"", {}, datetime(2026, 10, 2, tzinfo=UTC))


def test_non_openai_adapter_registered_in_code_without_central_protocol_switch(tmp_path):
    document = manifest()
    document["providers"][0].update(adapter="fixture_echo", endpoint="https://eighth.example/echo")
    document["models"][0].update(features=["echo_text"])
    path = tmp_path / "echo-registry.json"
    path.write_text(json.dumps(document))
    with pytest.raises(RegistryError, match="unregistered adapter"):
        Registry.load(path)
    adapter = EchoAdapter()
    registry = Registry.load(path, adapters={"fixture_echo": adapter})
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    status, _, raw = registry.transport(spec, url, headers, payload, 2)
    assert registry.interpret(spec, status, raw) == ("hello", 2, 3, None, "echo-request")
    assert adapter.calls == [
        (url, {"x-echo-key": "fixture-secret"}, {"echo": "hello", "limit": 42}, 2)
    ]
    assert "fixture_echo" not in Registry.builtin().adapters
    with pytest.raises(RegistryError):
        registry.transport(spec, "https://evil.example/echo", headers, payload, 2)
    assert len(adapter.calls) == 1


def test_custom_adapter_cannot_override_packaged_adapter_or_return_another_url(tmp_path):
    adapter = EchoAdapter()
    with pytest.raises(RegistryError, match="cannot be overridden"):
        Registry.builtin(adapters={"openai_chat": adapter})
    with pytest.raises(RegistryError, match="invalid adapter"):
        Registry.builtin(adapters={"incomplete": object()})
    document = manifest()
    document["providers"][0]["adapter"] = "bad_echo"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))

    class BadEcho(EchoAdapter):
        def request(self, *args):
            _, headers, payload = super().request(*args)
            return "https://evil.example/echo", headers, payload

    registry = Registry.load(path, adapters={"bad_echo": BadEcho()})
    with pytest.raises(RegistryError, match="unapproved"):
        request(registry, registry.resolve("eighth-chat"))


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/chat/completions",
        "https://eighth.example/v1/chat/completions/",
        "https://eighth.example/v1/other",
        "https://eighth.example/v1/chat/completions?token=secret",
        "https://eighth.example/v1/chat/completions#fragment",
        "https://user@eighth.example/v1/chat/completions",
    ],
)
def test_transport_rejects_origin_or_path_change_before_network(tmp_path, monkeypatch, url):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    _, headers, payload = request(registry, spec)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: pytest.fail("network opened"))
    with pytest.raises(RegistryError):
        registry.transport(spec, url, headers, payload, 1)


def test_unregistered_spec_and_header_rerouting_rejected(tmp_path, monkeypatch):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: pytest.fail("network opened"))
    for forged in (
        replace(spec, provider="other"),
        replace(spec, endpoint_template="https://evil"),
    ):
        with pytest.raises(RegistryError):
            request(registry, forged)
        with pytest.raises(RegistryError):
            registry.interpret(forged, *completion())
    with pytest.raises(RegistryError):
        registry.transport(spec, url, {**headers, "Host": "evil.example"}, payload, 1)
    with pytest.raises(RegistryError):
        registry.request(spec, "a", "secret\r\nHost:evil.example", "hi", 1, None, None)


class FixtureResponse(io.BytesIO):
    def __init__(self, raw, *, status=200):
        super().__init__(raw)
        self.status = status
        self.headers = {"Content-Type": "application/json"}


def test_transport_posts_fixed_payload_and_bounds_response(tmp_path, monkeypatch):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(
                (req.full_url, req.get_header("Authorization"), json.loads(req.data), timeout)
            )
            return FixtureResponse(completion()[1])

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    status, _, raw = registry.transport(spec, url, headers, payload, 2)
    assert registry.interpret(spec, status, raw)[0] == "fixture answer"
    assert calls == [(url, "Bearer fixture-secret", payload, 2)]

    class Oversized:
        def open(self, *_args, **_kwargs):
            return FixtureResponse(b"x" * (MAX_RESPONSE_BYTES + 1))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Oversized())
    with pytest.raises(RegistryError, match="too large"):
        registry.transport(spec, url, headers, payload, 2)


def test_redirect_never_follows_or_transfers_credentials(tmp_path, monkeypatch):
    registry = load(tmp_path)
    spec = registry.resolve("eighth-chat")
    url, headers, payload = request(registry, spec)
    calls = []

    build_opener = urllib.request.build_opener

    class FixtureHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            calls.append((req.full_url, req.get_header("Authorization")))
            response_headers = Message()
            response_headers["Location"] = "https://evil.example/chat"
            response = urllib.response.addinfourl(
                io.BytesIO(b""), response_headers, req.full_url, 302
            )
            response.msg = "Found"
            return response

    def build(handler):
        # Exercise urllib's actual error/redirect handler chain, replacing only
        # the HTTPS wire handler with a local response fixture.
        return build_opener(handler, FixtureHTTPS())

    monkeypatch.setattr(urllib.request, "build_opener", build)
    status, response_headers, _ = registry.transport(spec, url, headers, payload, 2)
    assert status == 302 and response_headers["Location"] == "https://evil.example/chat"
    assert calls == [(url, "Bearer fixture-secret")]
