"""Standalone public fixture validation. No SSH, root, credential, or provider IO."""

import ast
import ctypes
import hashlib
import io
import json
import os
import resource
import shlex
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType


def need(ok):
    if not ok:
        raise ValueError("offline_gate")


def read(path, *, limit=262144):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == os.getuid()
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1
            and before.st_size <= limit
        )
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        need(
            len(raw) == before.st_size
            and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
        )
        return raw


def fixtures(module):
    """Actual SQLite readonly enforcement; synthetic rows only."""
    with tempfile.TemporaryDirectory(prefix="aqb-history-public-") as directory:
        db = Path(directory) / "fixture.sqlite3"
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE gateway_tasks(request_key TEXT PRIMARY KEY,state TEXT,provider TEXT,
                  model TEXT,reservation_id TEXT,dispatched_at TEXT,http_status INTEGER,
                  reported_input_tokens INTEGER,reported_output_tokens INTEGER);
                CREATE TABLE gateway_attempts(request_key TEXT,attempt_no INTEGER,reservation_id TEXT,
                  provider TEXT,state TEXT,dispatched_at TEXT,http_status INTEGER,
                  reported_input_tokens INTEGER,reported_output_tokens INTEGER);
                CREATE TABLE reservations(id TEXT PRIMARY KEY,target_id TEXT,state TEXT,
                  target_snapshot TEXT,shared_scope TEXT,dispatched_at TEXT);
                CREATE TABLE charges(reservation_id TEXT,bucket TEXT,metric TEXT,amount INTEGER);
                CREATE TABLE execution_completion(reservation_id TEXT,finished_at TEXT);
                CREATE TABLE queue_jobs(request_key TEXT,state TEXT,payload BLOB);
                CREATE TABLE queue_attempts(request_key TEXT);
            """)
            for key, provider, state, rid, usage in (
                (module.GROQ_KEY, "groq", "completed", "g", (76, 25)),
                ("PUBLIC_CF", "cloudflare", "completed_usage_unknown", "c", (5, 2)),
                ("PUBLIC_NV", "nvidia", "unknown", "n", (None, None)),
                ("PUBLIC_UNSENT", "mistral", "reserved", "m", (None, None)),
            ):
                model = "openai/gpt-oss-20b" if provider == "groq" else "PUBLIC_MODEL"
                when = None if state == "reserved" else "PUBLIC_UTC"
                con.execute(
                    "INSERT INTO gateway_tasks VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, state, provider, model, rid, when, 200, *usage),
                )
                con.execute(
                    "INSERT INTO gateway_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, 0, rid, provider, state, when, 200, *usage),
                )
                con.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                    (
                        rid,
                        "PUBLIC_TARGET",
                        "unknown" if state == "completed_usage_unknown" else state,
                        json.dumps({"provider": provider}),
                        provider + ":PUBLIC_SCOPE",
                        when,
                    ),
                )
                if state in {"completed", "completed_usage_unknown"}:
                    con.execute("INSERT INTO execution_completion VALUES(?,?)", (rid, "PUBLIC_UTC"))
            for bucket, metric, amount in (
                ("rpm", "requests", 1),
                ("rpd", "requests", 1),
                ("tpm", "input_tokens", 76),
            ):
                con.execute(
                    "INSERT INTO charges VALUES(?,?,?,?)",
                    ("g", "groq:asus-dev-key:" + bucket, metric, amount),
                )
            con.execute(
                "INSERT INTO queue_jobs VALUES(?,?,?)",
                ("PUBLIC_ROW", "completed", b"PUBLIC_PAYLOAD_MUST_NOT_PROJECT"),
            )
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            report = module.classify_ledger(con, set())
            try:
                con.execute("DELETE FROM gateway_tasks")
            except sqlite3.OperationalError:
                pass
            else:
                raise ValueError("readonly_not_enforced")
        need(
            report["classification"]
            == {
                "known_groq_settled": 1,
                "not_dispatched": 1,
                "dispatched_known_result": 1,
                "dispatch_or_settlement_unknown": 1,
            }
        )
        need(report["blocked_providers"] == ["cloudflare", "mistral", "nvidia"])
        need(not report["probe_permission"] and "PUBLIC_PAYLOAD" not in json.dumps(report))
        need(hashlib.sha256(db.read_bytes()).hexdigest() == before)
        return {
            "readonly_sqlite_enforced": True,
            "database_bytes_preserved": True,
            "four_classes": True,
            "zero_charge_completed_usage_unknown_quarantined": True,
        }


def verify(directory):
    directory = Path(directory)
    info = directory.lstat()
    need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )
    seal = json.loads(read(directory / "seal.json"))
    need(
        set(seal) == {"schema", "mode", "files", "native_authorized", "apply_supported"}
        and seal["schema"] == 1
        and seal["mode"] == "private_history_readonly_review"
        and seal["native_authorized"] is False
        and seal["apply_supported"] is False
    )
    names = {
        "history-audit-payload.tar",
        "verify_history_audit_offline.py",
        "history-audit-once.sh",
    }
    need(
        set(seal["files"]) == names - {"history-audit-once.sh"}
        and {p.name for p in directory.iterdir()} == names | {"seal.json"}
    )
    files = {name: read(directory / name) for name in names}
    need(all(hashlib.sha256(files[name]).hexdigest() == sha for name, sha in seal["files"].items()))
    shell = files["history-audit-once.sh"].decode()
    need(shell.count("exec /usr/bin/sudo -n") == 1)
    words = shlex.split(shell[shell.index("exec /usr/bin/sudo -n") :])
    code = words[words.index("-c") + 1]
    syntax = ast.parse(code)
    assignments = [
        node
        for node in syntax.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "expected" for target in node.targets)
    ]
    need(len(assignments) == 1)
    expected = ast.literal_eval(assignments[0].value)
    need(
        expected
        == {**seal["files"], "seal.json": hashlib.sha256(read(directory / "seal.json")).hexdigest()}
    )
    compile(code, "<root-copy-syntax-only>", "exec")
    with tarfile.open(fileobj=io.BytesIO(files["history-audit-payload.tar"]), mode="r:") as archive:
        rows = archive.getmembers()
        need(len(rows) == 1)
        row = rows[0]
        need(
            row.name == "integrate_provider_pool.py"
            and row.isfile()
            and row.uid == row.gid == 0
            and row.mode == 0o600
            and row.mtime == 0
            and not row.pax_headers
            and row.size <= 131072
        )
        source = archive.extractfile(row).read()
    module = ModuleType("history_audit_public_fixture")
    exec(compile(source, "<pinned-history-audit>", "exec"), module.__dict__)  # noqa: S102 - verified public source.
    # Install after imports. Default plan must not read private files, connect to
    # a DB, open a socket, spawn children, or execute an OS command.
    armed = [False]

    def guard(event, _args):
        if (
            event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
            or event == "os.system"
        ):
            raise ValueError("forbidden_offline_effect")
        if armed[0] and event in {"open", "sqlite3.connect", "os.listdir", "os.scandir"}:
            raise ValueError("default_plan_io")

    sys.addaudithook(guard)
    output, previous = io.StringIO(), sys.argv
    try:
        sys.argv = ["PUBLIC"]
        armed[0] = True
        with redirect_stdout(output):
            need(module.main() == 0)
    finally:
        armed[0] = False
        sys.argv = previous
    need(json.loads(output.getvalue())["apply"] is False)
    checks = fixtures(module)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    need(libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0)
    need(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))
    return {
        "status": "passed",
        "mode": "history_audit_offline_public_fixture",
        "python": ".".join(map(str, sys.version_info[:3])),
        "files_verified": 4,
        "checks": checks,
        "default_plan_io": 0,
        "kernel_core_zero": True,
        "kernel_dumpable_zero": True,
        "credential_reads": 0,
        "provider_calls": 0,
        "production_private_reads": 0,
        "production_db_writes": 0,
        "service_commands": 0,
        "native_audit_executed": False,
        "apply_executed": False,
    }


if __name__ == "__main__":
    try:
        need(len(sys.argv) == 3 and sys.argv[1] == "--directory")
        print(json.dumps(verify(sys.argv[2]), sort_keys=True))
    except BaseException:  # noqa: BLE001 - never project exception text or paths.
        print('{"status":"blocked","code":"offline_validation_unverified","automatic_retry":false}')
        raise SystemExit(1) from None
