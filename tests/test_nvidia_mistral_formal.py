import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, target, task
from test_groq_formal import curl_output, response

from quota_broker import bounded_curl
from quota_broker.gateway import Gateway

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_nvidia_mistral_once as stage
import v1_remaining_once as prior_stage


@pytest.mark.parametrize("provider", ["nvidia", "mistral"])
@pytest.mark.parametrize("scenario", ["success", "length", "missing-usage", "timeout", "capacity"])
def test_normal_route_persists_safe_timing_reason_and_accounting(
    tmp_path, monkeypatch, provider, scenario
):
    calls = []
    body = response(finish="length" if scenario == "length" else "stop")
    body["id"] = (
        "cmpl-safe-fixture" if provider == "mistral" else "12345678-1234-1234-1234-123456789abc"
    )
    if scenario == "missing-usage":
        body["usage"] = {"prompt_tokens": 11}
    status, code = (
        (429, 0) if scenario == "capacity" else (200, 28) if scenario == "timeout" else (200, 0)
    )
    if scenario == "capacity":
        body = {"detail": "Service tier capacity exceeded.", "extra": "fixture-secret-value"}

    def http(config, timeout):
        calls.append(config)
        assert timeout == (120 if provider == "nvidia" else 30)
        assert (
            b'"enable_thinking\\":false' in config
            if provider == "nvidia"
            else b"api.mistral.ai" in config
        )
        assert b'"max_tokens\\":16' in config
        return code, curl_output(body, status, code)

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    gateway = Gateway(
        tmp_path / "formal.sqlite",
        (target(provider, provider, stage.MODELS[provider]),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        clock=lambda: NOW,
    )
    request = task("formal-two", provider=provider, model=stage.MODELS[provider])
    result = gateway.run(request)
    details = result["diagnostics"]
    assert details["time_total_ms"] == 100
    assert result["attempts"][0]["diagnostics"] == details
    if scenario in ("success", "length"):
        assert result["state"] == "completed" and result["ledger_state"] == "completed"
        assert result["response_truncated"] is (scenario == "length")
        assert result["reported_input_tokens"] == 11
        assert gateway.usage()[0]["ledger_input_tokens"] == 11
    elif scenario == "missing-usage":
        assert result["state"] == "completed_usage_unknown" and result["ledger_state"] == "unknown"
        assert result["reported_input_tokens"] is None
    else:
        assert result["state"] == "unknown" and result["ledger_state"] == "unknown"
        assert "answer" not in result
        if scenario == "capacity" and provider == "mistral":
            assert details["reason_category"] == "service_capacity_reported"
            assert details["next_check"] == "check_provider_capacity"
        if scenario == "timeout":
            assert details["timeout_phase"] == "response_body"
    assert "answer" not in gateway.run(request) and len(calls) == 1
    assert "fixture-secret-value" not in json.dumps(result)
    db = (tmp_path / "formal.sqlite").read_bytes()
    for value in (
        b"fixture-secret-value",
        b"fixture input",
        b"partial fixture answer",
        b"Service tier capacity exceeded",
    ):
        assert value not in db


def test_plan_does_not_resolve_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_nvidia_mistral_once.py"])
    monkeypatch.setattr(stage, "service_token", lambda _: 1 / 0)
    assert stage.main() == 0
    assert "each maximum 1 authenticated models GET" in capsys.readouterr().out


@pytest.mark.parametrize(
    "scenario", ["success", "mistral-capacity", "nvidia-timeout", "model-not-visible", "bad-key"]
)
def test_bounded_stage_get_gates_post_default_transport_and_preserves_receipts(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    old = []
    for i in range(3):
        path = tmp_path / f"old{i}.sqlite"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT,dispatched_at TEXT)")
            if i < 2:
                con.executemany(
                    "INSERT INTO gateway_attempts VALUES(?,?)",
                    [(p, "fixture") for p in stage.MODELS],
                )
        old.append(path)
    original = [path.read_bytes() for path in old]
    monkeypatch.setattr(prior_stage, "ALL_PRIOR", old)
    db = tmp_path / "new.sqlite"
    monkeypatch.setattr(stage, "DB", db)
    monkeypatch.setattr(sys, "argv", ["v1_nvidia_mistral_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(stage, "cli", lambda: "fixture")
    monkeypatch.setattr(stage, "metadata_names", lambda _: set(stage.SECRETS.values()))
    tokens, reads, calls = [], [], []
    monkeypatch.setattr(stage, "service_token", lambda _: tokens.append(1) or "fixture-token")

    def resolver(name):
        reads.append(name)
        return "bad\nkey" if scenario == "bad-key" else "fixture-secret-value"

    monkeypatch.setattr(stage, "doppler_resolver_from_token", lambda *_: resolver)

    def http(config, timeout):
        text = config.decode()
        provider = "mistral" if "api.mistral.ai" in text else "nvidia"
        is_get = 'request = "GET"' in text
        calls.append((provider, is_get))
        if is_get:
            assert timeout == 30
            body = {
                "data": [
                    {
                        "id": "other"
                        if scenario == "model-not-visible"
                        else stage.MODELS[provider],
                        "capabilities": {"completion_chat": True},
                    }
                ]
            }
            return 0, curl_output(body)
        assert timeout == (120 if provider == "nvidia" else 30)
        assert 'max_tokens\\":512' in text
        if scenario == "mistral-capacity" and provider == "mistral":
            return 0, curl_output(
                {
                    "message": "Service tier capacity exceeded",
                    "type": "rate_limit_error",
                    "object": "error",
                },
                429,
            )
        if scenario == "nvidia-timeout" and provider == "nvidia":
            return 28, curl_output(response(), 200, 28)
        return 0, curl_output(response(finish="stop", text="READY"))

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    assert stage.main() == (0 if scenario == "success" else 2)
    assert tokens == [1] and sorted(reads) == sorted(stage.SECRETS.values())
    expected = (
        []
        if scenario == "bad-key"
        else [(p, True) for p in stage.MODELS]
        if scenario == "model-not-visible"
        else [(p, get) for p in stage.MODELS for get in (True, False)]
    )
    assert calls == expected
    assert original == [path.read_bytes() for path in old]
    assert db.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    for value in ("fixture-secret-value", "fixture-token", "READY", *stage.PROMPTS.values()):
        assert value not in output and value.encode() not in db.read_bytes()
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM diagnostic_gets").fetchone()[0] == 2
        assert con.execute(
            "SELECT count(*) FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
        ).fetchone()[0] == sum(not get for _, get in expected)
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(RuntimeError, match="never replay"):
        stage.main()
    assert calls == expected and tokens == [1]
