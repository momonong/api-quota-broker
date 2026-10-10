import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, target, task
from test_groq_formal import curl_output, response

from quota_broker import bounded_curl
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import safe_response_diagnostics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_mistral_isolated_once as stage


def test_numeric_code_and_error_message_survive_without_arbitrary_prose():
    body = {
        "object": "error",
        "type": "rate_limit_exceeded",
        "param": None,
        "code": "1300",
        "message": "Rate limit exceeded.",
    }
    details = safe_response_diagnostics(
        "mistral", 429, {"X-RateLimit-Remaining-Tokens": "0"}, json.dumps(body).encode()
    )
    assert details["provider_error_code"] == 1300
    assert details["provider_error_type"] == "rate_limit_exceeded"
    assert details["provider_error_object"] == "error"
    assert details["provider_error_param"] is None
    assert details["provider_error_message_safe"] == "rate limit exceeded"
    assert details["provider_error_message_redacted"] is False
    assert details["x_ratelimit_remaining_tokens"] == 0
    assert details["error_code"] == "mistral_rate_limit_error"


@pytest.mark.parametrize("code", ["unknown_model", 1300, "1300", None, True, 100000, "opaque-key"])
def test_bounded_code_values_and_sensitive_reflection(code):
    body = {"object": "error", "code": code, "message": "Rate limit exceeded."}
    details = safe_response_diagnostics("mistral", 429, {}, json.dumps(body).encode())
    expected = code if type(code) is int and code == 1300 else 1300 if code == "1300" else None
    if code == "unknown_model":
        expected = code
    assert details["provider_error_code"] == expected
    if code is not None:
        sensitive = safe_response_diagnostics(
            "mistral", 429, {}, json.dumps(body).encode(), sensitive_values=(str(code),)
        )
        assert sensitive["provider_error_code"] is None


def test_unseen_credentials_pii_input_and_answer_echo_are_removed():
    message = (
        "Rate limit exceeded for workspace jane@example.com org-abcdefghijklmnop. "
        "Bearer unseenCredential123 https://example.com/account/secret "
        '"fixture input" "fixture answer" +886-912-345-678 fixture-secret-value'
    )
    body = {
        "object": "error",
        "type": "fixture-secret-value",
        "code": "fixture-secret-value",
        "message": message,
    }
    details = safe_response_diagnostics(
        "mistral",
        429,
        {"X-RateLimit-Limit-Requests": "12", "X-RateLimit-Remaining": "fixture-secret-value"},
        json.dumps(body).encode(),
        sensitive_values=("fixture-secret-value", "fixture input"),
    )
    serialized = json.dumps(details)
    for forbidden in (
        "jane",
        "example.com",
        "org-",
        "unseenCredential",
        "fixture input",
        "fixture answer",
        "886",
        "fixture-secret-value",
    ):
        assert forbidden not in serialized
    assert details["provider_error_message_redacted"] is True
    assert details["provider_error_type"] is None
    assert details["provider_error_code"] is None
    assert details["x_ratelimit_limit_requests"] == 12
    assert "x_ratelimit_remaining" not in details


def test_message_bounds_and_error_only_capture():
    raw = json.dumps({"object": "error", "message": "Rate limit exceeded " * 300}).encode()
    details = safe_response_diagnostics("mistral", 429, {}, raw)
    assert details["provider_error_message_safe"] == "[omitted: exceeds bound]"
    raw = json.dumps({"object": "error", "message": "Rate limit exceeded " * 30}).encode()
    details = safe_response_diagnostics("mistral", 429, {}, raw)
    assert len(details["provider_error_message_safe"]) <= 256
    assert details["provider_error_message_redacted"] is True
    assert "provider_error_message_safe" not in safe_response_diagnostics("mistral", 200, {}, raw)
    details = safe_response_diagnostics(
        "mistral", 429, {"X-RateLimit-Limit": "12", "X-RateLimit-Reset": "malicious"}, b"not json"
    )
    assert details["x_ratelimit_limit"] == 12 and "x_ratelimit_reset" not in details


def test_fixed_request_bucket_headers_are_numeric_bounded_and_secret_filtered():
    headers = {
        "X-RateLimit-Limit-Requests-Minute": "12",
        "X-RateLimit-Remaining-Requests-Day": "0",
        "X-RateLimit-Reset-Requests-Second": "1",
        "X-RateLimit-Limit-Req-Minute": "4",
        "X-RateLimit-Remaining-Req-10-Second": "0",
        "X-RateLimit-Unknown": "123",
        "X-RateLimit-Limit-Tokens": "12345678901",
        "X-RateLimit-Remaining-Tokens": "123",
    }
    details = safe_response_diagnostics("mistral", 429, headers, b"{}", sensitive_values=("123",))
    assert details["x_ratelimit_limit_requests_minute"] == 12
    assert details["x_ratelimit_remaining_requests_day"] == 0
    assert details["x_ratelimit_reset_requests_second"] == 1
    assert details["x_ratelimit_limit_req_minute"] == 4
    assert details["x_ratelimit_remaining_req_10_second"] == 0
    assert "x_ratelimit_unknown" not in details
    assert "x_ratelimit_limit_tokens" not in details
    assert "x_ratelimit_remaining_tokens" not in details


