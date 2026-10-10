"""Standalone sealed public-fixture verifier. Default plan has no host effects."""

import ctypes
import hashlib
import importlib.util
import io
import json
import os
import re
import resource
import sqlite3
import stat
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


def need(ok):
    if not ok:
        raise ValueError("offline_history_ops_gate")


def verify(directory, wrapper_sha256):
    directory = Path(directory)
    meta = directory.lstat()
    need(
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == os.getuid()
        and stat.S_IMODE(meta.st_mode) == 0o700
    )

    def read(name):
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and info.st_size <= 131072
            )
            raw = stream.read(131073)
            after = os.fstat(stream.fileno())
            need(
                len(raw) == info.st_size
                and (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            )
            return raw

    seal = json.loads(read("seal.json"))
    need(
        type(wrapper_sha256) is str
        and re.fullmatch(r"[a-f0-9]{64}", wrapper_sha256)
        and hashlib.sha256(read("history-ops-upgrade-once.sh")).hexdigest() == wrapper_sha256
    )
    names = {
        "history_ops_entry.py",
        "history_audit_reader.py",
        "history_audit_protocol.py",
        "ops_history_projection.py",
        "api-quota-broker-history-audit@.service",
        "history-api-quota-broker-ops@.service",
        "broker_ops_policy.py",
        "r14_ops_entry.py",
        "install_history_ops.py",
        "verify_history_ops_offline.py",
    }
    need(set(seal) == {"schema", "files"} and seal["schema"] == 1 and set(seal["files"]) == names)
    need(
        {path.name for path in directory.iterdir()}
        == names | {"seal.json", "history-ops-upgrade-once.sh"}
    )
    raw = {name: read(name) for name in names}
    need(
        all(
            hashlib.sha256(raw[name]).hexdigest() == digest
            for name, digest in seal["files"].items()
        )
    )
    need(
        hashlib.sha256(raw["r14_ops_entry.py"]).hexdigest()
        == "0f2e75149567b27e2184ccbc89ba0e7da222a2a9cb6aa20268a6b757062492dc"
    )
    need(
        hashlib.sha256(raw["broker_ops_policy.py"]).hexdigest()
        == "30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522"
    )
    modules = {}
    for name in sorted(names):
        if not name.endswith(".py") or name == "verify_history_ops_offline.py":
            continue
        compile(raw[name], "<pinned-public-source>", "exec")
        spec = importlib.util.spec_from_file_location("public_" + name[:-3], directory / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    armed = [False]

    def guard(event, _args):
        if (
            event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
            or event == "os.system"
        ):
            raise ValueError("offline_forbidden_effect")
        if armed[0] and event in {"open", "sqlite3.connect", "os.listdir", "os.scandir"}:
            raise ValueError("default_plan_io")

    sys.addaudithook(guard)
    for name in (
        "history_ops_entry.py",
        "r14_ops_entry.py",
        "history_audit_reader.py",
        "install_history_ops.py",
    ):
        previous, output = sys.argv, io.StringIO()
        try:
            sys.argv = ["PUBLIC"]
            armed[0] = True
            with redirect_stdout(output):
                need(modules[name].main() == 0)
        finally:
            armed[0] = False
            sys.argv = previous
        need(type(json.loads(output.getvalue())) is dict)
    projection, protocol = (
        modules["ops_history_projection.py"],
        modules["history_audit_protocol.py"],
    )
    with tempfile.TemporaryDirectory(prefix="history-ops-public-") as work:
        db = Path(work) / "ledger.sqlite3"
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE gateway_tasks(request_key TEXT PRIMARY KEY,state TEXT,provider TEXT,model TEXT,
                    reservation_id TEXT,dispatched_at TEXT,http_status INTEGER,reported_input_tokens INTEGER,reported_output_tokens INTEGER);
                CREATE TABLE gateway_attempts(request_key TEXT,attempt_no INTEGER,reservation_id TEXT,provider TEXT,
                    state TEXT,dispatched_at TEXT,http_status INTEGER,reported_input_tokens INTEGER,reported_output_tokens INTEGER);
                CREATE TABLE reservations(id TEXT PRIMARY KEY,target_id TEXT,state TEXT,target_snapshot TEXT,shared_scope TEXT,dispatched_at TEXT);
                CREATE TABLE charges(reservation_id TEXT,bucket TEXT,metric TEXT,amount INTEGER);
                CREATE TABLE execution_completion(reservation_id TEXT,finished_at TEXT);
                CREATE TABLE queue_jobs(request_key TEXT,state TEXT,payload BLOB);
                CREATE TABLE queue_attempts(request_key TEXT);
            """)
            con.execute(
                "INSERT INTO gateway_tasks VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    projection.GROQ_KEY,
                    "completed",
                    "groq",
                    "openai/gpt-oss-20b",
                    "g",
                    "PUBLIC_UTC",
                    200,
                    76,
                    25,
                ),
            )
            con.execute(
                "INSERT INTO gateway_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                (projection.GROQ_KEY, 0, "g", "groq", "completed", "PUBLIC_UTC", 200, 76, 25),
            )
            con.execute(
                "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                (
                    "g",
                    "PUBLIC_TARGET",
                    "completed",
                    '{"provider":"groq"}',
                    "groq:PUBLIC",
                    "PUBLIC_UTC",
                ),
            )
            con.execute("INSERT INTO execution_completion VALUES(?,?)", ("g", "PUBLIC_UTC"))
            for bucket, metric, amount in (
                ("rpm", "requests", 1),
                ("rpd", "requests", 1),
                ("tpm", "input_tokens", 76),
            ):
                con.execute(
                    "INSERT INTO charges VALUES(?,?,?,?)",
                    ("g", "groq:asus-dev-key:" + bucket, metric, amount),
                )
        before = db.read_bytes()
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            report = projection.classify_ledger(con, set())
            try:
                con.execute("DELETE FROM gateway_tasks")
            except sqlite3.OperationalError:
                pass
            else:
                raise ValueError("readonly_not_enforced")
        need(
            report["provider_usage"]["groq"]["reported_input_tokens_sum"] == 76
            and report["provider_usage"]["groq"]["reported_output_tokens_sum"] == 25
            and db.read_bytes() == before
        )
        for code in (
            "audit_schema_missing_tables",
            "audit_schema_missing_columns",
            "audit_queue_active",
        ):
            need(
                protocol.validate(protocol.blocked(code, "ledger", "a" * 32), "a" * 32)["code"]
                == code
            )
    worker = raw["history-api-quota-broker-ops@.service"].decode()
    audit = raw["api-quota-broker-history-audit@.service"].decode()
    need("InaccessiblePaths=/etc/api-quota-broker/credentials /var/lib/api-quota-broker" in worker)
    need(
        "RuntimeMaxSec=190" in worker
        and "TimeoutStartSec=90" in audit
        and "ProtectSystem=strict" in audit
        and "NoNewPrivileges=yes" in audit
        and "LoadCredential" not in audit
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    need(libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0)
    return {
        "status": "passed",
        "mode": "history_ops_offline_public_only",
        "source_files_verified": 10,
        "python": ".".join(map(str, sys.version_info[:3])),
        "default_plan_io": 0,
        "readonly_sqlite_fixture": True,
        "usage_76_25_projected": True,
        "kernel_core_zero": True,
        "kernel_dumpable_zero": True,
        "credential_reads": 0,
        "Doppler_GET": 0,
        "provider_calls": 0,
        "root_install_executed": False,
        "native_history_audit_executed": False,
        "native_systemd_sandbox_verified": False,
    }


if __name__ == "__main__":
    if sys.argv[1:] == []:
        print('{"mode":"history_ops_offline_verifier_plan","host_changes":0}')
    else:
        try:
            need(
                len(sys.argv) == 5
                and sys.argv[1] == "--directory"
                and sys.argv[3] == "--wrapper-sha256"
            )
            print(json.dumps(verify(sys.argv[2], sys.argv[4]), sort_keys=True))
        except BaseException:  # noqa: BLE001 - no raw errors/private paths.
            print(
                '{"status":"blocked","code":"history_ops_offline_unverified","automatic_retry":false}'
            )
            raise SystemExit(1) from None
