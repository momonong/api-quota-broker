"""Official native Workers AI schemas and exact catalog transport boundaries."""

import json
from dataclasses import replace

import pytest

from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
from quota_broker.gateway_providers import ProviderError, interpret, official_request, provider_http
from quota_broker.registry import Registry

MODEL = "@cf/google/gemma-4-26b-a4b-it"
OLD = "@cf/meta/llama-3.2-1b-instruct"


def response(result, **outer):
    return json.dumps({"success": True, "errors": [], "result": result, **outer}).encode()


def chat(content="answer", **other):
    return {
        "id": "chat-fixture",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        **other,
    }


def typed(raw):
    spec = replace(Registry.builtin().resolve(MODEL, "cloudflare"), adapter="cloudflare_text")
    return PACKAGED_FAMILY_ADAPTERS["cloudflare_text"].interpret_task(
        spec, "text_generation", 200, {}, raw
    )


@pytest.mark.parametrize("result", [{"response": "answer"}, chat()])
def test_native_legacy_and_chat_schemas_return_text_with_unknown_missing_usage(result):
    raw = response(result)
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[:4] == ("answer", None, None, None)
    parsed = typed(raw)
    assert parsed.content["text"] == "answer"
    assert parsed.usage == {}


def test_native_chat_explicit_usage_and_finish_metadata_are_preserved():
    raw = response(chat(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}))
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[:4] == ("answer", 2, 3, None)
    result = typed(raw)
    assert result.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    assert result.request_id == "chat-fixture"
    assert result.finish_reason == "stop" and result.truncated is False


@pytest.mark.parametrize(
    "result",
    [
        chat(content=None),
        chat(choices=[]),
        chat(choices=[{"message": {"content": "one"}}, {"message": {"content": "two"}}]),
        chat(response="conflicting legacy answer"),
    ],
)
def test_native_chat_ambiguous_or_missing_text_cannot_become_success(result):
    raw = response(result)
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


def test_native_conflicting_usage_is_not_used_for_settlement():
    raw = response(
        chat(usage={"prompt_tokens": 2, "completion_tokens": 3}),
        usage={"prompt_tokens": 100, "completion_tokens": 200},
    )
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[1:4] == (None, None, None)
    assert typed(raw).usage == {}


@pytest.mark.parametrize("model", [MODEL, OLD])
def test_builtin_transport_accepts_only_exact_catalog_cloudflare_paths(monkeypatch, model):
    calls = []

    class Response:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _bound):
            return response({"response": "answer"})

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return Response()

    monkeypatch.setattr(
        "quota_broker.gateway_providers.urllib.request.build_opener", lambda *_args: Opener()
    )
    url, headers, payload = official_request(
        "cloudflare", model, "account_123", "fixture-secret", "input", 32, None, None
    )
    assert provider_http(url, headers, payload, 1)[0] == 200
    for bad in (
        url + "?x=1",
        url + "/extra",
        url.replace(model, "@cf/unknown/model"),
        url.replace("account_123", "../escape"),
    ):
        with pytest.raises(ProviderError, match="unapproved provider URL"):
            provider_http(bad, headers, payload, 1)
    assert calls == [url]


def test_builtin_glm_uses_documented_model_specific_thinking_options():
    _, _, payload = official_request(
        "nvidia", "z-ai/glm-5.3-flash", "account", "fixture-secret", "input", 128, None, None
    )
    assert payload["model"] == "z-ai/glm-5.3-flash"
    assert payload["max_tokens"] == 128
    assert payload["chat_template_kwargs"] == {"clear_thinking": True}


@pytest.mark.parametrize("model", [MODEL, OLD])
def test_builtin_native_requests_explicitly_reject_capacity_queue(model):
    _, _, payload = official_request(
        "cloudflare", model, "a" * 32, "fixture-secret", "input", 32, None, None
    )
    assert payload["options"] == {"rejectIfBusy": True}


