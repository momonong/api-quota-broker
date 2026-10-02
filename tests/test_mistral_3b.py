import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, target, task
from test_groq_formal import curl_output, response

from quota_broker import bounded_curl
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import ProviderError, safe_response_diagnostics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_mistral_3b_once as stage


def test_normal_3b_route_is_fixed_plain_chat_and_settles(tmp_path, monkeypatch):
    calls = []

    def http(config, timeout):
        calls.append(config)
        assert timeout == 30
        assert b"api.mistral.ai/v1/chat/completions" in config
        assert b"ministral-3b-latest" in config
        assert b'max_tokens\\":16' in config
        assert b"reasoning_effort" not in config
        return 0, curl_output(response(text="READY", finish="stop"))

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    db = tmp_path / "3b.sqlite"
    gateway = Gateway(
        db,
        (target("3b", "mistral", stage.MODEL),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        clock=lambda: NOW,
    )
    request = task("3b", provider="mistral", model=stage.MODEL)
    result = gateway.run(request)
    assert result["state"] == "completed" and result["ledger_state"] == "completed"
    assert result["finish_reason"] == "stop" and result["reported_input_tokens"] == 11
    assert gateway.run(request)["state"] == "completed" and len(calls) == 1
    assert b"READY" not in db.read_bytes() and b"fixture-secret-value" not in db.read_bytes()
    # A caller cannot change the fixed adapter to another model or add reasoning.
    headers, payload = (
        {"Authorization": "Bearer fixture-secret-value"},
        {
            "model": stage.MODEL,
            "messages": [{"role": "user", "content": "fixture"}],
            "max_tokens": 32,
            "stream": False,
        },
    )
    with pytest.raises(ProviderError, match="unapproved chat payload"):
        bounded_curl.chat_http(
            "https://api.mistral.ai/v1/chat/completions",
            headers,
            {**payload, "reasoning_effort": "high"},
            30,
        )
    with pytest.raises(ProviderError, match="unapproved chat payload"):
        bounded_curl.chat_http(
            "https://api.mistral.ai/v1/chat/completions",
            headers,
            {**payload, "model": "mistral-large-latest"},
            30,
        )
    assert len(calls) == 1


def test_error_ids_are_strict_and_not_reflected_secrets():
    correlation = "12345678-1234-1234-1234-123456789abc"
    headers = {
        "mistral-correlation-id": correlation,
        "x-kong-request-id": correlation,
        "cf-ray": "0123456789abcdef-KHH",
        "set-cookie": "fixture-secret-value",
    }
    details = safe_response_diagnostics("mistral", 429, headers, b"{}")
    assert details["mistral_correlation_id"] == correlation
    assert details["x_kong_request_id"] == correlation
    assert details["cf_ray"] == "0123456789abcdef-KHH"
    details = safe_response_diagnostics(
        "mistral", 429, headers, b"{}", sensitive_values=(correlation,)
    )
    assert "mistral_correlation_id" not in details and "x_kong_request_id" not in details
    assert "fixture-secret-value" not in json.dumps(details)
    assert "mistral_correlation_id" not in safe_response_diagnostics(
        "mistral", 429, {"mistral-correlation-id": "fixture-secret-value"}, b"{}"
    )


def test_plan_is_offline(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_mistral_3b_once.py"])
    monkeypatch.setattr(stage, "service_token", lambda _: 1 / 0)
    assert stage.main() == 0
    assert "3-second gap" in capsys.readouterr().out


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "rate-limit",
        "missing-model",
        "paid-only",
        "no-chat",
        "free-excluded",
        "account-billing",
        "bad-key",
        "get-429",
        "length",
        "timeout",
    ],
)
def test_one_get_gates_one_default_post_with_gap_and_no_replay(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    old = [tmp_path / f"old{i}.sqlite" for i in range(4)]
    for path in old:
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT,dispatched_at TEXT)")
            con.execute("INSERT INTO gateway_attempts VALUES('mistral','fixture')")
    with sqlite3.connect(old[-1]) as con:
        con.execute("CREATE TABLE acceptance_receipt(details_json TEXT)")
        prior = {
            "model": "mistral-small-latest",
            "http_status": 429,
            "diagnostics": {"provider_error_code": 1300},
            "completed_at": "2026-10-01T00:00:00+00:00",
        }
        con.execute("INSERT INTO acceptance_receipt VALUES(?)", (json.dumps(prior),))
    original = [p.read_bytes() for p in old]
    monkeypatch.setattr(stage, "ALL_PRIOR", old[:2])
    monkeypatch.setattr(stage, "FIRST_STAGE_DB", old[2])
    monkeypatch.setattr(stage, "ISOLATED_DB", old[3])
    db = tmp_path / "3b-new.sqlite"
    monkeypatch.setattr(stage, "DB", db)
    monkeypatch.setattr(sys, "argv", ["v1_mistral_3b_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(stage, "cli", lambda: "fixture")
    monkeypatch.setattr(stage, "metadata_names", lambda _: {"MISTRAL_API_KEY"})
    tokens, reads, calls, pauses = [], [], [], []
    monkeypatch.setattr(stage, "service_token", lambda _: tokens.append(1) or "fixture-token")
    monkeypatch.setattr(stage.time, "sleep", lambda seconds: pauses.append(seconds))

    def resolver(name):
        reads.append(name)
        return "bad\nkey" if scenario == "bad-key" else "fixture-secret-value"

    monkeypatch.setattr(stage, "doppler_resolver_from_token", lambda *_: resolver)

    def http(config, timeout):
        is_get = b'request = "GET"' in config
        calls.append(is_get)
        assert timeout == 30 and b"api.mistral.ai" in config
        if is_get:
            if scenario == "get-429":
                return 0, curl_output(
                    {
                        "object": "error",
                        "type": "rate_limited",
                        "code": "1300",
                        "message": "Rate limit exceeded.",
                    },
                    429,
                )
            model = {
                "id": "ministral-3b-2512",
                "aliases": [stage.MODEL],
                "capabilities": {"completion_chat": scenario != "no-chat"},
            }
            if scenario == "missing-model":
                model["aliases"] = ["other-model"]
            if scenario == "paid-only":
                model["requires_payment"] = True
            if scenario == "free-excluded":
                model["free_eligible"] = False
            return 0, curl_output(
                {"data": [model], "billing_enabled": scenario == "account-billing"}
            )
        assert pauses == [3] and b"ministral-3b-latest" in config
        assert b'max_tokens\\":32' in config and b"reasoning_effort" not in config
        body = response(text="READY", finish="length" if scenario == "length" else "stop")
        if scenario == "rate-limit":
            body = {
                "object": "error",
                "type": "rate_limited",
                "code": "1300",
                "message": "Rate limit exceeded.",
            }
        return (28 if scenario == "timeout" else 0), curl_output(
            body, 429 if scenario == "rate-limit" else 200, 28 if scenario == "timeout" else 0
        )

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    assert stage.main() == (0 if scenario == "success" else 2)
    assert tokens == [1] and reads == ["MISTRAL_API_KEY"]
    gated = scenario not in (
        "missing-model",
        "paid-only",
        "free-excluded",
        "account-billing",
        "no-chat",
        "bad-key",
        "get-429",
    )
    assert calls == ([True, False] if gated else [] if scenario == "bad-key" else [True])
    assert pauses == ([3] if gated else [])
    assert original == [p.read_bytes() for p in old]
    output = capsys.readouterr().out
    for forbidden in ("fixture-secret-value", "fixture-token", "READY", stage.PROMPT):
        assert forbidden not in output and forbidden.encode() not in db.read_bytes()
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM diagnostic_gets").fetchone()[0] == 1
        receipt = con.execute("SELECT details_json FROM acceptance_receipt").fetchall()
        assert len(receipt) == int(gated)
        if gated:
            saved = json.loads(receipt[0][0])
            assert saved["full_answer_verified"] is (scenario == "success")
            if scenario == "rate-limit":
                assert saved["diagnostics"]["provider_error_code"] == 1300
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    assert db.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="never replay"):
        stage.main()
    assert tokens == [1] and calls == (
        [True, False] if gated else [] if scenario == "bad-key" else [True]
    )
