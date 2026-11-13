"""Scoped shared-runtime publishing with fixture release trees and fake systemd."""

import base64
import hashlib
import importlib.util
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "shared_maintenance", Path(__file__).parents[1] / "deploy/hp_shared_maintenance.py"
)
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


def rows(marker=b"# code fixture\n"):
    result = []
    for path in maintenance.ALLOWED:
        raw = b"{}" if path.endswith(".json") else marker
        if path == "config/access.json":
            raw = b'{"version":1,"principals":[]}'
        if path == "config/gateway.json":
            raw = b'{"targets":[]}'
        if path == "config/secret-inventory.json":
            raw = json.dumps(
                {
                    "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
                    "project": "api-quota-broker",
                    "config": "dev",
                    "verified_at": "2026-11-11T00:00:00Z",
                    "expires_at": "2026-12-11T00:00:00Z",
                }
            ).encode()
        result.append(
            {
                "path": path,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content_base64": base64.b64encode(raw).decode(),
            }
        )
    return result


def candidate(current, sha, *, operation=None, release="new-release"):
    return {
        "schema": "hp-quota-shared-release-v1",
        "operation_id": operation or str(uuid.uuid4()),
        "release_id": release,
        "expected_current": current,
        "expected_manifest_sha256": sha,
        "files": rows(),
    }


def database(path, *, state=None, reservation=None):
    with sqlite3.connect(path) as con:
        con.executescript("""CREATE TABLE queue_jobs(request_key TEXT,state TEXT);
            CREATE TABLE reservations(id TEXT,state TEXT);
            CREATE TABLE execution_completion(reservation_id TEXT);
            CREATE TABLE observed_holds(id TEXT,amount INTEGER);""")
        if state:
            con.execute("INSERT INTO queue_jobs VALUES('job',?)", (state,))
        if reservation:
            con.execute("INSERT INTO reservations VALUES('old',?)", (reservation,))
        con.execute("INSERT INTO observed_holds VALUES('unknown-hold',123)")
    path.chmod(0o600)


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "OWNER", os.getuid())
    monkeypatch.setattr(maintenance, "GROUP", os.getgid())
    monkeypatch.setattr(maintenance, "SERVICE_UID", os.getuid())
    root, journal, state = tmp_path / "root", tmp_path / "journal", tmp_path / "runtime"
    for path in (root, journal, state):
        path.mkdir(mode=0o700)
    (root / "releases").mkdir(mode=0o750)
    (journal / "history").mkdir(mode=0o700)
    lock = tmp_path / "lock"
    lock.touch(mode=0o600)
    marker = state / "instance.json"
    marker.write_text(
        '{"schema":1,"instance_id":"hp-shared-20261111","historical_gap":true,'
        '"history_start":"2026-11-11T00:00:00+00:00","credential_fingerprint":"' + "f" * 64 + '"}'
    )
    marker.chmod(0o600)
    database(state / "ledger.sqlite3")
    instance = maintenance.Operator(root=root, journal=journal, lock=lock, runtime_state=state)
    manifest, files = maintenance.validate(candidate("baseline", "0" * 64, release="old-release"))
    instance.stage("old-release", manifest, files)
    (root / "current").symlink_to("releases/old-release")
    events = []
    monkeypatch.setattr(maintenance, "verify_service", lambda manifest: events.append("verify"))
    monkeypatch.setattr(maintenance, "stop_service", lambda: events.append("stop"))
    monkeypatch.setattr(maintenance, "start_service", lambda: events.append("start"))
    return instance, events


def wire(action, **kwargs):
    return {"schema": "hp-quota-shared-maintenance-v1", "action": action, **kwargs}


