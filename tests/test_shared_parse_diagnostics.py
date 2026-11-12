"""Offline regressions for shared typed-response failures; no provider I/O."""

import json
from dataclasses import replace

import pytest
from test_family_gateway import catalog
from test_gateway import HMAC_KEY, NOW, target

from quota_broker.gateway import Gateway


@pytest.mark.parametrize(
    "raw, shape",
    [
        (b"<html>private provider error</html>", "non_json"),
        (
            json.dumps({"error": {"code": 502, "message": "private provider error"}}).encode(),
            "nested_error",
        ),
        (
            json.dumps(
                {
                    "error": {"code": 502, "message": "private provider error"},
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "partial response"},
                            "finish_reason": "error",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                }
            ).encode(),
            "nested_error",
        ),
    ],
)
def test_http_200_parse_failure_retains_safe_diagnostics_and_never_replays(tmp_path, raw, shape):
    # Catch dropped diagnostic evidence and acceptance of partial error payloads.
    registry = catalog("openrouter", "openai_inference", "text_generation", "fixture:free")
    item = replace(
        target("chat", "openrouter", "fixture:free"),
        model_info=registry.resolve("fixture:free", "openrouter"),
    )
    calls = []

    def transport(*args):
        calls.append(args)
        return 200, {"content-type": "application/json"}, raw

    gateway = Gateway(
        tmp_path / "parse.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-private-key",
        clock=lambda: NOW,
        registry=registry,
        family_transport=transport,
    )
    task = {
        "request_key": "parse-original",
        "capability": "text_generation",
        "input": {"messages": [{"role": "user", "content": "private input"}]},
        "max_output_tokens": 16,
        "max_attempts": 1,
    }
    outcome = gateway.run(task)
    assert outcome["state"] == outcome["ledger_state"] == "unknown"
    assert outcome["http_status"] == 200
    assert outcome["ledger_basis"] == "held_estimate"
    assert outcome["diagnostics"]["phase"] == "parse_response"
    assert outcome["diagnostics"]["body_shape"] == shape
    assert "result" not in outcome and not outcome.get("answer")
    saved = gateway.status("parse-original")
    assert saved["diagnostics"]["body_shape"] == shape
    for forbidden in (
        "private input",
        "private provider error",
        "partial response",
        "fixture-private-key",
    ):
        assert forbidden not in json.dumps(saved)
    gateway.run(task)
    assert len(calls) == 1
