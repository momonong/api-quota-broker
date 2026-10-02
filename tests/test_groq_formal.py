import io
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gateway import HMAC_KEY, NOW, target, task

from quota_broker import bounded_curl
from quota_broker.cli import gateway_cli
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import ProviderError, official_request, provider_http
from quota_broker.gateway_server import make_gateway_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_groq_formal_once as formal


def response(*, finish="length", usage=None, text="partial fixture answer"):
    return {
        "id": "chatcmpl-safe-fixture",
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 3},
    }


def curl_output(body, status=200, code=0):
    metrics = {
        "http_code": str(status),
        "exitcode": code,
        **dict.fromkeys(bounded_curl.TIMINGS, 0.1),
    }
    return (
        f"HTTP/2 {status}\r\ncontent-type: application/json\r\n\r\n".encode()
        + json.dumps(body).encode()
        + bounded_curl.MARKER
        + json.dumps(metrics).encode()
    )


def default_gateway(tmp_path):
    return Gateway(
        tmp_path / "formal.sqlite",
        (target("groq", "groq", "openai/gpt-oss-20b"),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_default_route_uses_curl_settles_actual_usage_and_never_replays(
    tmp_path, monkeypatch, finish
):
    sent = []

    def run(config, timeout):
        sent.append(config)
        assert timeout == 30
        # Parse the real curl config with all transfers disabled.
        parsed = subprocess.run(
            ["/usr/bin/curl", "--disable", "--silent", "--config", "-", "--version"],
            input=config,
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert parsed.returncode == 0
        return 0, curl_output(response(finish=finish))

    monkeypatch.setattr(bounded_curl, "run_curl", run)
    monkeypatch.setattr(
        "quota_broker.gateway_providers.urllib.request.build_opener", lambda *_: 1 / 0
    )
    gateway = default_gateway(tmp_path)
    request = task("formal", provider="groq")
    result = gateway.run(request)
    assert result["state"] == "completed"
    assert result["answer"] == "partial fixture answer"
    assert result["finish_reason"] == finish
    assert result["response_truncated"] is (finish == "length")
    assert result["attempts"][0]["response_truncated"] is (finish == "length")
    assert (
        result["ledger_state"] == "completed" and result["ledger_basis"] == "settled_provider_usage"
    )
    assert result["reported_input_tokens"] == 11 and result["reported_output_tokens"] == 3
    assert gateway.usage()[0]["ledger_input_tokens"] == 11
    assert gateway.usage()[0]["ledger_held_count"] == 0
    assert gateway.usage()[0]["truncated_count"] == int(finish == "length")
    restarted = default_gateway(tmp_path)
    assert restarted.run(request)["response_truncated"] is (finish == "length")
    assert "answer" not in restarted.run(request) and len(sent) == 1
    config = sent[0].decode()
    assert 'url = "https://api.groq.com/openai/v1/chat/completions"' in config
    for option in ('retry = "0"', 'max-redirs = "0"', 'proto = "=https"', 'max-time = "30.0"'):
        assert option in config
    for option in ("user-agent", "insecure", "location", "proxy"):
        assert option not in config.lower()
    db = (tmp_path / "formal.sqlite").read_bytes()
    for forbidden in (b"fixture-secret-value", b"partial fixture answer", b"fixture input"):
        assert forbidden not in db


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": 11},
        {"completion_tokens": 3},
        {"prompt_tokens": True, "completion_tokens": 3},
        {"prompt_tokens": 11, "completion_tokens": -1},
        {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 13},
        {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": True},
        {"prompt_tokens": 11, "completion_tokens": 17},
    ],
)
def test_truncated_answer_with_untrusted_usage_keeps_hold_without_retry(
    tmp_path, monkeypatch, usage
):
    sent = []
    monkeypatch.setattr(
        bounded_curl,
        "run_curl",
        lambda *_: (sent.append(1) or 0, curl_output(response(usage=usage))),
    )
    gateway = default_gateway(tmp_path)
    result = gateway.run(task("unknown-usage", provider="groq"))
    assert result["state"] == "completed_usage_unknown"
    assert result["answer"] == "partial fixture answer" and result["response_truncated"] is True
    assert result["reported_input_tokens"] is None and result["reported_output_tokens"] is None
    assert result["ledger_state"] == "unknown" and result["ledger_basis"] == "held_estimate"
    assert gateway.usage()[0]["input_unknown_count"] == 1
    assert gateway.usage()[0]["ledger_held_count"] == 1
    gateway.run(task("unknown-usage", provider="groq"))
    assert sent == [1]


def test_arbitrary_finish_and_reflected_key_never_enter_metadata(tmp_path, monkeypatch):
    body = response(finish="fixture-secret-value")
    body["id"] = "chatcmpl-fixture-secret-value"
    monkeypatch.setattr(bounded_curl, "run_curl", lambda *_: (0, curl_output(body)))
    gateway = default_gateway(tmp_path)
    result = gateway.run(task("safe-metadata", provider="groq"))
    assert result["finish_reason"] == "unclassified" and result["response_truncated"] is None
    assert result["provider_request_id"] is None
    assert b"fixture-secret-value" not in (tmp_path / "formal.sqlite").read_bytes()


@pytest.mark.parametrize("case", ["timeout", "partial-429", "empty"])
def test_uncertain_or_error_response_never_falls_back(tmp_path, monkeypatch, case):
    calls = []
    if case == "timeout":
        status, code, body = 200, 28, response()
    elif case == "partial-429":
        status, code, body = 429, 0, {**response(), "error": {"type": "rate_limit_error"}}
    else:
        status, code, body = 200, 0, response(text=" ")
    monkeypatch.setattr(
        bounded_curl,
        "run_curl",
        lambda *_: (calls.append(1) or code, curl_output(body, status, code)),
    )
    gateway = default_gateway(tmp_path)
    result = gateway.run(task("uncertain", provider="groq"))
    assert result["state"] == "unknown" and result["ledger_state"] == "unknown"
    assert "answer" not in result
    gateway.run(task("uncertain", provider="groq"))
    assert calls == [1]


@pytest.mark.parametrize("change", ["url", "credential", "stream", "extra", "output", "timeout"])
def test_curl_rejects_contract_changes_without_dispatch(monkeypatch, change):
    monkeypatch.setattr(bounded_curl, "run_curl", lambda *_: 1 / 0)
    url, headers, payload = official_request(
        "groq", "openai/gpt-oss-20b", "fixture", "fixture-secret", "fixture input", 16, None, None
    )
    timeout = 30
    if change == "url":
        url += "/other"
    elif change == "credential":
        headers["Authorization"] += "\nurl = bad"
    elif change == "stream":
        payload["stream"] = True
    elif change == "extra":
        payload["proxy"] = "bad"
    elif change == "output":
        payload["max_completion_tokens"] = 4097
    else:
        timeout = 31
    with pytest.raises(ProviderError):
        provider_http(url, headers, payload, timeout)


def test_additive_schema_migration_preserves_legacy_unknown_and_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(bounded_curl, "run_curl", lambda *_: (28, curl_output(response(), code=28)))
    gateway = default_gateway(tmp_path)
    request = task("legacy-unknown", provider="groq")
    old = gateway.run(request)
    path = tmp_path / "formal.sqlite"
    with sqlite3.connect(path) as con:
        for table in ("gateway_tasks", "gateway_attempts"):
            con.execute(f"ALTER TABLE {table} DROP COLUMN finish_reason")
            con.execute(f"ALTER TABLE {table} DROP COLUMN response_truncated")
        rows = con.execute("SELECT * FROM reservations").fetchall()
        charges = con.execute("SELECT * FROM charges").fetchall()
    monkeypatch.setattr(bounded_curl, "run_curl", lambda *_: 1 / 0)
    for _ in range(2):
        migrated = default_gateway(tmp_path)
        result = migrated.run(request)
        assert result["state"] == old["state"] == "unknown"
        assert result["finish_reason"] is None and result["response_truncated"] is None
        assert result["ledger_charges"] == old["ledger_charges"]
        with sqlite3.connect(path) as con:
            assert con.execute("SELECT * FROM reservations").fetchall() == rows
            assert con.execute("SELECT * FROM charges").fetchall() == charges
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_cli_and_api_use_default_groq_path_and_expose_partial_metadata(
    tmp_path, monkeypatch, capsys
):
    sent = []
    monkeypatch.setattr(
        bounded_curl, "run_curl", lambda *_: (sent.append(1) or 0, curl_output(response()))
    )
    gateway = default_gateway(tmp_path)
    server = make_gateway_server(gateway, "fixture-client-token-at-least-thirty-two", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_path = tmp_path / "client.token"
    token_path.write_text("fixture-client-token-at-least-thirty-two")
    args = SimpleNamespace(
        url=f"http://127.0.0.1:{server.server_port}",
        token_file=str(token_path),
        token_stdin=False,
        json=True,
        action="run",
        request_key="cli-partial",
        capability="text_generation",
        max_output_tokens=16,
        source_language=None,
        target_language=None,
        neuron_bound=None,
        provider="groq",
        model="openai/gpt-oss-20b",
    )
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO("fixture input"))
        gateway_cli(args)
        result = json.loads(capsys.readouterr().out)
        assert result["answer"] == "partial fixture answer"
        assert result["response_truncated"] is True and result["state"] == "completed"
        args.action = "status"
        gateway_cli(args)
        status = json.loads(capsys.readouterr().out)
        assert "answer" not in status and status["finish_reason"] == "length"
        args.action = "usage"
        args.from_at = args.to_at = None
        gateway_cli(args)
        assert json.loads(capsys.readouterr().out)[0]["truncated_count"] == 1
        assert sent == [1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_formal_plan_reads_no_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_groq_formal_once.py"])
    monkeypatch.setattr(formal, "service_token", lambda _: 1 / 0)
    assert formal.main() == 0
    assert "POST maximum 1; no GET" in capsys.readouterr().out


@pytest.mark.parametrize(
    "scenario", ["success", "complete-variant", "length", "unknown-usage", "bad-key"]
)
def test_formal_script_default_route_single_post_safe_receipt_and_old_dbs_unchanged(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    prior = []
    for index in range(4):
        path = tmp_path / f"prior{index}.sqlite"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT, dispatched_at TEXT)")
            if index in (0, 3):
                con.execute("INSERT INTO gateway_attempts VALUES('groq','fixture')")
        prior.append(path)
    original = [path.read_bytes() for path in prior]
    db = tmp_path / "new.sqlite"
    monkeypatch.setattr(formal, "DB", db)
    monkeypatch.setattr(formal, "ALL_PRIOR", prior[:3])
    monkeypatch.setattr(formal, "DIAGNOSIS_DB", prior[3])
    monkeypatch.setattr(sys, "argv", ["v1_groq_formal_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(formal, "cli", lambda: "fixture")
    monkeypatch.setattr(formal, "metadata_names", lambda _: {"GROQ_API_KEY"})
    tokens, reads, calls = [], [], []
    monkeypatch.setattr(formal, "service_token", lambda _: tokens.append(1) or "fixture-token")

    def resolve(name):
        reads.append(name)
        return "bad\nkey" if scenario == "bad-key" else "fixture-secret-value"

    monkeypatch.setattr(formal, "doppler_resolver_from_token", lambda *_: resolve)

    def curl(config, timeout):
        calls.append(config)
        assert timeout == 30
        assert b'max_completion_tokens\\":512' in config
        assert b"/openai/v1/models" not in config
        body = response(
            finish="length" if scenario == "length" else "stop",
            usage={}
            if scenario == "unknown-usage"
            else {"prompt_tokens": 11, "completion_tokens": 32},
            text="READY." if scenario == "complete-variant" else "READY",
        )
        return 0, curl_output(body)

    monkeypatch.setattr(bounded_curl, "run_curl", curl)
    if scenario == "bad-key":
        from quota_broker.gateway import GatewayError

        with pytest.raises(GatewayError):
            formal.main()
    else:
        complete = scenario in ("success", "complete-variant")
        assert formal.main() == (0 if complete else 2)
        safe = json.loads(capsys.readouterr().out)
        assert safe["full_answer_verified"] is complete
        assert safe["ready_exact_match"] is (scenario != "complete-variant")
        assert len(calls) == 1
        with sqlite3.connect(db) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
                ).fetchone()[0]
                == 1
            )
            saved = json.loads(
                con.execute("SELECT details_json FROM acceptance_receipt").fetchone()[0]
            )
            assert saved == safe
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert tokens == [1] and reads == ["GROQ_API_KEY"]
    assert len(calls) == (0 if scenario == "bad-key" else 1)
    assert original == [path.read_bytes() for path in prior]
    assert db.stat().st_mode & 0o777 == 0o600
    for forbidden in (b"fixture-secret-value", b"fixture-token", b"READY", formal.PROMPT.encode()):
        assert forbidden not in db.read_bytes()
    with pytest.raises(RuntimeError, match="never replay"):
        formal.main()
    assert tokens == [1]
