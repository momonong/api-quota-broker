"""Real readonly SQLite classification with public data; never production/credential IO."""

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "integrated", ROOT / "deploy/asus/integrate_provider_pool.py"
)
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)
PRIVATE = "PUBLIC_PRIVATE_ROW_FIXTURE_DO_NOT_PROJECT"


def database(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE gateway_tasks(request_key TEXT PRIMARY KEY,state TEXT,provider TEXT,model TEXT,
      reservation_id TEXT,dispatched_at TEXT,http_status INTEGER,reported_input_tokens INTEGER,reported_output_tokens INTEGER);
    CREATE TABLE gateway_attempts(request_key TEXT,attempt_no INTEGER,reservation_id TEXT,provider TEXT,
      state TEXT,dispatched_at TEXT,http_status INTEGER,reported_input_tokens INTEGER,reported_output_tokens INTEGER);
    CREATE TABLE reservations(id TEXT PRIMARY KEY,target_id TEXT,state TEXT,target_snapshot TEXT,
      shared_scope TEXT,dispatched_at TEXT);
    CREATE TABLE charges(reservation_id TEXT,bucket TEXT,metric TEXT,amount INTEGER);
    CREATE TABLE execution_completion(reservation_id TEXT,finished_at TEXT);
    CREATE TABLE queue_jobs(request_key TEXT,state TEXT,payload BLOB);
    CREATE TABLE queue_attempts(request_key TEXT);
    """)
    add(con, i.GROQ_KEY, "groq", "completed", rid="g", usage=(76, 25), done=True)
    con.commit()
    con.close()


def add(
    con,
    key,
    provider,
    state,
    *,
    rid,
    usage=(5, 2),
    done=False,
    snapshot=True,
    dispatched=True,
    charges=3,
):
    model = "openai/gpt-oss-20b" if provider == "groq" else "PUBLIC_MODEL"
    when = "PUBLIC_UTC" if dispatched else None
    con.execute(
        "INSERT INTO gateway_tasks VALUES(?,?,?,?,?,?,?,?,?)",
        (key, state, provider, model, rid, when, 200, *usage),
    )
    con.execute(
        "INSERT INTO gateway_attempts VALUES(?,?,?,?,?,?,?,?,?)",
        (key, 0, rid, provider, state, when, 200, *usage),
    )
    reservation = (
        "unknown"
        if state == "completed_usage_unknown"
        else "cancelled"
        if state == "pre_send_failed"
        else state
    )
    con.execute(
        "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
        (
            rid,
            "PUBLIC_TARGET",
            reservation,
            json.dumps({"provider": provider}) if snapshot else None,
            provider + ":PUBLIC_SCOPE" if snapshot else None,
            when,
        ),
    )
    for n in range(charges):
        con.execute(
            "INSERT INTO charges VALUES(?,?,?,?)",
            (
                rid,
                provider + ":asus-dev-key:" + str(n),
                "input_tokens" if n == 2 else "requests",
                usage[0] if n == 2 else 1,
            ),
        )
    if done:
        con.execute("INSERT INTO execution_completion VALUES(?,?)", (rid, "PUBLIC_UTC"))


def classify(path, intended=()):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        return i.classify_ledger(con, set(intended))


def test_four_classes_are_distinct_and_existing_dispatch_not_blanket_block(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        add(con, "PUBLIC_UNSENT", "google", "pre_send_failed", rid="u", dispatched=False)
        add(con, "PUBLIC_DONE", "mistral", "completed", rid="d", done=True)
        add(con, "PUBLIC_UNKNOWN", "nvidia", "unknown", rid="x")
    before = db.read_bytes()
    result = classify(db)
    assert result["classification"] == {
        "known_groq_settled": 1,
        "not_dispatched": 1,
        "dispatched_known_result": 1,
        "dispatch_or_settlement_unknown": 1,
    }
    assert result["blocked_providers"] == ["nvidia"]
    assert "mistral" in result["not_history_quarantined_providers"] and db.read_bytes() == before
    assert not any(key.startswith("PUBLIC_") for key in result)


def test_readonly_connection_rejects_write_and_does_not_change_private_rows(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO queue_jobs VALUES(?,?,?)", (PRIVATE, "completed", PRIVATE.encode())
        )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = classify(db)
    assert PRIVATE not in json.dumps(result)
    with (
        sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con,
        pytest.raises(sqlite3.OperationalError),
    ):
        con.execute("DELETE FROM gateway_tasks")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_completed_usage_unknown_is_known_execution_but_scope_is_quarantined(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        add(
            con,
            "PUBLIC_CF_DONE",
            "cloudflare",
            "completed_usage_unknown",
            rid="cf",
            done=True,
            charges=0,
        )
        add(con, "PUBLIC_OTHER", "google", "completed", rid="other", done=True)
        con.execute(
            "UPDATE reservations SET shared_scope=? WHERE id='other'", ("cloudflare:PUBLIC_SCOPE",)
        )
    result = classify(db)
    assert result["classification"]["dispatched_known_result"] == 2
    assert (
        "cloudflare" in result["blocked_providers"]
        and "cloudflare" not in result["not_history_quarantined_providers"]
    )
    assert (
        "google" in result["blocked_providers"]
        and "google" not in result["not_history_quarantined_providers"]
    )


def test_old_file_intent_without_ledger_row_is_unknown_not_resend_permission(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    result = classify(db, {"google"})
    assert "google" in result["blocked_providers"] and not result["old_groq_replay"]


def test_unknown_legacy_scope_stops_without_guessing_a_provider(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        add(con, "PUBLIC_UNKNOWN", "nvidia", "unknown", rid="x", snapshot=False)
    with pytest.raises(i.Blocked, match="history_scope_unknown"):
        classify(db)


def test_known_groq_requires_settled_usage_and_completion_not_just_count(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM execution_completion")
    with pytest.raises(i.Blocked, match="groq_evidence_missing"):
        classify(db)


def test_malformed_old_journal_never_projects_arbitrary_private_values():
    with pytest.raises(i.Blocked, match="history_ambiguous"):
        i.inspect_old_intents({"mode": "asus_provider_pool_r1", "posts_intended": [PRIVATE]}, None)


def test_intent_union_preserves_all_prior_dispatch_not_latest_only():
    result = i.inspect_old_intents(
        {"mode": "asus_provider_pool_r1", "posts_intended": ["google"]},
        {"mode": "asus_provider_pool_r1", "posts_intended": ["mistral"]},
    )
    assert result == {"google", "mistral"}


def test_budget_reserves_full_provider_deadline_and_cleanup():
    b = i.Budget(start=0, clock=lambda: 800)
    with pytest.raises(i.Blocked, match="budget_insufficient"):
        b.before_post("nvidia")
    assert b.posts == 0
    b = i.Budget(start=0, clock=lambda: 0)
    assert b.before_post("nvidia") == 150 and b.before_post("cloudflare") == 60
    for _ in range(5):
        b.before_post("google")
    with pytest.raises(i.Blocked, match="post_budget"):
        b.before_post("google")
    for _ in range(4):
        b.before_restart(rollback=True)
    with pytest.raises(i.Blocked, match="restart_budget"):
        b.before_restart(rollback=True)


def test_no_apply_flag_can_bypass_current_readonly_material(monkeypatch, capsys):
    monkeypatch.setattr(i.sys, "argv", ["PUBLIC", "--apply"])
    monkeypatch.setattr(
        i, "private_preflight", lambda **kw: pytest.fail("apply must not audit or dispatch")
    )
    assert i.main() == 1
    d = json.loads(capsys.readouterr().out)
    assert (
        d["code"] == "integration_authority_unverified"
        and d["credential_access"] == d["provider_posts"] == d["db_write"] == 0
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM gateway_tasks WHERE reservation_id='g'",
        "INSERT INTO execution_completion VALUES('orphan','PUBLIC_UTC')",
        "UPDATE gateway_tasks SET reservation_id='missing' WHERE reservation_id='g'",
        "UPDATE charges SET amount=77 WHERE metric='input_tokens'",
    ],
)
def test_inconsistent_or_tampered_known_evidence_is_never_cleared(tmp_path, mutation):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        con.execute(mutation)
    with pytest.raises(i.Blocked):
        classify(db)


def test_unsent_reservation_retains_scope_hold_without_being_a_dispatch(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        add(con, "PUBLIC_RESERVED", "google", "reserved", rid="r", dispatched=False, charges=0)
    result = classify(db)
    assert result["classification"]["not_dispatched"] == 1
    assert "google" in result["blocked_providers"]
    assert result["probe_permission"] is False


def test_orphan_reserved_scope_blocks_current_config_provider(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
            (
                "orphan",
                "PUBLIC",
                "reserved",
                json.dumps({"provider": "cloudflare"}),
                "PUBLIC_SHARED",
                None,
            ),
        )
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        result = i.classify_ledger(con, set(), {"google": {"PUBLIC_SHARED"}})
    assert result["blocked_providers"] == ["cloudflare", "google"]


def test_quota_refusal_is_known_delivery_and_terminal_task_can_be_exhausted(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        add(con, "PUBLIC_429", "google", "quota_rejected", rid="q", usage=(0, 0))
        con.execute("UPDATE gateway_tasks SET state='quota_exhausted' WHERE reservation_id='q'")
        con.execute("UPDATE charges SET amount=0 WHERE reservation_id='q'")
    result = classify(db)
    assert result["classification"]["dispatched_known_result"] == 1
    assert result["blocked_providers"] == []


def test_snapshot_provider_mismatch_is_not_resolved_by_final_task_provider(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE reservations SET target_snapshot=? WHERE id='g'",
            (json.dumps({"provider": "google"}),),
        )
    with pytest.raises(i.Blocked, match="history_consistency"):
        classify(db)


def test_readonly_audit_sees_committed_wal_rows_without_immutable_shortcut(tmp_path):
    db = tmp_path / "data.sqlite"
    database(db)
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        add(writer, "PUBLIC_WAL", "google", "completed", rid="w", done=True)
        writer.commit()
        wal = Path(str(db) + "-wal")
        before = {path: path.read_bytes() for path in (db, wal)}
        report = classify(db)
        assert report["classification"]["dispatched_known_result"] == 1
        assert all(path.read_bytes() == value for path, value in before.items())


def test_genuine_gateway_schema_classifies_unknown_usage_with_reserved_charges(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "public_planner", ROOT / "deploy/asus/provider_pool_plan.py"
    )
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    config = tmp_path / "gateway.json"
    # Historical eligibility helper is used only to manufacture public fixture
    # targets; production audit never imports it or grants current eligibility.
    config.write_text(
        json.dumps(
            planner.qualified_config(
                {"groq", "cloudflare"}, "2026-10-08T00:00:00+00:00", "2026-10-07T00:00:00+00:00"
            )
        )
    )
    db = tmp_path / "ledger.sqlite3"
    calls = []

    # The historic helper stamps its estimate with wall time. This fixture uses
    # an October 7 clock; pin its estimate too, rather than expiring with today.
    value = json.loads(config.read_text())
    for target in value["targets"]:
        if "neuron_estimate" in target:
            target["neuron_estimate"]["verified_at"] = "2026-10-07T00:00:00+00:00"
    config.write_text(json.dumps(value))

    def transport(url, *_):
        calls.append(url)
        if "api.cloudflare.com" in url:
            body = {
                "success": True,
                "result": {
                    "response": "PUBLIC_READY",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            }
        else:
            body = {
                "model": "openai/gpt-oss-20b",
                "choices": [{"message": {"content": "PUBLIC_READY"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 76, "completion_tokens": 25},
            }
        return 200, {}, json.dumps(body).encode()

    gateway = Gateway(
        db,
        load_gateway_config(config),
        b"PUBLIC_HMAC_FIXTURE_32_BYTES_OR_MORE",
        lambda name: "0" * 32 if name == "CLOUDFLARE_ACCOUNT_ID" else "PUBLIC_CREDENTIAL",
        transport,
        clock=lambda: datetime(2026, 10, 7, 12, tzinfo=UTC),
    )
    for provider, key in (("groq", i.GROQ_KEY), ("cloudflare", "PUBLIC_CF")):
        result = gateway.run(
            {
                "request_key": key,
                "provider": provider,
                "model": planner.PROVIDERS[provider][0],
                "input": "PUBLIC_PROMPT",
                "capability": "text_generation",
                "max_output_tokens": 32,
                "max_attempts": 1,
            }
        )
        assert result["state"] == ("completed" if provider == "groq" else "completed_usage_unknown")
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE queue_jobs(request_key TEXT,state TEXT,payload BLOB)")
        con.execute("CREATE TABLE queue_attempts(request_key TEXT)")
        assert (
            con.execute(
                "SELECT count(*) FROM charges WHERE reservation_id=(SELECT reservation_id "
                "FROM gateway_tasks WHERE request_key='PUBLIC_CF')"
            ).fetchone()[0]
            == 4
        )
        assert (
            con.execute(
                "SELECT state FROM reservations WHERE id=(SELECT reservation_id "
                "FROM gateway_tasks WHERE request_key='PUBLIC_CF')"
            ).fetchone()[0]
            == "unknown"
        )
    before = db.read_bytes()
    result = classify(db)
    assert len(calls) == 2 and result["classification"]["known_groq_settled"] == 1
    assert result["classification"]["dispatched_known_result"] == 1
    assert result["blocked_providers"] == ["cloudflare"] and db.read_bytes() == before


def test_bootstrap_self_consistent_policy_does_not_grant_provenance_or_resume(
    tmp_path, monkeypatch
):
    root = tmp_path / "bootstrap"
    root.mkdir(mode=0o700)
    files = {
        "source.tar": b"PUBLIC_SOURCE",
        "enable_provider_pool.py": b"raise RuntimeError('NEVER_EXECUTE')",
        "provider_pool_plan.py": b"PUBLIC_PLANNER",
        "build_asus_release.py": b"PUBLIC_VERIFIER",
    }
    policy = {
        field: hashlib.sha256(files[name]).hexdigest()
        for name, field in (
            ("source.tar", "source_archive_sha256"),
            ("enable_provider_pool.py", "operator_sha256"),
            ("provider_pool_plan.py", "planner_sha256"),
            ("build_asus_release.py", "verifier_sha256"),
        )
    }
    files["policy.json"] = json.dumps(policy).encode()
    for name, raw in files.items():
        (root / name).write_bytes(raw)
        (root / name).chmod(0o600)
    original = Path.lstat

    def metadata(path):
        # Owner shim is explicit; file IO/hash/inodes are actual public fixtures.
        value = original(path)
        return SimpleNamespace(
            st_uid=0,
            st_gid=0,
            st_mode=value.st_mode,
            st_nlink=value.st_nlink,
            st_ino=value.st_ino,
            st_size=value.st_size,
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    report = i.audit_bootstrap(
        root=root,
        read=lambda path, **_: path.read_bytes(),
        exists=lambda path: path == root,
        bundle_read=lambda: pytest.fail("absent original bundle must not be opened"),
    )
    assert report["internal_policy_hashes_match"] and len(report["files"]) == 5
    assert not report["historical_bundle_provenance_verified"] and not report["resume_permission"]
    assert not report["source_matches_published_r1"]
    assert "NEVER_EXECUTE" not in json.dumps(report)


def test_native_guard_stops_before_any_private_read_on_wrong_identity(monkeypatch):
    monkeypatch.setattr(i.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        i, "private_preflight", lambda: pytest.fail("private read before scope guard")
    )
    with pytest.raises(i.Blocked, match="unit_scope"):
        i.protected_audit_main()
