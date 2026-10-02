import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bounded_curl as curl
import v1_groq_once as groq

from quota_broker.gateway_providers import ProviderError, official_request


def test_plan_has_no_credentials_or_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_groq_once.py"])
    monkeypatch.setattr(groq, "service_token", lambda _: 1 / 0)
    assert groq.main() == 0
    assert "authenticated GET maximum 1" in capsys.readouterr().out


@pytest.mark.parametrize("post", [False, True])
def test_curl_config_is_official_bounded_and_parses_without_network(monkeypatch, post):
    configs = []

    def fixture(config, timeout):
        configs.append(config.decode())
        assert timeout == 30
        result = subprocess.run(
            ["/usr/bin/curl", "--disable", "--silent", "--config", "-", "--version"],
            input=config,
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert result.returncode == 0 and b"fixture-secret" not in result.stdout
        metrics = {"http_code": "200", "exitcode": 0, **dict.fromkeys(curl.TIMINGS, 0.2)}
        return (
            0,
            b"HTTP/2 200\r\ncf-ray: 1234567890abcdef-TPE\r\n\r\n{}"
            + curl.MARKER
            + json.dumps(metrics).encode(),
        )

    monkeypatch.setattr(groq, "run_curl", fixture)
    headers = {"Authorization": "Bearer fixture-secret"}
    payload = (
        official_request(
            "groq", groq.MODEL, "fixture", "fixture-secret", groq.PROMPT, 32, None, None
        )[2]
        if post
        else None
    )
    status, received, _, details = groq.groq_http(
        groq.POST_URL if post else groq.GET_URL, headers, payload, 30
    )
    assert (
        status == 200
        and received["cf-ray"] == "1234567890abcdef-TPE"
        and details["transport_code"] == "ok"
    )
    text = configs[0]
    assert 'request = "POST"' in text if post else 'request = "GET"' in text
    assert 'retry = "0"' in text and 'max-redirs = "0"' in text
    assert "Authorization: Bearer fixture-secret" in text
    assert (
        "user-agent" not in text.lower()
        and "proxy" not in text.lower()
        and "location" not in text.lower()
    )
    assert "data-binary" in text if post else "data-binary" not in text
    with pytest.raises(ProviderError):
        groq.groq_http("https://example.invalid", headers, payload, 30)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("req_safe-id", "req_safe-id"),
        ("chatcmpl-safe", "chatcmpl-safe"),
        ("req_fixture-secret", None),
        ("req_fixture-token", None),
        ("arbitrary-value", None),
        ("req_bad\nheader", None),
    ],
)
def test_request_identifiers_are_validated_and_secret_filtered(value, expected):
    assert groq.safe_id(value, ("fixture-secret", "fixture-token")) == expected


@pytest.mark.parametrize(
    "secret",
    [
        "fixture\nurl = bad",
        "fixture\rsecret",
        "fixture\0secret",
        'fixture"secret',
        "fixture secret",
        "fixtureésecret",
        "x" * 257,
        "",
    ],
)
def test_malformed_credentials_never_reach_curl(monkeypatch, secret):
    monkeypatch.setattr(groq, "run_curl", lambda *_: 1 / 0)
    with pytest.raises(ProviderError):
        groq.groq_http(groq.GET_URL, {"Authorization": "Bearer " + secret}, None, 30)


