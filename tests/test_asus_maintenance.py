"""Real SQLite/file receipts and generated privileged entry boundaries."""

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tarfile
import types
from pathlib import Path

import pytest
from asus_packet_fixtures import normal_review

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ops = load("maintenance_ops")
protocol = load("maintenance_protocol")
builder = load("build_maintenance_review")


@pytest.fixture(scope="module")
def portable_normal_review(tmp_path_factory):
    return normal_review(tmp_path_factory.mktemp("normal-public-review"))


@pytest.fixture(autouse=True)
def portable_maintenance_packet(portable_normal_review, monkeypatch):
    monkeypatch.setattr(builder, "NORMAL", portable_normal_review)


RID = "a" * 32


def req(operation="deploy"):
    return {"operation": operation, "request_id": RID}


@pytest.mark.parametrize("operation", sorted(protocol.OPERATIONS))
def test_generated_entry_accepts_only_fixed_id_operations(operation):
    module = types.ModuleType("maintenance_test_entry")
    exec(compile(builder.entry_source(), "generated_ops_entry.py", "exec"), module.__dict__)  # noqa: S102 - reviewed generated code
    assert module.request(protocol.canonical(req(operation))) == req(operation)
    assert (
        module.diagnostic("helper_request", module.Denied("operation_denied"), req=req(operation))[
            "request_id"
        ]
        == RID
    )
    assert module.plan()["restart_enabled"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"operation": "shell", "request_id": RID},
        {"operation": "deploy", "request_id": "../../etc/passwd"},
        {"operation": "deploy", "request_id": RID, "path": "/tmp/evil"},
        {"operation": "deploy", "request_id": RID, "env": {}},
        {"operation": "deploy", "request_id": RID, "unit": "ssh.service"},
        {"operation": "deploy", "request_id": RID, "ExecStart": "/bin/sh"},
    ],
)
def test_wire_rejects_unbounded_surface(bad):
    with pytest.raises(ValueError):
        protocol.request(bad)


def test_receipt_rejects_secret_reflection():
    answer = protocol.receipt(req(), "passed", "ok")
    answer["message"] = "a secret value"
    with pytest.raises(ValueError):
        protocol.validate(answer, req())


class Entry:
    strict_json = staticmethod(protocol.strict)

    def __init__(self):
        self.commands = []

    def load_public_module(self, name, label):
        return protocol if name == "maintenance_protocol.py" else ops

    def read_root(self, path, **_):
        return path.read_bytes()

    def write_exclusive(self, path, raw, **_):
        with open(path, "xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def root_dir(self, *_, **__):
        pass

    def native(self, argv, **_):
        self.commands.append(argv)
        return b"inactive\n" if "show" in argv else b""


def test_dispatch_unknown_is_never_resent(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "STATE", tmp_path)
    entry = Entry()
    answer = ops.dispatch(entry, req())
    assert answer["maintenance_state"] == "pending"
    original = (tmp_path / (RID + ".request.json")).read_bytes()
    assert ops.dispatch(entry, req())["maintenance_state"] == "unknown"
    assert (tmp_path / (RID + ".request.json")).read_bytes() == original
    assert sum("start" in c for c in entry.commands) == 1
    assert ops.dispatch(entry, req("operation_status"))["maintenance_state"] == "unknown"
    assert sum("start" in c for c in entry.commands) == 1


def test_receipt_binds_original_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "STATE", tmp_path)
    entry = Entry()
    ops.dispatch(entry, req())
    entry.write_exclusive(
        tmp_path / (RID + ".result.json"),
        protocol.canonical(protocol.receipt(req(), "passed", "ok")),
    )
    assert ops.dispatch(entry, req("operation_status"))["maintenance_state"] == "passed"
    with pytest.raises(ValueError, match="request_conflict"):
        ops.dispatch(entry, req("restart"))
    assert sum("start" in c for c in entry.commands) == 1


def db():
    con = sqlite3.connect(":memory:")
    con.executescript("""CREATE TABLE queue_jobs(state TEXT,lease_owner TEXT,lease_token TEXT,
        lease_until TEXT,execution_until TEXT,run_started INT,execution_key TEXT,payload BLOB);
        CREATE TABLE queue_attempts(execution_key TEXT);
        CREATE TABLE queue_settings(id INT,verifier BLOB);
        INSERT INTO queue_settings VALUES(1,X'1234');""")
    return con