def test_exact_scope_no_key_files_or_reordered_payloads():
    value = candidate("baseline", "0" * 64)
    manifest, files = maintenance.validate(value)
    assert len(manifest) == 51 and set(files) == set(maintenance.ALLOWED)
    for module in (
        "crypto_admin",
        "crypto_config",
        "encrypted_transport",
        "execution_policy",
        "local_jobs",
        "secure_client",
    ):
        assert "app/quota_broker/" + module + ".py" in files
    for path in ("../../etc/shadow", "config/runtime.json", "launcher.py"):
        changed = candidate("baseline", "0" * 64)
        changed["files"][0]["path"] = path
        with pytest.raises(maintenance.Refused):
            maintenance.validate(changed)
    value["files"].reverse()
    with pytest.raises(maintenance.Refused):
        maintenance.validate(value)


@pytest.mark.parametrize(
    "state,result_state", [("queued", "none"), ("unknown", "none"), ("completed", "storage_failed")]
)
def test_local_unsettled_work_prevents_release_switch(operator, state, result_state):
    instance, events = operator
    with sqlite3.connect(instance.runtime_state / "ledger.sqlite3") as con:
        con.execute("CREATE TABLE local_jobs(state TEXT,result_state TEXT)")
        con.execute("INSERT INTO local_jobs VALUES(?,?)", (state, result_state))
    current, sha, _ = instance.current()
    with pytest.raises(maintenance.Refused, match="local_jobs_require_reconcile"):
        instance.handle(wire("publish", payload=candidate(current, sha)))
    assert instance.current()[0] == current
    assert events == []


def test_current_legacy_manifest_is_readable_but_cannot_be_new_payload(operator):
    instance, _ = operator
    location = instance.root / "releases/old-release"
    manifest = json.loads((location / "manifest.json").read_text())
    legacy = []
    for row in manifest:
        if Path(row["path"]).stem in {
            "crypto_admin",
            "crypto_config",
            "encrypted_transport",
            "execution_policy",
            "local_jobs",
            "secure_client",
        }:
            (location / row["path"]).unlink()
        else:
            legacy.append(row)
    (location / "manifest.json").write_bytes(maintenance.canonical(legacy))
    assert len(instance.manifest("old-release")) == 45
    value = candidate("baseline", "0" * 64)
    value["files"] = [row for row in value["files"] if row["path"] in {r["path"] for r in legacy}]
    with pytest.raises(maintenance.Refused):
        maintenance.validate(value)


def test_encrypted_config_blocks_legacy_rollback_before_service_stop(
    operator, tmp_path, monkeypatch
):
    instance, events = operator
    current, sha, _ = instance.current()
    operation = str(uuid.uuid4())
    instance.handle(wire("publish", payload=candidate(current, sha, operation=operation)))
    old = instance.root / "releases" / current
    rows = json.loads((old / "manifest.json").read_bytes())
    legacy = [row for row in rows if row["path"] in maintenance.LEGACY_ALLOWED]
    for row in rows:
        if row not in legacy:
            (old / row["path"]).unlink()
    (old / "manifest.json").write_bytes(maintenance.canonical(legacy))
    record = instance.receipt(operation)
    record["previous_manifest_sha256"] = maintenance.digest(maintenance.canonical(legacy))
    instance.save(record)
    private = tmp_path / "private-api.json"
    private.touch()
    monkeypatch.setattr(maintenance, "PRIVATE_CONFIG", private, raising=False)
    events.clear()
    with pytest.raises(maintenance.Refused, match="encrypted_rollback_requires_root_gate"):
        instance.handle(wire("rollback", operation_id=operation))
    assert events == []
    assert instance.current()[0] == "new-release"


def test_publish_cas_stop_before_switch_preserves_ledger_and_binding(operator):
    instance, events = operator
    before = (instance.runtime_state / "ledger.sqlite3").read_bytes()
    marker = (instance.runtime_state / "instance.json").read_bytes()
    current, sha, _ = instance.current()
    value = candidate(current, sha)
    result = instance.handle(wire("publish", payload=value))
    assert result["status"] == "complete" and result["current"] == "new-release"
    assert events.index("stop") < events.index("start")
    assert (instance.runtime_state / "ledger.sqlite3").read_bytes() == before
    assert (instance.runtime_state / "instance.json").read_bytes() == marker
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("publish", payload=value))
    result = instance.handle(wire("rollback", operation_id=value["operation_id"]))
    assert result["status"] == "rolled_back" and result["current"] == current
    assert (instance.runtime_state / "ledger.sqlite3").read_bytes() == before


