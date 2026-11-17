"""Gemma 4 official sync-output schema; no provider calls or retained raw responses."""

import copy
import json
from dataclasses import replace

import pytest

from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
from quota_broker.gateway_providers import ProviderError, interpret, safe_response_diagnostics
from quota_broker.registry import Registry

MODEL = "@cf/google/gemma-4-26b-a4b-it"
OLD = "@cf/meta/llama-3.2-1b-instruct"


def direct(**changes):
    body = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1794844800,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "visible answer", "refusal": None},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
    }
    body.update(changes)
    return body


def encode(body):
    return json.dumps(body).encode()


def typed(raw, model=MODEL):
    spec = replace(Registry.builtin().resolve(model, "cloudflare"), adapter="cloudflare_text")
    return PACKAGED_FAMILY_ADAPTERS["cloudflare_text"].interpret_task(
        spec, "text_generation", 200, {}, raw
    )


@pytest.mark.parametrize(
    "usage", [None, {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}]
)
def test_exact_gemma_direct_chat_returns_text_and_only_explicit_usage(usage):
    body = direct()
    if usage is not None:
        body["usage"] = usage
    raw = encode(body)
    parsed = typed(raw)
    assert parsed.content == {"text": "visible answer"}
    assert parsed.request_id == "chatcmpl-fixture"
    assert parsed.finish_reason == "stop" and parsed.truncated is False
    assert parsed.usage == (
        {} if usage is None else {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    )
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[:4] == (
        "visible answer",
        None if usage is None else 2,
        None if usage is None else 3,
        None,
    )


def test_direct_chat_does_not_expand_legacy_model_wire_schema():
    raw = encode(direct())
    assert interpret("cloudflare", 200, raw, model_id=OLD)[0] is None
    with pytest.raises(ProviderError):
        typed(raw, OLD)


@pytest.mark.parametrize(
    "change",
    [
        {"errors": [{"message": "private error"}]},
        {"error": {"message": "private error"}},
        {"success": False},
        {"result": None},
        {"response": "mixed legacy"},
        {"object": "other"},
        {"created": True},
        {"id": None},
        {"model": None},
        {"choices": []},
    ],
)
def test_direct_errors_mixed_envelopes_and_invalid_required_fields_are_rejected(change):
    raw = encode(direct(**change))
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


@pytest.mark.parametrize("content", [None, "", "   ", ["not a string"]])
def test_direct_null_empty_or_nontext_content_is_never_a_text_result(content):
    body = direct()
    body["choices"][0]["message"]["content"] = content
    raw = encode(body)
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


@pytest.mark.parametrize(
    "message_extra",
    [
        {"refusal": "refused"},
        {"refusal": ""},
        {"tool_calls": [{"id": "private-tool", "type": "function"}]},
        {"function_call": {"name": "private-function", "arguments": "{}"}},
    ],
)
def test_direct_tool_or_refusal_content_is_not_silently_accepted(message_extra):
    body = direct()
    body["choices"][0]["message"].update(message_extra)
    raw = encode(body)
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


def test_wrapped_and_top_level_chat_content_cannot_be_mixed():
    body = {"success": True, "result": direct(), "choices": direct()["choices"]}
    raw = encode(body)
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_fields_fail_closed_for_builtin_and_typed(nested):
    raw = encode(direct())
    if nested:
        raw = raw.replace(
            b'"content": "visible answer"', b'"content": "first", "content": "second"'
        )
    else:
        raw = raw[:-1] + b', "choices": []}'
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


def test_safe_cf_shape_diagnostics_are_fixed_and_never_capture_response_text():
    body = direct()
    body["choices"][0]["message"]["content"] = "private-answer-value"
    details = safe_response_diagnostics(
        "cloudflare",
        200,
        {},
        encode(body),
        requested_model=MODEL,
        sensitive_values=("private-answer-value",),
    )
    assert details["cloudflare_envelope"] == "direct_chat_completion"
    assert details["cloudflare_choice_shape"] == "one"
    assert details["cloudflare_content_shape"] == "text"
    assert details["cloudflare_nontext_message"] is False
    assert details["cloudflare_usage_shape"] == "missing"
    assert "private-answer-value" not in json.dumps(details)
    bad = copy.deepcopy(body)
    bad["choices"][0]["message"]["content"] = None
    details = safe_response_diagnostics("cloudflare", 200, {}, encode(bad))
    assert details["cloudflare_content_shape"] == "null"


@pytest.mark.parametrize(
    "inner_change",
    [
        {"error": {"message": "private nested error"}},
        {"errors": [{"message": "private nested errors"}]},
        {"success": False},
    ],
)
def test_wrapped_nested_error_payload_cannot_supply_an_answer(inner_change):
    raw = encode({"success": True, "result": direct(**inner_change)})
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)


def test_direct_shape_diagnostics_distinguish_invalid_metadata_without_saving_values():
    details = safe_response_diagnostics(
        "cloudflare", 200, {}, encode(direct(created=True)), requested_model=MODEL
    )
    assert details["cloudflare_text_schema_valid"] is False
    good = safe_response_diagnostics("cloudflare", 200, {}, encode(direct()), requested_model=MODEL)
    assert good["cloudflare_text_schema_valid"] is True


@pytest.mark.parametrize("finish_reason", [[], {}])
def test_malformed_finish_reason_remains_a_safe_parse_rejection(finish_reason):
    body = direct()
    body["choices"][0]["finish_reason"] = finish_reason
    raw = encode(body)
    details = safe_response_diagnostics("cloudflare", 200, {}, raw, requested_model=MODEL)
    assert details["cloudflare_text_schema_valid"] is False
    assert interpret("cloudflare", 200, raw, model_id=MODEL)[0] is None
    with pytest.raises(ProviderError):
        typed(raw)