def test_gateway_passes_input_for_redaction_and_never_replays(tmp_path, monkeypatch):
    calls = []
    body = {
        "object": "error",
        "type": "rate_limit_exceeded",
        "code": 1300,
        "message": "Rate limit exceeded",
    }

    def http(config, timeout):
        calls.append(config)
        assert timeout == 30 and b'reasoning_effort\\":\\"none' in config
        return 0, curl_output(body, 429)

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    db = tmp_path / "fixture.sqlite"
    gateway = Gateway(
        db,
        (target("mistral", "mistral", stage.MODEL),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        clock=lambda: NOW,
    )
    request = {
        **task("redaction", provider="mistral", model=stage.MODEL),
        "input": "Rate limit exceeded",
    }
    result = gateway.run(request)
    assert result["diagnostics"]["provider_error_message_safe"] == "[redacted]"
    assert result["diagnostics"]["provider_error_code"] == 1300
    assert result["state"] == "unknown" and result["ledger_state"] == "unknown"
    assert gateway.run(request)["state"] == "unknown" and len(calls) == 1
    assert b"Rate limit exceeded" not in db.read_bytes()


def test_plan_is_offline(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_mistral_isolated_once.py"])
    monkeypatch.setattr(stage, "service_token", lambda _: 1 / 0)
    assert stage.main() == 0
    assert "no GET" in capsys.readouterr().out


@pytest.mark.parametrize("scenario", ["success", "rate-limit", "length", "bad-key", "timeout"])
def test_isolated_once_uses_default_route_and_preserves_old_receipts(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    paths = [tmp_path / f"old{i}.sqlite" for i in range(3)]
    for path in paths:
        with sqlite3.connect(path) as con:
            con.execute(
                "CREATE TABLE gateway_attempts(provider TEXT,dispatched_at TEXT,completed_at TEXT)"
            )
            con.execute(
                "INSERT INTO gateway_attempts VALUES('mistral', 'fixture', '2026-10-01T00:00:00+00:00')"
            )
    with sqlite3.connect(paths[-1]) as con:
        con.execute("CREATE TABLE diagnostic_gets(provider TEXT,state TEXT,details_json TEXT)")
        con.execute(
            "INSERT INTO diagnostic_gets VALUES('mistral','completed',?)",
            (json.dumps({"fixed_chat_model_visible": True}),),
        )
    original = [p.read_bytes() for p in paths]
    monkeypatch.setattr(stage, "ALL_PRIOR", paths[:2])
    monkeypatch.setattr(stage, "FIRST_STAGE_DB", paths[-1])
    db = tmp_path / "isolated.sqlite"
    monkeypatch.setattr(stage, "DB", db)
    monkeypatch.setattr(sys, "argv", ["v1_mistral_isolated_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(stage, "cli", lambda: "fixture")
    monkeypatch.setattr(stage, "metadata_names", lambda _: {"MISTRAL_API_KEY"})
    tokens, reads, calls = [], [], []
    monkeypatch.setattr(stage, "service_token", lambda _: tokens.append(1) or "fixture-token")

    def resolver(name):
        reads.append(name)
        return "bad\nkey" if scenario == "bad-key" else "fixture-secret-value"

    monkeypatch.setattr(stage, "doppler_resolver_from_token", lambda *_: resolver)

    def http(config, timeout):
        calls.append(config)
        assert timeout == 30 and b"api.mistral.ai/v1/chat/completions" in config
        assert b'request = "POST"' in config and b'max_tokens\\":32' in config
        assert b'reasoning_effort\\":\\"none' in config
        body = response(finish="length" if scenario == "length" else "stop", text="READY")
        body["id"] = "cmpl-safe-fixture"
        if scenario == "rate-limit":
            body = {
                "object": "error",
                "type": "rate_limit_exceeded",
                "code": "1300",
                "message": "Rate limit exceeded.",
            }
        return (28 if scenario == "timeout" else 0), curl_output(
            body, 429 if scenario == "rate-limit" else 200, 28 if scenario == "timeout" else 0
        )

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    assert stage.main() == (0 if scenario == "success" else 2)
    assert tokens == [1] and reads == ["MISTRAL_API_KEY"]
    assert len(calls) == (0 if scenario == "bad-key" else 1)
    assert original == [p.read_bytes() for p in paths]
    assert db.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    for forbidden in ("fixture-secret-value", "fixture-token", "READY", stage.PROMPT):
        assert forbidden not in output and forbidden.encode() not in db.read_bytes()
    with sqlite3.connect(db) as con:
        saved = json.loads(con.execute("SELECT details_json FROM acceptance_receipt").fetchone()[0])
        assert saved["full_answer_verified"] is (scenario == "success")
        if scenario == "rate-limit":
            assert saved["diagnostics"]["provider_error_code"] == 1300
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(RuntimeError, match="never replay"):
        stage.main()
    assert tokens == [1] and len(calls) == (0 if scenario == "bad-key" else 1)