def test_stale_baseline_and_access_policy_change_never_stop_service(operator):
    instance, events = operator
    current, sha, _ = instance.current()
    value = candidate(current, "0" * 64)
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("publish", payload=value))
    value = candidate(current, sha)
    row = next(r for r in value["files"] if r["path"] == "config/access.json")
    raw = b'{"version":1,"principals":[{"new":"credential"}]}'
    row.update(
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        content_base64=base64.b64encode(raw).decode(),
    )
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("publish", payload=value))
    assert events == []


@pytest.mark.parametrize(
    "state,reservation",
    [
        ("queued", None),
        ("waiting", None),
        ("running", None),
        (None, "reserved"),
        (None, "dispatched"),
    ],
)
def test_busy_work_is_not_restarted_or_dispatched_by_maintenance(operator, state, reservation):
    instance, events = operator
    db = instance.runtime_state / "ledger.sqlite3"
    with sqlite3.connect(db) as con:
        if state:
            con.execute("INSERT INTO queue_jobs VALUES('pending',?)", (state,))
        if reservation:
            con.execute("INSERT INTO reservations VALUES('inflight',?)", (reservation,))
    current, sha, _ = instance.current()
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("publish", payload=candidate(current, sha)))
    assert events == [] and instance.current()[0] == current


def test_unknown_and_holds_survive_code_update_without_replay(operator):
    instance, events = operator
    db = instance.runtime_state / "ledger.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO reservations VALUES('unknown', 'unknown')")
        con.execute("INSERT INTO queue_jobs VALUES('unknown', 'unknown')")
    before = db.read_bytes()
    current, sha, _ = instance.current()
    assert instance.handle(wire("publish", payload=candidate(current, sha)))["status"] == "complete"
    assert db.read_bytes() == before and events.count("start") == 1


def test_readiness_failure_stays_unknown_and_blocks_a_new_operation(operator, monkeypatch):
    instance, _events = operator
    current, sha, _ = instance.current()
    value = candidate(current, sha)
    calls = []

    def verify(manifest):
        calls.append(1)
        if len(calls) > 1:
            raise maintenance.Refused("fixture readiness failure")

    monkeypatch.setattr(maintenance, "verify_service", verify)
    result = instance.handle(wire("publish", payload=value))
    assert result["status"] == "unknown"
    status = instance.handle(wire("status", operation_id=value["operation_id"]))
    assert status["status"] == "unknown" and status["operation_id"] == value["operation_id"]
    assert instance.handle(wire("status"))["status"] == "reconcile_required"
    now, new_sha, _ = instance.current()
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("publish", payload=candidate(now, new_sha, release="third-release")))


def test_job_arriving_during_stop_keeps_old_code_and_never_restarts(operator, monkeypatch):
    instance, events = operator
    current, sha, _ = instance.current()

    def stop():
        events.append("stop")
        with sqlite3.connect(instance.runtime_state / "ledger.sqlite3") as con:
            con.execute("INSERT INTO queue_jobs VALUES('new-arrival','queued')")

    monkeypatch.setattr(maintenance, "stop_service", stop)
    value = candidate(current, sha)
    result = instance.handle(wire("publish", payload=value))
    assert result["status"] == "unknown" and result["current"] == current
    assert result["error_code"] == "pending_jobs_require_drain"
    assert "start" not in events


def test_stop_uses_service_grace_and_requires_zero_processes(monkeypatch):
    calls = []

    def command(args, timeout=30):
        calls.append((args, timeout))
        if "show" in args:
            return b"ActiveState=inactive\nSubState=dead\nMainPID=42\nControlPID=0\nJob=\n"
        return b""

    monkeypatch.setattr(maintenance, "command", command)
    with pytest.raises(maintenance.Refused, match="service_stop_uncertain"):
        maintenance.stop_service()
    assert calls[0] == (["/usr/bin/systemctl", "stop", "quota-shared-broker.service"], 185)


