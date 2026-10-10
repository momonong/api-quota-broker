import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import resource
import stat
from pathlib import Path


def need(value, *_codes):
    if not value:
        raise ValueError("repair_gate")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def service_snapshot(entry):
    result = {}
    for name, expected in SERVICES.items():
        s = entry.service_state(name)
        need(
            tuple(s[k] for k in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts"))
            == expected
        )
        result[name] = s
    return result


def main(files):
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    need(ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0)
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[
        1
    ].lstrip("/")
    need(
        (group / "memory.swap.max").read_text().strip() == "0"
        and (group / "memory.swap.current").read_text().strip() == "0"
    )
    entry_path = BASE / "ops_entry.py"
    meta = entry_path.lstat()
    need(
        meta.st_uid == 0
        and stat.S_ISREG(meta.st_mode)
        and stat.S_IMODE(meta.st_mode) == 0o644
        and meta.st_nlink == 1
    )
    need(sha(entry_path.read_bytes()) == OLD_ENTRY)
    spec = importlib.util.spec_from_file_location("repair_entry", entry_path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.prevent_process_dumps()
    entry.package()
    old = entry.load_public_module("maintenance_ops.py", "repair_old_maintenance")
    fd = os.open(STATE.parent / "operation.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        need(meta.st_uid == 0 and meta.st_nlink == 1 and stat.S_IMODE(meta.st_mode) == 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = install(entry, old, files)
    finally:
        os.close(fd)
    return record


"""Install only the sealed one-operation FD recovery maintenance profile."""
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops/maintenance")
APP = Path("/opt/api-quota-broker")
OLD_ENTRY = "03e4376dd5180e729f0ecc1acf00fc936ac3adde50ccad889e394d48cc42d461"
OLD_MANIFEST = "ebdf60c9f8bcc93d74d2647c86bac01e78ebffb10a43e073700e5f2486c6ab74"
BACKUP = STATE / "fd-recovery-r1.install-backup"
CLAIM = STATE / "fd-recovery-r1.install-claim.json"
RESULT = STATE / "fd-recovery-r1.install-result.json"
SERVICES = {
    "api-quota-broker.service": ("387484", "1049144733993", "0"),
    "orderflow.service": ("240075", "690258498304", "0"),
    "ssh.service": ("172081", "515138274924", "0"),
}

FD_RECOVERY_ID = "e1028cade84c4fc09fa0cbba85d19be0"
FD_RECOVERY_MANIFEST = "510b514833ce06ee491128a8c12d5ab2acb2666d0f9839ee79cdc82e0ec4dda8"
FD_RECOVERY_SOURCE = "968bb6f6a17733e8f07f3c621b6b04eb4118b140023e3f1c99d35c5a209759af"
FD_RECOVERY_WHEEL = "d240b48e84e2c9f6266556972dfb5b5ead470b3d72b14efe7db618b01e9ac685"
FD_RECOVERY_QUEUE = "asus-normal-v1-2026-10-08-r1-queued-auto-a1"
FD_RECOVERY_EXECUTION = "q-5a09fb653fcd4e27a3819c1c7abd104e"


def fd_recovery_gate(con):
    """One reviewed, untouched preparation; never a general quiescence bypass."""
    con.execute("PRAGMA query_only=ON")
    need(con.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    counts = {
        "gateway_tasks": 9,
        "gateway_attempts": 8,
        "reservations": 8,
        "charges": 26,
        "execution_completion": 8,
        "queue_jobs": 1,
        "queue_attempts": 1,
    }
    for table, count in counts.items():
        need(
            con.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == count,
            "queue_not_quiescent",
        )
    row = con.execute(
        "SELECT request_key,state,attempt_count,max_attempts,execution_key,run_started,"
        "wait_policy,payload IS NOT NULL,result IS NULL,expires_at,deadline,lease_until,execution_until "
        "FROM queue_jobs"
    ).fetchone()
    need(
        tuple(row[:9])
        == (FD_RECOVERY_QUEUE, "running", 1, 1, FD_RECOVERY_EXECUTION, 1, "reject", 1, 1),
        "queue_not_quiescent",
    )
    now = datetime.now(UTC)
    need(datetime.fromisoformat(row[9]) > now + timedelta(minutes=5), "queue_not_quiescent")
    need(
        row[10] is None or datetime.fromisoformat(row[10]) > now + timedelta(minutes=5),
        "queue_not_quiescent",
    )
    need(
        all(value and datetime.fromisoformat(value) < now for value in row[11:]),
        "queue_not_quiescent",
    )
    task = con.execute(
        "SELECT state,reservation_id,target_id,provider,model,dispatched_at,completed_at "
        "FROM gateway_tasks WHERE request_key=?",
        (FD_RECOVERY_EXECUTION,),
    ).fetchone()
    need(task == ("preparing", None, None, None, None, None, None), "queue_not_quiescent")
    need(
        con.execute("SELECT request_key,attempt_no,execution_key FROM queue_attempts").fetchall()
        == [(FD_RECOVERY_QUEUE, 1, FD_RECOVERY_EXECUTION)],
        "queue_not_quiescent",
    )
    need(
        con.execute(
            "SELECT count(*) FROM gateway_attempts WHERE request_key=?", (FD_RECOVERY_EXECUTION,)
        ).fetchone()[0]
        == 0,
        "queue_not_quiescent",
    )
    prefix = "gw:" + FD_RECOVERY_EXECUTION + ":"
    need(
        con.execute(
            "SELECT count(*) FROM reservations WHERE substr(request_key,1,?)=?",
            (len(prefix), prefix),
        ).fetchone()[0]
        == 0,
        "queue_not_quiescent",
    )
    need(
        con.execute("SELECT id,length(verifier)>0 FROM queue_settings").fetchall() == [(1, 1)],
        "queue_not_quiescent",
    )
    return 1


def install(entry, old, files):
    need(set(files) == {"maintenance_ops.py", "manifest.json"})
    entry.package()
    original_manifest = entry.read_root(BASE / "manifest.json", mode=0o644)
    need(sha(original_manifest) == OLD_MANIFEST)
    expected = json.loads(original_manifest)
    expected["files"]["maintenance_ops.py"] = sha(files["maintenance_ops.py"])
    need(json.loads(files["manifest.json"]) == expected)
    need(
        os.readlink(APP / "current")
        == "releases/release-20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855"
    )
    need(
        not any(
            os.path.lexists(p)
            for p in (CLAIM, RESULT, BACKUP, STATE / (FD_RECOVERY_ID + ".request.json"))
        )
    )
    active = entry.native(
        (
            "/usr/bin/systemctl",
            "list-units",
            "--no-legend",
            "--plain",
            "--state=active,activating,deactivating",
            "api-quota-broker-maintenance@*.service",
            "api-quota-broker-ops@*.service",
            "api-quota-broker-history-audit@*.service",
        )
    )
    need(not active.strip())
    before = service_snapshot(entry)
    originals = {BASE / name: entry.read_root(BASE / name, mode=0o644) for name in files}
    receipt_names = [
        "5d66c9118e144365b2c0331936ec0af7" + s
        for s in (
            ".request.json",
            ".started",
            ".result.json",
            ".repair-started",
            ".repair-result.json",
        )
    ] + ["deployment.json"]
    receipts = {n: entry.read_root(STATE / n, mode=0o600) for n in receipt_names}
    with closing(
        sqlite3.connect("file:/var/lib/api-quota-broker/ledger.sqlite3?mode=ro", uri=True)
    ) as con:
        fd_recovery_gate(con)
    entry.write_exclusive(
        CLAIM, wire({"request_id": FD_RECOVERY_ID, "operation": "install_fixed_recovery"})
    )
    result = {
        "state": "blocked",
        "stage": "backup",
        "request_id": FD_RECOVERY_ID,
        "service_changes": 0,
        "database_writes": 0,
        "provider_posts": 0,
        "rollback_verified": False,
    }
    changed = []
    try:
        BACKUP.mkdir(mode=0o700)
        for name, raw in receipts.items():
            entry.write_exclusive(BACKUP / name, raw)
        for path, raw in originals.items():
            entry.write_exclusive(BACKUP / path.name, raw)
        with closing(
            sqlite3.connect("file:/var/lib/api-quota-broker/ledger.sqlite3?mode=ro", uri=True)
        ) as con:
            fd_recovery_gate(con)
            with closing(sqlite3.connect(BACKUP / "ledger.sqlite3")) as copied:
                con.backup(copied)
                need(copied.execute("PRAGMA quick_check").fetchall() == [("ok",)])
        (BACKUP / "ledger.sqlite3").chmod(0o600)
        with (BACKUP / "ledger.sqlite3").open("rb") as stream:
            os.fsync(stream.fileno())
        old.sync(BACKUP)
        result["stage"] = "install"
        for name, raw in files.items():
            path = BASE / name
            changed.append(path)
            old.atomic(entry, path, raw, mode=0o644)
        entry.package()
        need(service_snapshot(entry) == before)
        need(all(entry.read_root(STATE / n, mode=0o600) == raw for n, raw in receipts.items()))
        result.update(state="passed", stage="installed")
    except BaseException:  # noqa: BLE001 - preserve bounded failure evidence
        try:
            for path in reversed(changed):
                need(
                    sha(entry.read_root(path, mode=0o644))
                    in {sha(originals[path]), sha(files[path.name])}
                )
                old.atomic(entry, path, originals[path], mode=0o644)
            entry.package()
            need(service_snapshot(entry) == before)
            result["rollback_verified"] = True
        except BaseException:  # noqa: BLE001 - preserve bounded failure evidence
            result.update(state="unknown", stage="rollback_unverified")
    entry.write_exclusive(RESULT, wire(result))
    return result
