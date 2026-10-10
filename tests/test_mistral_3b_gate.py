import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import v1_mistral_3b_gate_once as repair
import v1_mistral_3b_once as stage


def test_repair_plan_is_offline(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_mistral_3b_gate_once.py"])
    monkeypatch.setattr(stage, "service_token", lambda _: 1 / 0)
    assert repair.main() == 0
    assert "1 new authenticated GET" in capsys.readouterr().out


@pytest.mark.parametrize(
    "prior_state", ["valid", "post-used", "dispatched-without-receipt", "no-get"]
)
def test_repair_requires_unused_post_and_preserves_first_get_receipt(
    tmp_path, monkeypatch, prior_state
):
    old = tmp_path / "first-get.sqlite"
    with sqlite3.connect(old) as con:
        con.execute("CREATE TABLE diagnostic_gets(dispatched_at TEXT,details_json TEXT)")
        con.execute("CREATE TABLE acceptance_receipt(details_json TEXT)")
        if prior_state != "no-get":
            con.execute(
                "INSERT INTO diagnostic_gets VALUES('fixture',?)",
                (json.dumps({"http_status": 200, "state": "gate_failed"}),),
            )
        if prior_state == "post-used":
            con.execute("INSERT INTO acceptance_receipt VALUES('{}')")
        if prior_state == "dispatched-without-receipt":
            con.execute("CREATE TABLE gateway_attempts(dispatched_at TEXT)")
            con.execute("INSERT INTO gateway_attempts VALUES('fixture')")
    original = old.read_bytes()
    db = tmp_path / "repaired.sqlite"
    monkeypatch.setattr(stage, "DB", old)
    monkeypatch.setattr(repair, "DB", db)
    monkeypatch.setattr(sys, "argv", ["v1_mistral_3b_gate_once.py", "--live", "--db", str(db)])
    calls = []

    def run_once(path, key):
        calls.append((path, key))
        path.touch(mode=0o600)
        return 0

    monkeypatch.setattr(stage, "run_once", run_once)
    if prior_state == "valid":
        assert repair.main() == 0
        assert calls == [(db, repair.KEY)]
        with pytest.raises(RuntimeError, match="never replay"):
            repair.main()
    else:
        with pytest.raises(RuntimeError, match="one prior GET and zero prior POST"):
            repair.main()
        assert calls == []
    assert old.read_bytes() == original