def test_status_proves_preclaim_absence_only_under_clean_journal(operator):
    instance, events = operator
    operation = str(uuid.uuid4())
    current, sha, _ = instance.current()
    before = (instance.runtime_state / "ledger.sqlite3").read_bytes()
    result = instance.handle(wire("status", operation_id=operation))
    assert result == {
        "status": "not_started",
        "operation_id": operation,
        "current": current,
        "manifest_sha256": sha,
        "provider_calls": 0,
        "paid_allowed": False,
        "automatic_retry": False,
    }
    assert events == []
    assert not list(instance.journal.glob("*.json"))
    assert (instance.runtime_state / "ledger.sqlite3").read_bytes() == before


@pytest.mark.parametrize("obstruction", ["pending", "foreign_unknown", "history", "broken_receipt"])
def test_status_never_proves_absence_with_unresolved_or_operation_artifacts(operator, obstruction):
    instance, _events = operator
    operation = str(uuid.uuid4())
    if obstruction == "pending":
        (instance.journal / (str(uuid.uuid4()) + ".pending")).write_text("{}")
    elif obstruction == "history":
        (instance.journal / "history" / (operation + "-" + "a" * 64 + ".json")).write_text("{}")
    elif obstruction == "broken_receipt":
        (instance.journal / (operation + ".json")).symlink_to("missing")
    else:
        current, sha, _ = instance.current()
        instance.save(
            {
                "operation_id": str(uuid.uuid4()),
                "release_id": "new-release",
                "manifest_sha256": "b" * 64,
                "previous": current,
                "previous_manifest_sha256": sha,
                "status": "unknown",
            },
            new=True,
        )
    with pytest.raises((maintenance.Refused, OSError)):
        instance.handle(wire("status", operation_id=operation))


def config_row(payload, name, document):
    row = next(r for r in payload["files"] if r["path"] == "config/" + name)
    raw = json.dumps(document).encode()
    row.update(
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        content_base64=base64.b64encode(raw).decode(),
    )


@pytest.mark.parametrize(
    "problem", ["expiry", "missing_expiry", "naive_expiry", "capacity", "secret", "inventory_scope"]
)
def test_cross_file_configuration_refuses_bad_candidate_before_publish(operator, problem):
    instance, events = operator
    current, sha, _ = instance.current()
    payload = candidate(current, sha)
    item = {
        "enabled": True,
        "provider": "openrouter",
        "secret_ref": "OPENROUTER_API_KEY",
        "required_capacity": [{"metric": "requests", "window": "day"}],
        "verified_at": "2026-11-11T00:00:00Z",
        "expires_at": "2026-12-11T00:00:00Z",
    }
    if problem == "expiry":
        item["expires_at"] = "2026-12-11T00:01:00Z"
    elif problem == "missing_expiry":
        item.pop("expires_at")
    elif problem == "naive_expiry":
        item["expires_at"] = "2026-12-11T00:00:00"
    elif problem == "capacity":
        item["required_capacity"] = []
    elif problem == "secret":
        item["secret_ref"] = "OTHER_KEY"
    elif problem == "inventory_scope":
        config_row(
            payload,
            "secret-inventory.json",
            {
                "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
                "project": "other",
                "config": "dev",
                "verified_at": "2026-11-11T00:00:00Z",
                "expires_at": "2026-12-11T00:00:00Z",
            },
        )
    config_row(payload, "gateway.json", {"targets": [item]})
    with pytest.raises(maintenance.Refused):
        instance.handle(wire("validate", payload=payload))
    assert events == []
    assert not list(instance.journal.glob("*.json"))


