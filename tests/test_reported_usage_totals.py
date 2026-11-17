"""Only explicit provider totals establish complete token accounting."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quota_broker.registry import Registry, RegistryError


def registered(provider="google", adapter=None, custom=None):
    base = Registry.builtin()
    spec = next(s for s in base.models.values() if s.provider == provider)
    if adapter:
        spec = replace(spec, adapter=adapter)
    registry = Registry({(spec.provider, spec.model): spec}, adapters=custom)
    return registry, spec


def encoded(usage, provider="google"):
    return json.dumps({"usageMetadata" if provider == "google" else "usage": usage}).encode()


@pytest.mark.parametrize("adapter", [None, "gemini_generate_content"])
def test_google_preserves_explicit_total_including_thinking_without_changing_tuple(adapter):
    registry, spec = registered(adapter=adapter)
    raw = encoded(
        {
            "promptTokenCount": 11,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 19,
        }
    )
    assert registry.reported_usage(spec, 200, raw) == {
        "input_tokens": 11,
        "output_tokens": 3,
        "total_tokens": 19,
    }
    assert len(registry.interpret(spec, 200, raw)) == 5


@pytest.mark.parametrize("adapter", [None, "gemini_generate_content"])
def test_missing_total_remains_missing_even_when_thoughts_and_pair_are_known(adapter):
    registry, spec = registered(adapter=adapter)
    assert registry.reported_usage(
        spec,
        200,
        encoded(
            {
                "promptTokenCount": 11,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 5,
            }
        ),
    ) == {"input_tokens": 11, "output_tokens": 3}


@pytest.mark.parametrize("bad", [-1, True, 1.5, "19", 10**12 + 1])
def test_invalid_explicit_total_never_falls_back_to_known_pair(bad):
    registry, spec = registered()
    with pytest.raises(RegistryError):
        registry.reported_usage(
            spec,
            200,
            encoded(
                {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": bad,
                }
            ),
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"usageMetadata":{"totalTokenCount":14,"totalTokenCount":19}}',
        b'{"usageMetadata":{"totalTokenCount":14},"usageMetadata":{"totalTokenCount":19}}',
        b'{"usageMetadata":{"promptTokenCount":11,"candidatesTokenCount":3,"totalTokenCount":13}}',
        b'{"usageMetadata":[]}',
        b'{"usageMetadata":{"thoughtsTokenCount":true}}',
        b'{"usageMetadata":{"totalTokenCount":NaN}}',
        b"not json",
    ],
)
def test_ambiguous_or_contradictory_json_cannot_report_a_total(raw):
    registry, spec = registered()
    with pytest.raises(RegistryError):
        registry.reported_usage(spec, 200, raw)


@pytest.mark.parametrize(
    "provider,adapter",
    [
        ("nvidia", None),
        ("groq", None),
        ("mistral", None),
        ("openrouter", None),
        ("nvidia", "openai_chat"),
    ],
)
def test_chat_adapters_preserve_only_explicit_consistent_total(provider, adapter):
    registry, spec = registered(provider, adapter)
    raw = encoded({"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}, provider)
    assert registry.reported_usage(spec, 200, raw) == {
        "input_tokens": 11,
        "output_tokens": 3,
        "total_tokens": 14,
    }
    with pytest.raises(RegistryError):
        registry.reported_usage(
            spec,
            200,
            encoded(
                {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "total_tokens": 19,
                },
                provider,
            ),
        )


@pytest.mark.parametrize("adapter", [None, "cloudflare_workers_ai"])
def test_cloudflare_nested_usage_and_conflicting_outer_observation(adapter):
    registry, spec = registered("cloudflare", adapter)
    usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14, "neurons": 7}
    raw = json.dumps({"success": True, "result": {"usage": usage}}).encode()
    assert registry.reported_usage(spec, 200, raw) == {
        "input_tokens": 11,
        "output_tokens": 3,
        "total_tokens": 14,
        "neurons": 7,
    }
    with pytest.raises(RegistryError):
        registry.reported_usage(
            spec,
            200,
            json.dumps(
                {
                    "usage": {**usage, "total_tokens": 15},
                    "result": {"usage": usage},
                }
            ).encode(),
        )


def unused(*args, **kwargs):
    raise AssertionError("numeric usage extraction must not request or transport")


def test_custom_adapter_without_hook_never_inherits_provider_schema():
    custom = SimpleNamespace(request=unused, transport=unused, interpret=unused)
    registry, spec = registered(adapter="custom", custom={"custom": custom})
    assert registry.reported_usage(spec, 200, encoded({"totalTokenCount": 19})) == {}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"secret": "not public"},
        {"total_tokens": True},
        {"total_tokens": -1},
        {"total_tokens": 10**12 + 1},
        {"total_tokens": 0.1},
    ],
)
def test_registry_validates_custom_hook_numeric_schema(value):
    custom = SimpleNamespace(
        request=unused, transport=unused, interpret=unused, reported_usage=lambda *args: value
    )
    registry, spec = registered(adapter="custom", custom={"custom": custom})
    with pytest.raises(RegistryError):
        registry.reported_usage(spec, 200, b"{}")


def test_error_or_missing_usage_does_not_fabricate_resources():
    registry, spec = registered()
    assert registry.reported_usage(spec, 200, b"{}") == {}
    assert registry.reported_usage(spec, 429, b"not a usage response") == {}
    assert registry.reported_usage(spec, 200, encoded({"totalTokenCount": 0})) == {
        "total_tokens": 0
    }


def test_typed_google_rejects_total_smaller_than_known_thinking_usage():
    from quota_broker.family_adapters import _usage
    from quota_broker.gateway_providers import ProviderError

    with pytest.raises(ProviderError):
        _usage(
            {
                "promptTokenCount": 11,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 14,
            }
        )
    assert (
        _usage(
            {
                "promptTokenCount": 11,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 19,
            }
        )["total_tokens"]
        == 19
    )
