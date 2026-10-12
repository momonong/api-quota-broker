"""Google PDF/schema admission and queue contracts; all provider I/O is injected."""

import copy
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_families import pdf_media
from test_framework import QUEUE_KEY, TOKEN, close, serve
from test_gateway import HMAC_KEY, NOW, fixture_transport, target

from quota_broker.client import ClientError, _json_http
from quota_broker.config import ResourceEstimate
from quota_broker.families import MediaLimits
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.queue import DurableQueue
from quota_broker.registry import Registry, RegistryError

MODEL = "gemini-3.5-flash-lite"
MANAGEMENT_FIELDS = (
    "orderNo",
    "invoiceNo",
    "client",
    "product",
    "code",
    "qty",
    "unitPrice",
    "amount",
    "currency",
    "date",
    "incoterms",
    "unit",
)
SCHEMAS = [
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {field: {"type": "string"} for field in MANAGEMENT_FIELDS},
                },
            }
        },
        "required": ["items"],
    },
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["description", "quantity"],
                },
            }
        },
        "required": ["items"],
    },
]


def pdf_task(schema=None):
    return {
        "request_key": "of-fixture-pdf-001",
        "capability": "text_generation",
        "provider": "google",
        "model": MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract line items. Reply in JSON only."},
                        pdf_media(),
                    ],
                }
            ]
        },
        "options": {"json_schema": copy.deepcopy(schema or SCHEMAS[0])},
        "requirements": {"features": ["document_input", "structured_output"]},
        "max_output_tokens": 2048,
        "max_attempts": 1,
        "wait_policy": "reject",
    }


def build(tmp_path, *, estimate=True, expired=False, finish="STOP", usage=True, timeout=False):
    tmp_path.chmod(0o700)
    db = tmp_path / "queue.sqlite"
    db.touch(mode=0o600)
    # This bound is deliberately fixture-only, not a proposed PDF tokenizer or
    # production estimate. Production PDF admission stays closed without evidence.
    bounds = (
        (
            ResourceEstimate(
                "input_tokens",
                4096,
                2 * 1024 * 1024,
                2048,
                "fixture-only trusted bound",
                NOW - timedelta(hours=1),
                NOW + timedelta(seconds=-1 if expired else 60),
            ),
        )
        if estimate
        else ()
    )
    item = replace(
        target("google-pdf", "google", MODEL), max_output_tokens=2048, resource_estimates=bounds
    )
    family_calls, legacy_calls, secret_lookups = [], [], []

    def resolve(_):
        secret_lookups.append(True)
        return "fixture-provider-credential"

    def transport(request, timeout_seconds, bound):
        family_calls.append(request)
        if timeout:
            raise TimeoutError("fixture uncertain execution")
        body = {
            "candidates": [
                {"content": {"parts": [{"text": '{"items":[]}'}]}, "finishReason": finish}
            ]
        }
        if usage:
            body["usageMetadata"] = {"promptTokenCount": 300, "candidatesTokenCount": 6}
        return 200, {}, json.dumps(body).encode()

    gateway = Gateway(
        db,
        (item,),
        HMAC_KEY,
        resolve,
        fixture_transport(legacy_calls),
        clock=lambda: NOW,
        family_transport=transport,
        media_limits=MediaLimits(1048576, 2097152, 4194304),
    )
    return gateway, family_calls, legacy_calls, secret_lookups


def test_only_verified_google_model_gains_pdf_and_schema_features():
    registry = Registry.builtin()
    selected = registry.resolve(MODEL, "google")
    assert selected.adapter == "builtin:google"
    assert set(selected.features) == {
        "text_generation",
        "text",
        "document_input",
        "structured_output",
    }
    assert registry.resolve("gemini-2.5-flash-lite", "google").features == (
        "text_generation",
        "text",
    )
    with pytest.raises(RegistryError):
        registry.resolve("gemini-3.1-flash-lite", "google")