@pytest.mark.parametrize("group", ["absent", "empty", "nonempty"])
def test_failed_unit_reset_requires_zero_pids_no_job_and_empty_cgroup(tmp_path, monkeypatch, group):
    root = tmp_path / "cgroup"
    system = root / "system.slice"
    system.mkdir(parents=True)
    cgroup = system / maintenance.SERVICE
    if group != "absent":
        cgroup.mkdir(mode=0o755)
        (cgroup / "cgroup.procs").write_text("42\n" if group == "nonempty" else "")
        (cgroup / "cgroup.procs").chmod(0o644)
    monkeypatch.setattr(maintenance, "CGROUP_ROOT", root)
    monkeypatch.setattr(maintenance, "OWNER", os.getuid())
    calls = []

    def command(args, timeout=30):
        calls.append(args)
        if "show" in args:
            state = "inactive" if any("reset-failed" in c for c in calls) else "failed"
            sub = "dead" if state == "inactive" else "failed"
            return f"ActiveState={state}\nSubState={sub}\nMainPID=0\nControlPID=0\nJob=\nControlGroup=/system.slice/{maintenance.SERVICE}\n".encode()
        return b""

    monkeypatch.setattr(maintenance, "command", command)
    if group == "nonempty":
        with pytest.raises(maintenance.Refused, match="service_cgroup_not_empty"):
            maintenance.stop_service()
        assert not any("reset-failed" in c for c in calls)
    else:
        maintenance.stop_service()
        assert calls[2] == ["/usr/bin/systemctl", "reset-failed", maintenance.SERVICE]
        assert len(calls) == 4


def test_idle_closes_sqlite_connection_on_success_and_failure(operator, monkeypatch):
    instance, _events = operator
    connect = sqlite3.connect
    opened = []

    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(maintenance.sqlite3, "connect", tracked)
    instance.idle()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("SELECT 1")
    with connect(instance.runtime_state / "ledger.sqlite3") as con:
        con.execute("INSERT INTO queue_jobs VALUES('job','queued')")
    with pytest.raises(maintenance.Refused):
        instance.idle()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("SELECT 1")


def test_cross_file_equal_inventory_deadline_is_valid_without_app_import(operator):
    instance, events = operator
    current, sha, _ = instance.current()
    payload = candidate(current, sha)
    config_row(
        payload,
        "gateway.json",
        {
            "targets": [
                {
                    "enabled": True,
                    "provider": "openrouter",
                    "secret_ref": "OPENROUTER_API_KEY",
                    "required_capacity": [{"metric": "requests", "window": "day"}],
                    "verified_at": "2026-11-11T00:00:00Z",
                    "expires_at": "2026-12-11T00:00:00Z",
                }
            ]
        },
    )
    assert instance.handle(wire("validate", payload=payload))["status"] == "validated"
    assert events == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("MainPID", "9"),
        ("ControlPID", "9"),
        ("Job", "9 start"),
        ("ActiveState", "active"),
        ("ControlGroup", "/system.slice/other.service"),
    ],
)
def test_failed_cleanup_never_resets_live_or_unexpected_unit(tmp_path, monkeypatch, field, value):
    root = tmp_path / "cgroup"
    (root / "system.slice").mkdir(parents=True)
    monkeypatch.setattr(maintenance, "CGROUP_ROOT", root)
    fields = {
        "ActiveState": "failed",
        "SubState": "failed",
        "MainPID": "0",
        "ControlPID": "0",
        "Job": "",
        "ControlGroup": "",
    }
    fields[field] = value
    calls = []

    def command(args, timeout=30):
        calls.append(args)
        return (
            "".join(f"{key}={value}\n" for key, value in fields.items()).encode()
            if "show" in args
            else b""
        )

    monkeypatch.setattr(maintenance, "command", command)
    with pytest.raises(maintenance.Refused):
        maintenance.stop_service()
    assert not any("reset-failed" in c for c in calls)


@pytest.mark.parametrize("child_processes", ["", "123\n"])
def test_empty_top_cgroup_never_hides_child_groups(tmp_path, monkeypatch, child_processes):
    root = tmp_path / "cgroup"
    group = root / "system.slice" / maintenance.SERVICE
    group.mkdir(parents=True, mode=0o755)
    (group / "cgroup.procs").write_text("")
    (group / "cgroup.procs").chmod(0o644)
    child = group / "child"
    child.mkdir(mode=0o755)
    (child / "cgroup.procs").write_text(child_processes)
    monkeypatch.setattr(maintenance, "CGROUP_ROOT", root)
    monkeypatch.setattr(maintenance, "OWNER", os.getuid())
    with pytest.raises(maintenance.Refused, match="service_cgroup_has_children"):
        maintenance._empty_service_cgroup("/system.slice/" + maintenance.SERVICE)


