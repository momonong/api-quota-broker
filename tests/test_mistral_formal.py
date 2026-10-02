import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_groq_formal import curl_output, response

from quota_broker import bounded_curl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_mistral_formal_once as stage


def test_plan_does_not_read_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_mistral_formal_once.py"])
    monkeypatch.setattr(stage, "service_token", lambda _: 1 / 0)
    assert stage.main() == 0
    assert "zero GET" in capsys.readouterr().out


@pytest.mark.parametrize("scenario", ["success", "length", "usage-missing", "timeout", "bad-key"])
def test_formal_once_is_one_normal_post_and_preserves_partial_and_unknown_receipts(
    tmp_path, monkeypatch, capsys, scenario
):
    tmp_path.chmod(0o700)
    old = [tmp_path / f"old{i}.sqlite" for i in range(4)]
    for i, path in enumerate(old):
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT,dispatched_at TEXT)")
            con.executemany(
                "INSERT INTO gateway_attempts VALUES('mistral','fixture')",
                [() for _ in range(2 if i == 0 else 1)],
            )
    with sqlite3.connect(old[-1]) as con:
        con.execute("CREATE TABLE diagnostic_gets(details_json TEXT)")
        con.execute(
            "INSERT INTO diagnostic_gets VALUES(?)",
            (json.dumps({"fixed_chat_model_visible": True}),),
        )
        con.execute("CREATE TABLE acceptance_receipt(details_json TEXT)")
        prior = {
            "model": stage.MODEL,
            "http_status": 200,
            "state": "completed",
            "ledger_state": "completed",
            "finish_reason": "length",
            "response_truncated": True,
            "completed_at": "2026-10-01T00:00:00+00:00",
        }
        con.execute("INSERT INTO acceptance_receipt VALUES(?)", (json.dumps(prior),))
    original = [p.read_bytes() for p in old]
    monkeypatch.setattr(stage, "ALL_PRIOR", old[:1])
    monkeypatch.setattr(stage, "FIRST_STAGE_DB", old[1])
    monkeypatch.setattr(stage, "ISOLATED_DB", old[2])
    monkeypatch.setattr(stage, "GATE_DB", old[3])
    db = tmp_path / "formal.sqlite"
    monkeypatch.setattr(stage, "DB", db)
    monkeypatch.setattr(sys, "argv", ["v1_mistral_formal_once.py", "--live", "--db", str(db)])
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
        assert b'request = "POST"' in config and b'request = "GET"' not in config
        assert b"ministral-3b-latest" in config and b'max_tokens\\":512' in config
        assert b"reasoning_effort" not in config
        body = response(text="READY", finish="length" if scenario == "length" else "stop")
        if scenario == "usage-missing":
            body.pop("usage")
        return (28 if scenario == "timeout" else 0), curl_output(
            body, 200, 28 if scenario == "timeout" else 0
        )

    monkeypatch.setattr(bounded_curl, "run_curl", http)
    assert stage.main() == (0 if scenario == "success" else 2)
    assert tokens == [1] and reads == ["MISTRAL_API_KEY"]
    assert len(calls) == (0 if scenario == "bad-key" else 1)
    assert original == [p.read_bytes() for p in old]
    output = capsys.readouterr().out
    for forbidden in ("fixture-secret-value", "fixture-token", "READY", stage.PROMPT):
        assert forbidden not in output and forbidden.encode() not in db.read_bytes()
    with sqlite3.connect(db) as con:
        receipt = json.loads(
            con.execute("SELECT details_json FROM acceptance_receipt").fetchone()[0]
        )
        assert receipt["full_answer_verified"] is (scenario == "success")
        if scenario == "success":
            assert receipt["ledger_state"] == "completed" and receipt["reported_input_tokens"] == 11
        assert not con.execute(
            "SELECT name FROM sqlite_master WHERE name='diagnostic_gets'"
        ).fetchone()
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    assert db.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="never replay"):
        stage.main()
    assert tokens == [1] and len(calls) == (0 if scenario == "bad-key" else 1)