def test_typed_native_requests_explicitly_reject_capacity_queue():
    spec = replace(Registry.builtin().resolve(MODEL, "cloudflare"), adapter="cloudflare_text")
    request = PACKAGED_FAMILY_ADAPTERS["cloudflare_text"].request_task(
        spec,
        "a" * 32,
        "fixture-secret",
        {
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": "input"}]},
            "max_output_tokens": 32,
            "options": {},
        },
    )
    assert request.payload["options"] == {"rejectIfBusy": True}


def test_typed_glm_uses_documented_thinking_options_without_reducing_output_budget():
    spec = replace(
        Registry.builtin().resolve("z-ai/glm-5.3-flash", "nvidia"), adapter="openai_inference"
    )
    request = PACKAGED_FAMILY_ADAPTERS["openai_inference"].request_task(
        spec,
        "a" * 32,
        "fixture-secret",
        {
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": "input"}]},
            "max_output_tokens": 128,
            "options": {"reasoning_effort": "high"},
        },
    )
    assert request.payload["chat_template_kwargs"] == {"clear_thinking": True}
    assert request.payload["max_tokens"] == 128
    assert request.payload["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "provider,model,raw",
    [
        (
            "cloudflare",
            MODEL,
            response(chat(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 100})),
        ),
        (
            "nvidia",
            "z-ai/glm-5.3-flash",
            json.dumps(
                chat(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 100})
            ).encode(),
        ),
    ],
)
def test_current_model_conflicting_total_cannot_settle_token_usage(provider, model, raw):
    assert interpret(provider, 200, raw, model_id=model)[1:3] == (None, None)


def test_gemma_neuron_reservation_uses_its_own_published_rates():
    from quota_broker.config import ConfigError, cloudflare_neuron_upper_bound

    assert cloudflare_neuron_upper_bound(1_000_000, 0, model=MODEL) == 9091
    assert cloudflare_neuron_upper_bound(0, 1_000_000, model=MODEL) == 27273
    assert cloudflare_neuron_upper_bound(32768, 4096, model=MODEL) == 410
    with pytest.raises(ConfigError):
        cloudflare_neuron_upper_bound(1, 1, model="@cf/unknown/model")


def test_gemma_formula_cannot_be_used_for_another_model_or_underreserve(tmp_path):
    from quota_broker.config import ConfigError, load_gateway_config

    config = {
        "targets": [
            {
                "id": "cf-gemma",
                "provider": "cloudflare",
                "model": MODEL,
                "account_id": "fixture",
                "secret_ref": "CF_TEST",
                "account_id_ref": "CF_TEST_ACCOUNT",
                "enabled": False,
                "free_eligible": False,
                "billing_enabled": False,
                "verified_at": "2026-11-16T00:00:00Z",
                "expires_at": "2026-11-17T00:00:00Z",
                "source": "fixture",
                "concurrency_limit": 1,
                "max_output_tokens": 4096,
                "provider_quota_facts": [],
                "capacity": {
                    "kind": "unknown",
                    "refresh_seconds": None,
                    "as_of": None,
                    "source": None,
                    "scope": None,
                    "expires_at": None,
                },
                "local_safety_caps": [
                    {
                        "bucket": "cf-rpm",
                        "metric": "requests",
                        "limit": 10,
                        "window": "rolling_minute",
                    },
                    {"bucket": "cf-rpd", "metric": "requests", "limit": 100, "window": "day"},
                    {"bucket": "cf-day", "metric": "neurons", "limit": 10000, "window": "day"},
                ],
                "neuron_estimate": {
                    "amount": 410,
                    "max_input_tokens": 32768,
                    "max_output_tokens": 4096,
                    "source": "cloudflare_gemma_4_26b_formula",
                    "verified_at": "2026-11-16T00:00:00Z",
                    "expires_at": "2026-11-17T00:00:00Z",
                },
            }
        ]
    }
    path = tmp_path / "cf.json"
    path.write_text(json.dumps(config))
    assert load_gateway_config(path)[0].neuron_estimate.amount == 410
    config["targets"][0]["neuron_estimate"]["amount"] = 409
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError, match="conservative bound"):
        load_gateway_config(path)
    config["targets"][0]["neuron_estimate"]["amount"] = 410
    config["targets"][0]["model"] = OLD
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError, match="exact model"):
        load_gateway_config(path)
