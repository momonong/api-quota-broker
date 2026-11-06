"""Only validated capabilities are retained; legacy rows stay unknown."""

import sqlite3
from datetime import timedelta
from pathlib import Path

from test_dashboard import dashboard
from test_dashboard_reports import recorded_attempts
from test_gateway import NOW, make_gateway, target, task

from quota_broker.gateway import Gateway


def test_new_task_and_attempt_record_validated_capability_without_content(tmp_path):
    calls = []
    gateway = make_gateway(tmp_path, [target("text", "groq", "openai/gpt-oss-20b")], calls)
    result = gateway.run(task(key="typed-metadata", text="private-capability-fixture"))
    assert result["task_type"] == "text_generation"
    assert result["attempts"][0]["task_type"] == "text_generation"
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT task_type FROM gateway_tasks").fetchone() == ("text_generation",)
        assert con.execute("SELECT task_type FROM gateway_attempts").fetchone() == (
            "text_generation",
        )
    assert b"private-capability-fixture" not in Path(gateway.db).read_bytes()
    assert gateway.usage(task_type="text_generation")[0]["requests"] == 1
    assert gateway.usage(task_type="translation") == []


def test_preexisting_database_migrates_without_retagging_history(tmp_path):
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as con:
        for table, schema in (
            (
                "gateway_tasks",
                (
                    "request_key TEXT PRIMARY KEY,payload_hmac TEXT NOT NULL,state TEXT NOT NULL,"
                    "reservation_id TEXT,target_id TEXT,provider TEXT,model TEXT,route_reason TEXT,"
                    "created_at TEXT NOT NULL,dispatched_at TEXT,completed_at TEXT,latency_ms INTEGER,"
                    "http_status INTEGER,error_code TEXT,provider_request_id TEXT,estimated_input_tokens INTEGER,"
                    "reported_input_tokens INTEGER,reported_output_tokens INTEGER,reported_neurons INTEGER,usage_source TEXT"
                ),
            ),
            (
                "gateway_attempts",
                (
                    "request_key TEXT NOT NULL,attempt_no INTEGER NOT NULL,"
                    "reservation_id TEXT NOT NULL UNIQUE,target_id TEXT NOT NULL,provider TEXT NOT NULL,"
                    "model TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,dispatched_at TEXT,"
                    "completed_at TEXT,latency_ms INTEGER,http_status INTEGER,error_code TEXT,provider_request_id TEXT,"
                    "estimated_input_tokens INTEGER,reported_input_tokens INTEGER,reported_output_tokens INTEGER,"
                    "reported_neurons INTEGER,usage_source TEXT,input_bytes INTEGER,PRIMARY KEY(request_key,attempt_no)"
                ),
            ),
        ):
            con.execute(f"CREATE TABLE {table}({schema})")
        con.execute(
            "INSERT INTO gateway_tasks(request_key,payload_hmac,state,provider,model,created_at,dispatched_at) "
            "VALUES('historical','digest','completed','groq','openai/gpt-oss-20b',?,?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
    gateway = Gateway(
        database,
        (),
        b"fixture-digest-material-32-bytes-long",
        lambda _: "unused",
        clock=lambda: NOW,
    )
    assert gateway.status("historical")["task_type"] is None
    assert gateway.usage(task_type="unknown")[0]["requests"] == 1
    assert gateway.usage(task_type="text_generation") == []
    # Initialization/migration is repeatable and never backfills a legacy type.
    Gateway(
        database,
        (),
        b"fixture-digest-material-32-bytes-long",
        lambda _: "unused",
        clock=lambda: NOW,
    )
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT request_key,task_type FROM gateway_tasks").fetchall() == [
            ("historical", None)
        ]
        assert "task_type" in {row[1] for row in con.execute("PRAGMA table_info(gateway_attempts)")}


def test_usage_groups_same_model_by_recorded_task_and_combines_all_filters(tmp_path):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    with sqlite3.connect(source.gateway.db) as con:
        con.execute(
            "UPDATE gateway_tasks SET task_type='translation' WHERE request_key='private-synthetic-key-0'"
        )
        con.execute(
            "UPDATE gateway_tasks SET task_type='text_generation' WHERE request_key='private-synthetic-key-1'"
        )
    unfiltered = board.usage_report({})
    assert {row["task_type"]: row["requests"] for row in unfiltered["rows"]} == {
        "unknown": 7,
        "translation": 1,
        "text_generation": 1,
    }
    report = board.usage_report(
        {
            "from": (NOW - timedelta(hours=48)).isoformat(),
            "to": (NOW - timedelta(hours=46)).isoformat(),
            "provider": "google",
            "task": "translation",
        }
    )
    assert len(report["rows"]) == 1
    assert report["rows"][0]["task_type"] == "translation"
    assert report["rows"][0]["requests"] == 1
    assert report["rows"][0]["reported_input_tokens"] == 10
    assert board.usage_report({"task": "unknown"})["rows"][0]["requests"] == 7