@pytest.mark.parametrize(
    "scenario", ["edge", "not_visible", "success", "project_block", "length", "timeout", "bad_key"]
)
def test_fixture_stage_is_once_gated_secret_safe_and_preserves_old_receipts(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    db = tmp_path / "new.sqlite"
    prior = []
    for index in range(3):
        path = tmp_path / f"prior{index}.sqlite"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT, dispatched_at TEXT)")
            if index == 0:
                con.execute("INSERT INTO gateway_attempts VALUES('groq','fixture')")
        prior.append(path)
    old_bytes = [path.read_bytes() for path in prior]
    monkeypatch.setattr(groq, "DB", db)
    monkeypatch.setattr(groq, "ALL_PRIOR", prior)
    monkeypatch.setattr(sys, "argv", ["v1_groq_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(groq, "cli", lambda: "fixture")
    monkeypatch.setattr(groq, "metadata_names", lambda _: {"GROQ_API_KEY"})
    tokens, reads, calls = [], [], []
    monkeypatch.setattr(groq, "service_token", lambda _: tokens.append(1) or "fixture-token")

    def resolver(name):
        reads.append(name)
        return "fixture-secret\n" if scenario == "bad_key" else "fixture-secret"

    monkeypatch.setattr(groq, "doppler_resolver_from_token", lambda *_: resolver)

    def http(url, headers, payload, timeout):
        calls.append(url)
        assert headers == {"Authorization": "Bearer fixture-secret"} and timeout == 30
        received = {
            "x-request-id": "req_safe-id",
            "cf-ray": "1234567890abcdef-TPE",
            "content-type": "application/json",
        }
        if url == groq.GET_URL:
            assert payload is None
            if scenario == "timeout":
                raise groq.ProviderPhaseTimeout("curl_process_deadline")
            status = 403 if scenario == "edge" else 200
            body = (
                {
                    "error_code": 1010,
                    "error_name": "browser_signature_banned",
                    "message": "fixture-secret",
                }
                if scenario == "edge"
                else {
                    "data": [
                        {"id": "other" if scenario == "not_visible" else groq.MODEL, "active": True}
                    ]
                }
            )
        else:
            assert url == groq.POST_URL and payload["max_completion_tokens"] == 32
            assert payload["messages"] == [{"role": "user", "content": groq.PROMPT}]
            if scenario == "project_block":
                status, body = (
                    403,
                    {
                        "error": {
                            "type": "permissions_error",
                            "code": "model_permission_blocked_project",
                            "message": "fixture-secret",
                        }
                    },
                )
            else:
                status, body = (
                    200,
                    {
                        "id": "chatcmpl-fixture-secret",
                        "choices": [
                            {
                                "message": {"content": "fixture-answer"},
                                "finish_reason": "length" if scenario == "length" else "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                    },
                )
        return (
            status,
            received,
            json.dumps(body).encode(),
            {"transport_code": "ok", "time_appconnect_ms": 20},
        )

    monkeypatch.setattr(groq, "groq_http", http)
    expected_calls = (
        []
        if scenario == "bad_key"
        else [groq.GET_URL]
        if scenario in ("edge", "not_visible", "timeout", "bad_key")
        else [groq.GET_URL, groq.POST_URL]
    )
    assert groq.main() == (0 if scenario == "success" else 2)
    assert calls == expected_calls and tokens == [1] and reads == ["GROQ_API_KEY"]
    assert old_bytes == [path.read_bytes() for path in prior]
    assert db.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    for private in ("fixture-secret", "fixture-token", "fixture-answer", groq.PROMPT):
        assert private not in output and private.encode() not in db.read_bytes()
    with sqlite3.connect(db) as con:
        get = con.execute("SELECT state,dispatched_at,completed_at FROM diagnostic_gets").fetchone()
        assert (get[1] is None if scenario == "bad_key" else get[1].endswith("+00:00")) and get[
            2
        ].endswith("+00:00")
        attempts = con.execute("SELECT state,provider_request_id FROM gateway_attempts").fetchall()
        assert len(attempts) == max(0, len(calls) - 1)
        if attempts:
            assert attempts[0] == ("completed" if scenario == "success" else "unknown", None)
            details = json.loads(
                con.execute(
                    "SELECT details_json FROM gateway_diagnostics WHERE request_key=?", (groq.KEY,)
                ).fetchone()[0]
            )
            assert (
                details["request_id"] == "req_safe-id"
                and details["cf_ray"] == "1234567890abcdef-TPE"
            )
            if scenario == "project_block":
                assert (
                    details["reason_category"] == "groq_model_blocked_project"
                    and details["next_check"] == "project_model_limits"
                )
            if scenario == "length":
                assert (
                    details["finish_reason"] == "length"
                    and details["reason_category"] == "completion_incomplete"
                )
        if scenario == "edge":
            details = json.loads(
                con.execute("SELECT details_json FROM gateway_diagnostics").fetchone()[0]
            )
            assert (
                details["reason_category"] == "groq_edge_browser_signature_blocked"
                and details["next_check"] == "groq_site_owner"
            )
    with pytest.raises(RuntimeError, match="never replay"):
        groq.main()
    assert calls == expected_calls and tokens == [1]