@pytest.mark.parametrize(
    "provider,ref,dimension,account_ref",
    [
        ("google", "GEMINI_API_KEY", ("requests", "day"), None),
        ("groq", "GROQ_API_KEY", ("requests", "day"), None),
        ("mistral", "MISTRAL_API_KEY", ("total_tokens", "rolling_minute"), None),
        ("nvidia", "NVIDIA_API_KEY", ("requests", "day"), None),
        ("ocrspace", "OCRSPACE_API_KEY", ("requests", "month"), None),
        ("sambanova", "SAMBANOVA_API_KEY", ("requests", "day"), None),
        ("huggingface", "HUGGINGFACE_API_TOKEN", ("credits_usd_micros", "budget"), None),
        ("cerebras", "CEREBRAS_API_KEY", ("requests", "day"), None),
        ("cloudflare", "CLOUDFLARE_API_TOKEN", ("neurons", "day"), "CLOUDFLARE_ACCOUNT_ID"),
    ],
)
def test_extended_provider_configuration_has_exact_enabled_inventory(
    provider, ref, dimension, account_ref
):
    payload = candidate("baseline", "0" * 64)
    item = {
        "enabled": True,
        "provider": provider,
        "secret_ref": ref,
        "required_capacity": [dict(zip(("metric", "window"), dimension, strict=True))],
        "verified_at": "2026-11-11T00:00:00Z",
        "expires_at": "2026-12-11T00:00:00Z",
    }
    names = [ref]
    if account_ref:
        item["account_id_ref"] = account_ref
        names.append(account_ref)
    config_row(payload, "gateway.json", {"targets": [item]})
    inventory = {
        "names": names,
        "project": "api-quota-broker",
        "config": "dev",
        "verified_at": "2026-11-11T00:00:00Z",
        "expires_at": "2026-12-11T00:00:00Z",
    }
    config_row(payload, "secret-inventory.json", inventory)
    assert maintenance.validate(payload)
    inventory["names"] = names + ["OPENAI_API_KEY"]
    config_row(payload, "secret-inventory.json", inventory)
    with pytest.raises(maintenance.Refused, match="inventory_scope"):
        maintenance.validate(payload)


@pytest.mark.parametrize("change", ["key", "account_ref", "capacity", "duplicate", "missing"])
def test_cloudflare_configuration_rejects_binding_and_inventory_gaps(change):
    payload = candidate("baseline", "0" * 64)
    target = {
        "enabled": True,
        "provider": "cloudflare",
        "secret_ref": "CLOUDFLARE_API_TOKEN",
        "account_id_ref": "CLOUDFLARE_ACCOUNT_ID",
        "required_capacity": [{"metric": "neurons", "window": "day"}],
        "verified_at": "2026-11-11T00:00:00Z",
        "expires_at": "2026-12-11T00:00:00Z",
    }
    names = ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]
    if change == "key":
        target["secret_ref"] = "GROQ_API_KEY"
    elif change == "account_ref":
        target["account_id_ref"] = "GROQ_API_KEY"
    elif change == "capacity":
        target["required_capacity"] = [{"metric": "requests", "window": "day"}]
    elif change == "duplicate":
        names.append("CLOUDFLARE_ACCOUNT_ID")
    elif change == "missing":
        names.pop()
    config_row(payload, "gateway.json", {"targets": [target]})
    config_row(
        payload,
        "secret-inventory.json",
        {
            "names": names,
            "project": "api-quota-broker",
            "config": "dev",
            "verified_at": "2026-11-11T00:00:00Z",
            "expires_at": "2026-12-11T00:00:00Z",
        },
    )
    with pytest.raises(maintenance.Refused):
        maintenance.validate(payload)