@pytest.mark.parametrize("schema", SCHEMAS)
def test_pdf_schema_through_authenticated_queue_and_repeat_result(tmp_path, schema):
    gateway, calls, _, secrets = build(tmp_path)
    queue = DurableQueue(gateway, QUEUE_KEY)
    server, thread, base = serve(gateway, queue)
    headers = {"Authorization": "Bearer " + TOKEN}
    task = pdf_task(schema)
    path = base + "/v1/queue/" + task["request_key"]
    try:
        plan = _json_http(base + "/v1/routes/explain", task, headers)
        assert plan["selected_target_id"] == "google-pdf"
        assert calls == secrets == []
        submitted = _json_http(base + "/v1/queue", task, headers)
        assert submitted["state"] == "queued" and submitted["result_available"] is False
        with pytest.raises(ClientError) as pending:
            _json_http(path + "/result", None, headers)
        assert pending.value.detail["error"] == "queue_result_pending"
        queue.tick("fixture-worker")
        status = _json_http(path, None, headers)
        result = _json_http(path + "/result", None, headers)
        assert status["state"] == status["gateway_state"] == result["state"] == "completed"
        assert result["request_key"] == status["execution_key"] != task["request_key"]
        assert result["result"]["text"] == '{"items":[]}'
        assert result["finish_reason"] == "stop" and result["response_truncated"] is False
        assert result["reported_input_tokens"] == 300
        assert result["ledger_basis"] == "settled_provider_usage"
        assert _json_http(path + "/result", None, headers) == result
        assert _json_http(base + "/v1/queue", task, headers)["state"] == "completed"
        assert queue.tick("fixture-worker") is None
        assert len(calls) == len(secrets) == 1
        wire = calls[0].payload
        assert wire["generationConfig"] == {
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        }
        media = wire["contents"][0]["parts"][1]["inlineData"]
        assert media == {
            "mimeType": "application/pdf",
            "data": task["input"]["messages"][0]["content"][1]["data"],
        }
        assert "answer" not in result
    finally:
        close(server, thread)


@pytest.mark.parametrize("estimate,expired", [(False, False), (True, True)])
def test_pdf_without_current_trusted_token_bound_never_resolves_or_sends(
    tmp_path, estimate, expired
):
    gateway, calls, _, secrets = build(tmp_path, estimate=estimate, expired=expired)
    plan = gateway.explain(pdf_task())
    assert plan["selected_target_id"] is None
    with pytest.raises(GatewayError):
        gateway.run(pdf_task())
    assert calls == secrets == []


def test_old_google_string_path_stays_usable(tmp_path):
    gateway, family, legacy, _ = build(tmp_path, estimate=False)
    result = gateway.run(
        {
            "request_key": "legacy",
            "capability": "text_generation",
            "provider": "google",
            "model": MODEL,
            "input": "Reply OK.",
            "max_output_tokens": 32,
            "max_attempts": 1,
        }
    )
    assert result["state"] == "completed" and result["answer"] == "fixture answer"
    assert len(legacy) == 1 and not family


@pytest.mark.parametrize(
    "finish,usage,expected,truncated",
    [
        ("MAX_TOKENS", True, "completed", True),
        ("STOP", False, "completed_usage_unknown", False),
    ],
)
def test_content_completion_and_usage_are_independent(tmp_path, finish, usage, expected, truncated):
    gateway, calls, _, _ = build(tmp_path, finish=finish, usage=usage)
    queue = DurableQueue(gateway, QUEUE_KEY)
    task = pdf_task()
    queue.submit(task)
    queue.tick("fixture-worker")
    assert queue.status(task["request_key"])["gateway_state"] == expected
    result = queue.result(task["request_key"])
    assert result["state"] == expected and result["response_truncated"] is truncated
    assert result["result"]["text"] == '{"items":[]}'
    assert queue.tick("fixture-worker") is None and len(calls) == 1


def test_uncertain_pdf_execution_never_automatically_replays(tmp_path):
    gateway, calls, _, _ = build(tmp_path, timeout=True)
    queue = DurableQueue(gateway, QUEUE_KEY)
    task = pdf_task()
    queue.submit(task)
    queue.tick("fixture-worker")
    assert queue.status(task["request_key"])["state"] == "unknown"
    queue.submit(task)
    assert queue.tick("fixture-worker") is None and len(calls) == 1


@pytest.mark.parametrize("invalid", ["upper_schema", "media_limit", "caller_bound"])
def test_invalid_pdf_contract_rejected_before_dispatch(tmp_path, invalid):
    gateway, calls, _, secrets = build(tmp_path)
    task = pdf_task()
    if invalid == "upper_schema":
        task["options"]["json_schema"]["type"] = "OBJECT"
    elif invalid == "media_limit":
        gateway.media_limits = MediaLimits(decoded_input_bytes=32)
    else:
        task["resource_bounds"] = {"input_tokens": 1}
    with pytest.raises(GatewayError):
        gateway.run(task)
    assert calls == secrets == []