@pytest.mark.parametrize("state", ["queued", "waiting", "running", "unknown", "failed"])
def test_maintenance_never_resumes_unsettled_queue(state):
    with db() as con:
        con.execute("INSERT INTO queue_jobs(state) VALUES(?)", (state,))
        with pytest.raises(ValueError, match="queue_not_quiescent"):
            ops.queue_gate(con, first=False)
        assert con.execute("SELECT state FROM queue_jobs").fetchone()[0] == state


def test_later_maintenance_allows_history_but_initial_activation_does_not():
    with db() as con:
        con.execute("INSERT INTO queue_jobs(state) VALUES('completed')")
        assert ops.queue_gate(con, first=False) == 1
        with pytest.raises(ValueError, match="queue_not_quiescent"):
            ops.queue_gate(con, first=True)


def test_empty_existing_queue_and_key_survive():
    with db() as con:
        assert ops.queue_gate(con, first=True) == 0
        assert con.execute("SELECT hex(verifier) FROM queue_settings").fetchone()[0] == "1234"


def test_profile_matches_already_verified_app_planner():
    # Use the established config fixture, never an actual credential or remote request.
    plan = load("normal_pool_plan")
    seven = load("seven_pool_plan")
    base = load("provider_pool_plan")
    evidence = json.loads(
        (ROOT / "tests/fixtures/asus-history/seven-r2-admission.json").read_bytes()
    )
    old = seven.config(
        base, set(seven.PROVIDERS), evidence["admission"], "2026-11-02T03:36:17+00:00", normal=True
    )
    assert ops.normal_config(old) == plan.config(old)
    assert all(
        t["max_output_tokens"] > 64
        for t in ops.normal_config(old)["targets"]
        if t["provider"] != "ocrspace"
    )


def test_packet_keeps_r15_modules_and_roots_exporter_outside_release():
    files = builder.packet()
    manifest = json.loads(files["manifest.json"])
    assert set(manifest["files"]) >= {
        "ops_entry.py",
        "broker_ops_policy.py",
        "history_audit_reader.py",
        "history_audit_protocol.py",
        "ops_history_projection.py",
    }
    for name, digest in manifest["files"].items():
        assert builder.sha(files[name]) == digest
    client = files["client-v1.service"].decode()
    assert "/opt/api-quota-broker/current/deploy/asus/export_client_credential.py" not in client
    assert "/usr/local/lib/api-quota-broker-ops/export_client_credential.py" in client
    unit = files["api-quota-broker-maintenance@.service"].decode()
    assert "PrivateTmp=yes" not in unit
    assert "TemporaryFileSystem=/tmp:rw,mode=0700,size=32M" in unit
    assert "ReadOnlyPaths=/var/lib/api-quota-broker /var/backups/api-quota-broker" in unit
    assert "LoadCredential" not in unit
    assert (
        files["ops_history_projection.py"]
        == (ROOT / "deploy/asus/ops_history_projection.py").read_bytes()
    )


