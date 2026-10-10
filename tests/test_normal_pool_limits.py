"""Usable normal-pool envelope, transparent refusal, and preserved r2 holds."""

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from test_asus_seven_pool import load

from quota_broker import cli
from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError, validate_task

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 8, 6, tzinfo=UTC)
normal, r2, base = map(load, ("normal_pool_plan", "seven_pool_plan", "provider_pool_plan"))
POLICY = json.loads((ROOT / "tests/fixtures/asus-history/seven-r2-admission.json").read_text())


def configured(tmp_path, *, normal_limits=True, transport=None, admitted=None):
    original = r2.config(
        base,
        admitted or set(r2.PROVIDERS),
        POLICY["admission"],
        "2026-11-02T03:36:17+00:00",
        normal=True,
        now=NOW,
    )
    value = normal.config(original) if normal_limits else original
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    gateway = Gateway(
        tmp_path / "db.sqlite3",
        load_gateway_config(path),
        b"public-fixture-hmac-at-least-32-bytes",
        lambda ref: "f" * 32 if "ACCOUNT" in ref else "public-fixture",
        transport,
        clock=lambda: NOW,
    )
    return gateway, original, value


def body(key="unique", **changes):
    return {
        "request_key": key,
        "capability": "text_generation",
        "input": "Public fixture.",
        "max_attempts": 1,
        "wait_policy": "reject",
        **changes,
    }


def test_profiles_preserve_identity_evidence_and_disabled_targets(tmp_path):
    _, before, after = configured(tmp_path, admitted={"groq", "cloudflare"})
    for old, new in zip(before["targets"], after["targets"], strict=True):
        for field in (
            "id",
            "provider",
            "model",
            "account_id",
            "secret_ref",
            "enabled",
            "free_eligible",
            "billing_enabled",
            "verified_at",
            "expires_at",
            "shared_concurrency_scope",
            "shared_concurrency_limit",
        ):
            assert new[field] == old[field]
        assert [x["bucket"] for x in new["local_safety_caps"]] == [
            x["bucket"] for x in old["local_safety_caps"]
        ]
    profiles = {x["provider"]: x for x in after["targets"]}
    assert profiles["google"]["max_output_tokens"] == 8192
    assert profiles["cloudflare"]["max_output_tokens"] == 2048
    assert profiles["ocrspace"]["max_output_tokens"] == 1
    assert profiles["cloudflare"]["neuron_estimate"]["max_input_tokens"] == 32768
    assert profiles["cloudflare"]["neuron_estimate"]["amount"] > 26
    assert before == r2.config(
        base,
        {"groq", "cloudflare"},
        POLICY["admission"],
        "2026-11-02T03:36:17+00:00",
        normal=True,
        now=NOW,
    )


def test_default_and_context_input_output_refusal_are_explicit(tmp_path):
    gateway, _, _ = configured(tmp_path)
    assert validate_task(body())["max_output_tokens"] == 1024
    assert (
        validate_task(body(input={"messages": [{"role": "user", "content": "Hello"}]}))[
            "max_output_tokens"
        ]
        == 1024
    )
    plan = gateway.explain(body(input="文" * 600, max_output_tokens=2048))
    assert plan["selected_target_id"] is not None  # Far beyond old 960-byte envelope.
    cf = next(x for x in plan["candidates"] if x["provider"] == "cloudflare")
    assert cf["request_limits"]["max_output_tokens"] == 2048
    assert cf["request_limits"]["max_legacy_input_bytes_for_requested_output"] == 8128
    with pytest.raises(GatewayError) as raised:
        gateway.run(body(provider="cloudflare", max_output_tokens=2049))
    details = raised.value.details["candidates"]
    assert "output_limit" in next(x for x in details if x["provider"] == "cloudflare")["reasons"]
    over_input = gateway.explain(
        body(provider="cloudflare", input="x" * 9000, max_output_tokens=1024)
    )
    assert over_input["selected_target_id"] is None
    row = next(x for x in over_input["candidates"] if x["provider"] == "cloudflare")
    assert any(x.startswith("local_cap:") for x in row["reasons"])
    assert "estimation_unconfigured" in row["reasons"]
    context = gateway.explain(
        body(provider="openrouter", input="x" * 16384, max_output_tokens=4096)
    )
    assert (
        "context_limit"
        in next(x for x in context["candidates"] if x["provider"] == "openrouter")["reasons"]
    )


def test_long_answer_and_partial_finish_preserve_truth_and_no_replay(tmp_path):
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        assert payload["max_completion_tokens"] == 2048
        return (
            200,
            {},
            json.dumps(
                {
                    "model": "openai/gpt-oss-20b",
                    "choices": [
                        {
                            "message": {"content": "public long answer " * 1500},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 400, "completion_tokens": 2048},
                }
            ).encode(),
        )

    gateway, _, _ = configured(tmp_path, transport=transport)
    request = body(provider="groq", input="Public prompt " * 200, max_output_tokens=2048)
    result = gateway.run(request)
    assert len(result["answer"]) > 64
    assert result["finish_reason"] == "length" and result["response_truncated"] is True
    assert result["reported_output_tokens"] == 2048
    assert gateway.run(request)["response_truncated"] is True
    assert len(calls) == 1
    assert "public long answer" not in (tmp_path / "db.sqlite3").read_bytes().decode(
        errors="ignore"
    )


def test_r2_cloudflare_holds_survive_config_upgrade_and_do_not_pin_concurrency(tmp_path):
    def transport(*args):
        return (
            200,
            {},
            b'{"success":true,"result":{"response":"public answer","usage":{"prompt_tokens":49,"completion_tokens":8}}}',
        )

    old, _, _ = configured(tmp_path, normal_limits=False, transport=transport)
    for key in ("preserve-r2-cf-1", "preserve-r2-cf-2"):
        assert (
            old.run(body(key, provider="cloudflare", max_output_tokens=64))["state"]
            == "completed_usage_unknown"
        )
    held = old.usage(provider="cloudflare")[0]["ledger_neurons"]
    new, _, _ = configured(tmp_path, transport=transport)
    assert new.usage(provider="cloudflare")[0]["ledger_neurons"] == held == 26
    assert (
        new.explain(body(provider="cloudflare", max_output_tokens=2048))["selected_target_id"]
        is not None
    )


def test_cli_default_output_and_prompt_stdin_are_separate(tmp_path, monkeypatch, capsys):
    token = tmp_path / "public-fixture-client"
    token.write_text("public-fixture-not-secret-32-characters")

    def http(url, value, headers, timeout):
        assert value["max_output_tokens"] is None and value["input"] == "Public prompt"
        return {"state": "public_fixture"}

    monkeypatch.setattr(cli, "_json_http", http)
    with (
        patch.object(
            sys,
            "argv",
            [
                "quota-broker",
                "gateway",
                "--token-file",
                str(token),
                "run",
                "--request-key",
                "new-key",
                "--capability",
                "text_generation",
            ],
        ),
        patch.object(sys, "stdin", io.StringIO("Public prompt")),
    ):
        cli.main()
