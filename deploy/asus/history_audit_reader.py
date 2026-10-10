"""Credential-free fixed reader for one PID1-created private readonly sandbox."""

import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import resource
import signal
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops/history-audit")
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
LIMIT = 32768


class Stop(ValueError):
    pass


def need(ok, code="audit_readonly_guard"):
    if not ok:
        raise Stop(code)


def guard(request_id):
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    need(
        resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
        and libc.prctl(4, 0, 0, 0, 0) == 0
        and libc.prctl(3, 0, 0, 0, 0) == 0
        and libc.prctl(39, 0, 0, 0, 0) == 1
    )
    name = "api-quota-broker-history-audit@" + request_id + ".service"
    need(Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + name)
    group = Path("/sys/fs/cgroup/system.slice") / name
    need(
        (group / "memory.max").read_text().strip() == "402653184"
        and (group / "memory.swap.max").read_text().strip() == "0"
    )
    need(
        all(
            os.statvfs(path).f_flag & os.ST_RDONLY
            for path in (DB, Path("/var/backups/api-quota-broker"), Path("/etc/api-quota-broker"))
        )
    )


def output_guard(request_id):
    """Never overwrite an existing result, even after a manual unit replay."""
    info = os.fstat(1)
    need(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == info.st_gid == 0
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_size == 0,
        "audit_request_replayed",
    )
    need(
        os.readlink("/proc/self/fd/1") == str(STATE / (request_id + ".result.json")),
        "audit_result_untrusted",
    )