def test_archive_rejects_link_and_traversal():
    spec = importlib.util.spec_from_file_location(
        "maintenance_release", ROOT / "scripts/build_asus_release.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    profile = json.loads(builder.packet()["maintenance-profile.json"])
    raw = (builder.NORMAL / "source.tar").read_bytes()
    verifier.verify_archive(raw, profile["manifest_sha256"])
    for name, kind, link in [
        ("../escape", tarfile.REGTYPE, ""),
        ("src/quota_broker/cli.py", tarfile.SYMTYPE, "/etc/shadow"),
    ]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            archive.addfile(info, io.BytesIO(b""))
        with pytest.raises(ValueError):
            verifier.verify_archive(buf.getvalue(), profile["manifest_sha256"])


def test_wheel_identity_matches_release():
    files = builder.packet()
    with tarfile.open(builder.NORMAL / "source.tar") as archive:
        manifest = json.loads(archive.extractfile("release-manifest.json").read())
    wheel = ops.wheel_files(files["project.whl"], manifest["files"])
    assert b"Version: 1.0.0\n" in wheel["api_quota_broker-1.0.0.dist-info/METADATA"]


def test_transaction_failure_restores_files_not_database():
    events = []

    class Backend:
        changed = False
        legacy_clear = True
        jobs = 0

        def __init__(self, *_):
            pass

        def preflight(self, **_):
            events.append("preflight")

        def deploy(self):
            self.changed = True
            events.append("new_ledger_row")
            raise ValueError("native_failed")

        def restore(self):
            events.append("restore_configuration_only")

    value = ops.execute(Entry(), req(), Backend)
    assert value["maintenance_state"] == "blocked"
    assert value["original_restored"] is True
    assert value["database_restored"] is False
    assert events == ["preflight", "new_ledger_row", "restore_configuration_only"]


def test_rollback_failure_stops_only_broker():
    commands = []

    class Backend:
        changed = False
        legacy_clear = True
        jobs = 0

        def __init__(self, *_):
            pass

        def preflight(self, **_):
            pass

        def deploy(self):
            self.changed = True
            raise ValueError("native_failed")

        def restore(self):
            raise ValueError("foreign_change")

        def command(self, *args):
            commands.append(args)

    value = ops.execute(Entry(), req(), Backend)
    assert value["code"] == "rollback_unverified"
    assert commands == [("stop", "api-quota-broker.service")]


def test_terminal_execution_deadline_is_history_not_active_lease():
    with db() as con:
        con.execute(
            "INSERT INTO queue_jobs(state,execution_until) VALUES('completed','2026-10-09T00:00:00Z')"
        )
        assert ops.queue_gate(con, first=False) == 1


def test_preservation_checks_old_rows_and_retains_new_holds(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    original = backup / "ledger.sqlite3"
    live = tmp_path / "live.sqlite3"
    tables = (
        "gateway_tasks",
        "gateway_attempts",
        "reservations",
        "charges",
        "execution_completion",
        "queue_jobs",
        "queue_attempts",
        "queue_settings",
    )
    with sqlite3.connect(original) as con:
        for table in tables:
            con.execute(f"CREATE TABLE {table}(value TEXT)")
            con.execute(f"INSERT INTO {table} VALUES(?)", ("original hold",))
        con.commit()
        with sqlite3.connect(live) as other:
            con.backup(other)
    value = object.__new__(ops.Native)
    value.backup = backup
    value.recovery = False
    value.db = lambda: sqlite3.connect(live)
    with sqlite3.connect(live) as con:
        con.execute("INSERT INTO reservations VALUES('new unknown hold')")
    value.preserved_rows()
    with sqlite3.connect(live) as con:
        assert con.execute("SELECT count(*) FROM reservations").fetchone()[0] == 2
        con.execute("UPDATE charges SET value='changed' WHERE rowid=1")
    with pytest.raises(ValueError, match="foreign_change"):
        value.preserved_rows()


@pytest.fixture
def filesystem_transaction(tmp_path, monkeypatch):
    import pwd
    import subprocess

    # Transaction fixture is unprivileged; native UID/capability behavior has its
    # own tests and sealed ASUS r3 receipt, rather than pretending this is root.
    monkeypatch.setattr(ops, "runtime_capabilities", lambda: None)

    names = {
        "BASE": "ops",
        "STATE": "state/maintenance",
        "APP": "app",
        "SYSTEM": "system",
        "CONFIG": "etc/gateway.json",
        "POLICY": "policy.json",
        "ACTIVE": "active.json",
        "DB": "ledger.sqlite3",
        "BACKUPS": "backups",
    }
    for name, relative in names.items():
        monkeypatch.setattr(ops, name, tmp_path / relative)
    for path in (
        ops.BASE,
        ops.STATE,
        ops.APP / "releases",
        ops.SYSTEM,
        ops.CONFIG.parent,
        ops.BACKUPS,
    ):
        path.mkdir(parents=True, exist_ok=True)
    files = builder.packet()
    for name in json.loads(files["manifest.json"])["files"]:
        (ops.BASE / name).write_bytes(files[name])
    ops.ACTIVE.write_bytes(files["active-release.json"])
    original_runtime = ops.APP / ops.OLD / "runtime"
    site = original_runtime / "lib/python3.14/site-packages"
    (site / "quota_broker").mkdir(parents=True)
    (site / "quota_broker/__init__.py").write_text("__version__='0.1.0'\n")
    (site / "api_quota_broker-0.1.0.dist-info").mkdir()
    (site / "api_quota_broker-0.1.0.dist-info/METADATA").write_text("Version: 0.1.0\n")
    (original_runtime / "bin").mkdir()
    for name, target in {
        "lib64": "lib",
        "bin/python": "/usr/bin/python3.14",
        "bin/python3": "python",
        "bin/python3.14": "python",
    }.items():
        (original_runtime / name).symlink_to(target)
    (original_runtime / "bin/quota-broker").write_text("old script")
    (ops.APP / "current").symlink_to(ops.OLD)
    plan = load("seven_pool_plan")
    base = load("provider_pool_plan")
    policy = json.loads((ROOT / "tests/fixtures/asus-history/seven-r2-admission.json").read_bytes())
    old = plan.config(
        base, set(plan.PROVIDERS), policy["admission"], "2026-11-02T03:36:17+00:00", normal=True
    )
    original = ops.wire(old)
    ops.CONFIG.write_bytes(original)
    ops.POLICY.write_bytes(
        ops.wire({"config_sha256": ops.digest(original), "expires_at": "2026-11-05T04:33:31+00:00"})
    )
    (ops.SYSTEM / ops.SERVICE).write_bytes(b"old broker unit")
    monkeypatch.setattr(ops, "OLD_CONFIG", ops.digest(original))
    metadata = ops.CONFIG.parent / "credentials"
    metadata.mkdir()
    (metadata / "doppler-metadata.json").write_bytes(
        ops.wire(
            {
                "project": "api-quota-broker",
                "config": "dev",
                "access": "read",
                "human_dashboard_attested": True,
                "expires_at": "2026-11-02T03:36:17+00:00",
            }
        )
    )
    con = db()
    for table in (
        "gateway_tasks",
        "gateway_attempts",
        "reservations",
        "charges",
        "execution_completion",
    ):
        con.execute(f"CREATE TABLE {table}(value TEXT)")
        con.execute(f"INSERT INTO {table} VALUES(?)", ("original " + table,))
    con.commit()
    with sqlite3.connect(ops.DB) as target:
        con.backup(target)
    con.close()
    spec = importlib.util.spec_from_file_location(
        "maintenance_fixture_verifier", ROOT / "scripts/build_asus_release.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    archive = (builder.NORMAL / "source.tar").read_bytes()
    monkeypatch.setattr(ops.Native, "upload", lambda _: (archive, verifier))
    # Only route writes for the one fixed wrapper to the fixture namespace.
    original_path = ops.Path

    def path(value):
        return tmp_path / "aqb" if str(value) == "/usr/local/bin/aqb" else original_path(value)

    monkeypatch.setattr(ops, "Path", path)
    commands = []
    monkeypatch.setattr(pwd, "getpwnam", lambda _: types.SimpleNamespace(pw_uid=1000, pw_gid=1000))

    def run(argv, **kwargs):
        assert kwargs["user"] == kwargs["group"] == 1000
        assert kwargs["extra_groups"] == []
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ops.subprocess, "run", run)

    class Files(Entry):
        def runtime_policy(self):
            return json.loads(ops.POLICY.read_bytes())

        def package(self):
            pass

        def broker_pins(self, config_sha):
            assert ops.digest(ops.CONFIG.read_bytes()) == config_sha

        def service_state(self, *_):
            return {"fixture": "active"}

    return Files(), commands, tmp_path


def test_actual_file_deploy_and_rollback_keep_new_ledger(filesystem_transaction):
    entry, commands, root = filesystem_transaction
    original = ops.CONFIG.read_bytes()
    value = ops.execute(entry, req())
    assert value["maintenance_state"] == "passed", value
    assert os.readlink(ops.APP / "current") != ops.OLD
    assert (root / "aqb").exists()
    assert commands and all(call[1]["user"] == 1000 for call in commands)
    assert json.loads(ops.CONFIG.read_bytes())["targets"][0]["max_output_tokens"] > 64
    with sqlite3.connect(ops.DB) as con:
        con.execute("INSERT INTO reservations VALUES('new unknown hold')")
    rollback = ops.execute(entry, {"operation": "rollback", "request_id": "b" * 32})
    assert rollback["maintenance_state"] == "passed", rollback
    assert rollback["original_restored"] is True
    assert ops.CONFIG.read_bytes() == original
    assert os.readlink(ops.APP / "current") == ops.OLD
    assert not (root / "aqb").exists()
    with sqlite3.connect(ops.DB) as con:
        assert con.execute("SELECT value FROM reservations ORDER BY rowid").fetchall() == [
            ("original reservations",),
            ("new unknown hold",),
        ]


def test_partial_file_install_rolls_back_and_keeps_receipts(filesystem_transaction, monkeypatch):
    entry, _, root = filesystem_transaction
    original = entry.write_exclusive

    def failure(path, raw, **kwargs):
        if path == root / "aqb":
            raise OSError("simulated disk failure")
        return original(path, raw, **kwargs)

    monkeypatch.setattr(entry, "write_exclusive", failure)
    outcome = ops.execute(entry, req())
    assert outcome["maintenance_state"] == "blocked"
    assert outcome["original_restored"] is True
    assert os.readlink(ops.APP / "current") == ops.OLD
    assert not (ops.SYSTEM / ops.CLIENT).exists()
    assert (ops.STATE / "deployment.json").exists()
    assert (ops.STATE / ("backup-" + RID) / "ledger.sqlite3").exists()


REAL_UPLOAD = ops.Native.upload


def later_packet():
    """A different code release with hostile privileged helper/unit DATA."""
    import base64
    import csv
    import zipfile

    files = builder.packet()
    with tarfile.open(builder.NORMAL / "source.tar") as archive:
        payload = {m.name: archive.extractfile(m).read() for m in archive}
    manifest = json.loads(payload.pop("release-manifest.json"))
    payload["src/quota_broker/__init__.py"] += b"\n# authorized later code-only fixture\n"
    payload["deploy/asus/api-quota-broker-client.service"] = b"[Service]\nExecStart=/bin/false\n"
    payload["deploy/asus/export_client_credential.py"] = (
        b'raise RuntimeError("must never execute as root")\n'
    )
    for row in manifest["files"]:
        raw = payload[row["path"]]
        row.update(size=len(raw), sha256=builder.sha(raw))
    spec = importlib.util.spec_from_file_location(
        "later_verifier", ROOT / "scripts/build_asus_release.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    archive = verifier._tar(payload, verifier._canonical(manifest))
    with zipfile.ZipFile(io.BytesIO(files["project.whl"])) as wheel:
        content = {name: wheel.read(name) for name in wheel.namelist()}
    content["quota_broker/__init__.py"] = payload["src/quota_broker/__init__.py"]
    record = "api_quota_broker-1.0.0.dist-info/RECORD"
    rows = []
    for name, raw in content.items():
        rows.append(
            [name, "", ""]
            if name == record
            else [
                name,
                "sha256="
                + base64.urlsafe_b64encode(bytes.fromhex(builder.sha(raw))).decode().rstrip("="),
                str(len(raw)),
            ]
        )
    text = io.StringIO()
    csv.writer(text).writerows(rows)
    content[record] = text.getvalue().encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as wheel:
        for name, raw in content.items():
            wheel.writestr(name, raw)
    return archive, buf.getvalue(), verifier


def test_repeated_code_deploy_does_not_install_uploaded_root_code(
    filesystem_transaction, monkeypatch
):
    entry, _, root = filesystem_transaction
    first = ops.execute(entry, req())
    assert first["maintenance_state"] == "passed"
    first_current = os.readlink(ops.APP / "current")
    trusted_unit = (ops.SYSTEM / ops.CLIENT).read_bytes()
    trusted_exporter = (ops.BASE / "export_client_credential.py").read_bytes()
    later = "c" * 32
    incoming = root / ("aqb-release-" + later)
    incoming.mkdir(mode=0o700)
    archive, wheel, verifier = later_packet()
    for name, raw in [("source.tar", archive), ("project.whl", wheel)]:
        (incoming / name).write_bytes(raw)
        (incoming / name).chmod(0o600)
    prior_path = ops.Path
    monkeypatch.setattr(
        ops,
        "Path",
        lambda p: incoming if str(p) == "/var/tmp/aqb-release-" + later else prior_path(p),
    )
    prior_loader = entry.load_public_module
    monkeypatch.setattr(
        entry,
        "load_public_module",
        lambda n, l: verifier if n == "release_verifier.py" else prior_loader(n, l),
    )
    monkeypatch.setattr(ops.Native, "upload", REAL_UPLOAD)
    second = ops.execute(entry, {"operation": "deploy", "request_id": later})
    assert second["maintenance_state"] == "passed", second
    assert os.readlink(ops.APP / "current") != first_current
    assert (ops.SYSTEM / ops.CLIENT).read_bytes() == trusted_unit
    assert (ops.BASE / "export_client_credential.py").read_bytes() == trusted_exporter
    assert (root / "aqb").exists()
    assert (ops.STATE / ("backup-" + RID) / "ledger.sqlite3").exists()
    assert (ops.STATE / ("backup-" + later) / "ledger.sqlite3").exists()
    restored = ops.execute(entry, {"operation": "rollback", "request_id": "d" * 32})
    assert restored["maintenance_state"] == "passed", restored
    assert os.readlink(ops.APP / "current") == first_current
    assert (ops.SYSTEM / ops.CLIENT).read_bytes() == trusted_unit
    assert (root / "aqb").exists()


def test_new_mutation_cannot_bypass_unknown_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "STATE", tmp_path)
    entry = Entry()
    ops.dispatch(entry, req())
    later = {"operation": "deploy", "request_id": "b" * 32}
    answer = ops.dispatch(entry, later)
    assert answer["maintenance_state"] == "blocked" and answer["code"] == "interrupted"
    assert not (tmp_path / ("b" * 32 + ".request.json")).exists()
    assert sum("start" in c for c in entry.commands) == 1


def test_missing_setuid_stops_before_candidate_write(monkeypatch):
    monkeypatch.setattr(ops.Path, "read_text", lambda self: "CapEff:\t00000040\n")
    monkeypatch.setattr(
        ops.Native, "upload", lambda self: pytest.fail("must not stage without capability")
    )
    with pytest.raises(ValueError, match="runtime_invalid"):
        ops.Native.stage(object.__new__(ops.Native))


def test_authorized_continuation_preserves_original_and_blocks_new_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "STATE", tmp_path)
    entry = Entry()
    original = {"operation": "deploy", "request_id": ops.REPAIR_ID}
    receipt = ops.wire(
        protocol.receipt(original, "blocked", "internal_error", legacy_clear=True, queue_jobs=0)
    )
    entry.write_exclusive(tmp_path / (ops.REPAIR_ID + ".request.json"), ops.wire(original))
    entry.write_exclusive(tmp_path / (ops.REPAIR_ID + ".result.json"), receipt)
    entry.write_exclusive(tmp_path / (ops.REPAIR_ID + ".started"), b"1\n")
    permit = {
        "schema": 1,
        "request_id": ops.REPAIR_ID,
        "original_result_sha256": ops.digest(receipt),
        "manifest_sha256": ops.REPAIR_MANIFEST,
        "archive": "releases/failed-stage-" + ops.REPAIR_ID,
    }
    entry.write_exclusive(tmp_path / (ops.REPAIR_ID + ".repair-authorized.json"), ops.wire(permit))
    assert ops.result(entry, original)["maintenance_state"] == "unknown"
    assert ops.dispatch(entry, req())["code"] == "interrupted"
    assert not any("start" in c for c in entry.commands)
    entry.write_exclusive(
        tmp_path / (ops.REPAIR_ID + ".repair-result.json"),
        ops.wire(protocol.receipt(original, "passed", "ok", legacy_clear=True, queue_jobs=0)),
    )
    assert ops.result(entry, original)["maintenance_state"] == "passed"
    assert (tmp_path / (ops.REPAIR_ID + ".result.json")).read_bytes() == receipt
    assert (tmp_path / (ops.REPAIR_ID + ".started")).read_bytes() == b"1\n"
    permit["manifest_sha256"] = "0" * 64
    (tmp_path / (ops.REPAIR_ID + ".repair-authorized.json")).write_bytes(ops.wire(permit))
    with pytest.raises(ValueError, match="scope_invalid"):
        ops.result(entry, original)
