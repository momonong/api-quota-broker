"""Production shared-route wire contracts; fixtures only, no provider I/O."""

import json

import pytest
from test_family_adapters import profile, specification, task, text_input
from test_family_gateway import catalog

from quota_broker.family_transport import validate_family_request
from quota_broker.gateway_providers import ProviderError, official_request
from quota_broker.registry import Registry


def responses_body(**changes):
    return {
        "id": "resp_fixture",
        "object": "response",
        "status": "completed",
        "model": "gpt-6-luna",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "42", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 19, "output_tokens": 5, "total_tokens": 24},
        **changes,
    }


def test_native_responses_registry_admission_and_fixed_wire():
    registry = catalog("openai", "openai_responses", "text_generation", "gpt-6-luna")
    spec = registry.resolve("gpt-6-luna", "openai")
    data = task("text_generation", text_input())
    request = registry.request_task(spec, "personal", "fixture-secret", data)
    validate_family_request(request)
    assert request.url == "https://api.openai.com/v1/responses"
    assert request.payload == {
        "model": "gpt-6-luna",
        "input": text_input()["messages"],
        "max_output_tokens": 100,
        "store": False,
        "stream": False,
        "reasoning": {"effort": "none"},
        "service_tier": "default",
    }
    result = registry.interpret_task(
        spec, "text_generation", 200, {}, json.dumps(responses_body()).encode()
    )
    assert result.content["text"] == "42"
    assert result.usage == {"input_tokens": 19, "output_tokens": 5, "total_tokens": 24}
    assert result.finish_reason == "stop" and result.truncated is False
    assert result.request_id == "resp_fixture"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "in_progress"},
        {"status": "failed"},
        {"status": "unknown"},
        {"output": [{"type": "function_call", "name": "unexpected"}]},
        {"usage": {"input_tokens": 19, "output_tokens": 5, "total_tokens": 99}},
    ],
)
def test_responses_ambiguous_or_invalid_reply_is_not_success(changes):
    adapter = profile("openai_responses")
    spec = specification("openai_responses", "openai", "text_generation")
    with pytest.raises(ProviderError):
        adapter.interpret_task(
            spec, "text_generation", 200, {}, json.dumps(responses_body(**changes)).encode()
        )


def test_responses_output_limit_preserves_partial_content_and_truncation():
    adapter = profile("openai_responses")
    spec = specification("openai_responses", "openai", "text_generation")
    body = responses_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    body["output"][0]["status"] = "incomplete"
    result = adapter.interpret_task(spec, "text_generation", 200, {}, json.dumps(body).encode())
    assert result.content["text"] == "42"
    assert result.finish_reason == "length" and result.truncated is True


def test_responses_no_usage_is_unknown_not_zero():
    adapter = profile("openai_responses")
    spec = specification("openai_responses", "openai", "text_generation")
    result = adapter.interpret_task(
        spec, "text_generation", 200, {}, json.dumps(responses_body(usage=None)).encode()
    )
    assert result.usage == {}


@pytest.mark.parametrize("maximum", [1, 15])
def test_responses_rejects_below_provider_minimum_before_io(maximum):
    adapter = profile("openai_responses")
    spec = specification("openai_responses", "openai", "text_generation")
    data = {**task("text_generation", text_input()), "max_output_tokens": maximum}
    with pytest.raises(ValueError):
        adapter.admit_task(spec, data)


def test_all_packaged_openrouter_chat_paths_enforce_zero_price():
    expected = {"allow_fallbacks": False, "max_price": {"prompt": 0, "completion": 0, "request": 0}}
    adapter = profile("openai_inference")
    spec = specification("openai_inference", "openrouter", "text_generation")
    request = adapter.request_task(
        spec, "account", "fixture-secret", task("text_generation", text_input())
    )
    assert request.payload["provider"] == expected
    builtin = next(s for s in Registry.builtin().models.values() if s.provider == "openrouter")
    _url, _headers, payload = official_request(
        "openrouter", builtin.model, "account", "fixture-secret", "hello", 64, None, None
    )
    assert payload["provider"] == expected