def modules():
    for directory in reversed((BASE, *BASE.parents)):
        value = directory.lstat()
        need(
            stat.S_ISDIR(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022,
            "audit_package_invalid",
        )

    def read(name):
        fd = os.open(BASE / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == 0
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o644
                and info.st_size <= 131072,
                "audit_package_invalid",
            )
            raw = stream.read(131073)
            after = os.fstat(stream.fileno())
            need(
                len(raw) == info.st_size
                and (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
                "audit_package_invalid",
            )
            return raw

    manifest = json.loads(read("manifest.json"))
    need(
        set(manifest) == {"schema", "files", "units"} and manifest["schema"] == 2,
        "audit_package_invalid",
    )
    need(
        set(manifest["files"])
        == {
            "ops_entry.py",
            "broker_ops_policy.py",
            "history_audit_protocol.py",
            "history_audit_reader.py",
            "ops_history_projection.py",
        },
        "audit_package_invalid",
    )
    need(
        hashlib.sha256(read("ops_entry.py")).hexdigest() == manifest["files"]["ops_entry.py"],
        "audit_package_invalid",
    )
    spec = importlib.util.spec_from_file_location("aqb_history_root_entry", BASE / "ops_entry.py")
    need(spec is not None and spec.loader is not None, "audit_package_invalid")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.package()
    protocol = entry.load_public_module("history_audit_protocol.py", "aqb_history_reader_protocol")
    projection = entry.load_public_module(
        "ops_history_projection.py", "aqb_history_reader_projection"
    )
    return entry, protocol, projection


def perform(request_id, *, entry, protocol, projection, scope=guard, state=STATE, owner=0):
    check = "sandbox"
    lockfd = None

    def checkpoint(value):
        nonlocal check
        check = value

    try:
        scope(request_id)
        check = "package"
        entry.package()
        entry.root_dir(state, mode=0o700)
        lockfd = os.open(state / "reader.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        meta = os.fstat(lockfd)
        need(
            stat.S_ISREG(meta.st_mode)
            and meta.st_uid == owner
            and meta.st_nlink == 1
            and stat.S_IMODE(meta.st_mode) == 0o600
        )
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return protocol.blocked("audit_busy", "sandbox", request_id)
        claim = entry.strict_json(
            entry.read_root(state / (request_id + ".claim.json"), mode=0o600, limit=256)
        )
        need(
            claim
            == {
                "schema": 1,
                "operation": "history_audit",
                "request_id": request_id,
                "dispatch_intent": True,
                "credential_auth_verified": True,
            },
            "audit_request_replayed",
        )
        try:
            entry.write_exclusive(
                state / (request_id + ".reader.json"),
                json.dumps(
                    {"schema": 1, "private_read_intent": True, "request_id": request_id}
                ).encode()
                + b"\n",
            )
        except FileExistsError:
            return protocol.blocked("audit_request_replayed", "package", request_id)
        check = "ops_policy"
        policy = entry.runtime_policy()
        need(
            policy["enabled"] is False
            and policy["expires_at"] == "2026-11-05T04:33:31+00:00"
            and datetime.now(UTC) < datetime.fromisoformat(policy["expires_at"]),
            "audit_ops_pin_drift",
        )
        check = "ledger"
        report, _private_config = projection.private_preflight(check=checkpoint)
        return protocol.passed(report, request_id)
    except TimeoutError:
        return protocol.blocked("audit_sql_timeout", check, request_id)
    except FileNotFoundError:
        return protocol.blocked("audit_required_input_missing", check, request_id)
    except PermissionError:
        return protocol.blocked("audit_required_input_inaccessible", check, request_id)
    except sqlite3.OperationalError:
        return protocol.blocked("audit_sqlite_unavailable", check, request_id)
    except sqlite3.DatabaseError:
        return protocol.blocked("audit_ledger_integrity", check, request_id)
    except BaseException as error:  # noqa: BLE001 - project codes, never error text.
        code = (
            error.args[0]
            if type(error) in (Stop, projection.Blocked, entry.Denied) and len(error.args) == 1
            else None
        )
        code = protocol.SOURCE_CODES.get(code, code)
        if code not in protocol.CODES:
            code = {
                "config": "audit_config_invalid",
                "old_intents": "audit_old_intent_invalid",
                "ledger": "audit_ledger_inconsistent",
                "bootstrap": "audit_package_provenance_mismatch",
                "ops_policy": "audit_ops_pin_drift",
                "release": "audit_package_invalid",
                "package": "audit_package_invalid",
                "sandbox": "audit_readonly_guard",
            }.get(check, "audit_internal_error")
        return protocol.blocked(code, check, request_id)
    finally:
        if lockfd is not None:
            os.close(lockfd)


def main():
    if sys.argv[1:] == []:
        print('{"mode":"fixed_history_reader_proposal","private_reads":0,"credential_reads":0}')
        return 0
    # Invalid nonce or output destination produces no bytes at all. In
    # particular, a repeated systemctl start cannot overwrite retained results.
    output_allowed = False
    check = "sandbox"
    try:
        need(
            len(sys.argv) == 2 and re.fullmatch(r"[a-f0-9]{32}", sys.argv[1]),
            "audit_result_untrusted",
        )
        request_id = sys.argv[1]
        output_guard(request_id)
        output_allowed = True
        guard(request_id)
        check = "package"
        entry, protocol, projection = modules()
    except BaseException:  # noqa: BLE001 - do not touch unverified stdout.
        if output_allowed:
            value = {
                "status": "blocked",
                "operation": "history_audit",
                "request_id": request_id,
                "service": "api-quota-broker.service",
                "automatic_retry": False,
                "code": "audit_package_invalid" if check == "package" else "audit_readonly_guard",
                "check": check,
                "private_values_exposed": False,
                "provider_posts": 0,
                "db_write": 0,
            }
            os.write(1, json.dumps(value, separators=(",", ":")).encode() + b"\n")
        return 1
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(75)
    try:
        result = perform(request_id, entry=entry, protocol=protocol, projection=projection)
        raw = (
            json.dumps(protocol.validate(result, request_id), separators=(",", ":")).encode()
            + b"\n"
        )
        need(len(raw) <= LIMIT, "audit_result_untrusted")
        os.write(1, raw)
        return 0 if result["status"] == "passed" else 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
