"""Fixed reviewed repair of the single terminal pre-switch maintenance failure.

The caller seals PAYLOAD before execution. No external argv, paths, commands,
or application code are accepted. Original receipts and staged bytes survive.
"""

import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import resource
import stat
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops/maintenance")
APP = Path("/opt/api-quota-broker")
SYSTEM = Path("/etc/systemd/system")
RID = "5d66c9118e144365b2c0331936ec0af7"
MANIFEST = "20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855"
OLD_MANIFEST = "d6865f3bff7bb7410b1814e8fbdbd2fd310c3d1bc4b4f7d542286c08a8d4a86a"
OLD_ENTRY = "03e4376dd5180e729f0ecc1acf00fc936ac3adde50ccad889e394d48cc42d461"
UNIT = "api-quota-broker-maintenance@.service"
BACKUP = STATE / "repair-setuid-r1.backup"
CLAIM = STATE / "repair-setuid-r1.claim.json"
RESULT = STATE / "repair-setuid-r1.result.json"
SERVICES = {
    "api-quota-broker.service": ("348393", "944479363357", "0"),
    "orderflow.service": ("240075", "690258498304", "0"),
    "ssh.service": ("172081", "515138274924", "0"),
}


def need(value):
    if not value:
        raise ValueError("repair_gate")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tree_fingerprint(path):
    """Hash public staged bytes/metadata without executing or altering them."""
    root = path.lstat()
    need(stat.S_ISDIR(root.st_mode) and root.st_uid == 0)
    paths = [path, *path.rglob("*")]
    need(len(paths) < 10000)
    result = []
    total = 0
    for item in sorted(paths):
        info = item.lstat()
        need(info.st_uid == 0)
        row = [str(item.relative_to(path)), info.st_mode, info.st_uid, info.st_gid]
        if stat.S_ISLNK(info.st_mode):
            # Symlink permissions do not define mutability; parent is root-owned.
            row += [os.readlink(item)]
        elif stat.S_ISREG(info.st_mode):
            need(not info.st_mode & 0o022)
            need(info.st_nlink == 1 and info.st_size <= 33554432)
            total += info.st_size
            need(total <= 167772160)
            row += [sha(item.read_bytes())]
        else:
            need(stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022)
        result.append(row)
    return sha(wire(result))


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


def repair(entry, old, files):
    need(set(files) == {"maintenance_ops.py", UNIT, "manifest.json"})
    entry.package()
    need(sha(entry.read_root(BASE / "manifest.json", mode=0o644)) == OLD_MANIFEST)
    original_manifest = json.loads(entry.read_root(BASE / "manifest.json", mode=0o644))
    new_manifest = json.loads(files["manifest.json"])
    expected_manifest = json.loads(wire(original_manifest))
    expected_manifest["files"]["maintenance_ops.py"] = sha(files["maintenance_ops.py"])
    expected_manifest["units"][UNIT] = sha(files[UNIT])
    need(new_manifest == expected_manifest)
    old_unit = entry.read_root(SYSTEM / UNIT, mode=0o644)
    expected_unit = old_unit.replace(
        b"Group=root\n", b"Group=root\nAmbientCapabilities=CAP_SETUID\n"
    )
    expected_unit += b"InaccessiblePaths=-/run/credentials -/run/api-quota-broker\n"
    need(files[UNIT] == expected_unit)
    req = {"operation": "deploy", "request_id": RID}
    protocol = old.module(entry, "maintenance_protocol.py")
    expected_result = old.wire(
        protocol.receipt(req, "blocked", "internal_error", legacy_clear=True, queue_jobs=0)
    )
    need(entry.strict_json(entry.read_root(STATE / (RID + ".request.json"), mode=0o600)) == req)
    need(entry.read_root(STATE / (RID + ".result.json"), mode=0o600) == expected_result)
    need(entry.read_root(STATE / (RID + ".started"), mode=0o600) == b"1\n")
    absent = [
        CLAIM,
        RESULT,
        BACKUP,
        STATE / "deployment.json",
        STATE / ("backup-" + RID),
        *(
            STATE / (RID + suffix)
            for suffix in (".repair-authorized.json", ".repair-started", ".repair-result.json")
        ),
    ]
    need(not any(os.path.lexists(p) for p in absent))
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
    native = old.Native(entry, req)
    native.preflight()
    need(
        native.initial
        and native.jobs == 0
        and native.legacy_clear
        and native.manifest_sha == MANIFEST
    )
    native.upload()
    before = service_snapshot(entry)
    candidate = APP / ("releases/release-" + MANIFEST)
    archived = APP / ("releases/failed-stage-" + RID)
    need(not os.path.lexists(archived))
    need(sha(entry.read_root(candidate / "release-manifest.json", mode=0o644)) == MANIFEST)
    marker = entry.strict_json(
        entry.read_root(candidate / "maintenance-runtime-install.json", mode=0o644)
    )
    need(marker["manifest_sha256"] == MANIFEST and marker["root_candidate_execution"] is False)
    fingerprint = tree_fingerprint(candidate)
    targets = {
        BASE / "maintenance_ops.py": files["maintenance_ops.py"],
        SYSTEM / UNIT: files[UNIT],
        BASE / "manifest.json": files["manifest.json"],
    }
    originals = {p: entry.read_root(p, mode=0o644) for p in targets}
    record = {
        "state": "blocked",
        "stage": "backup",
        "same_request_id": RID,
        "provider_posts": 0,
        "dispatched": False,
        "rollback_verified": False,
        "original_receipt_preserved": True,
    }
    entry.write_exclusive(CLAIM, wire({"schema": 1, "request_id": RID, "dispatch_intent": False}))
    changed = []
    moved = False
    permit_written = False
    try:
        BACKUP.mkdir(mode=0o700)
        for i, (p, raw) in enumerate(originals.items()):
            entry.write_exclusive(BACKUP / str(i), raw)
        entry.write_exclusive(
            BACKUP / "index.json",
            wire(
                {
                    str(p): {"file": str(i), "sha256": sha(raw)}
                    for i, (p, raw) in enumerate(originals.items())
                }
            ),
        )
        entry.write_exclusive(
            BACKUP / "stage.json",
            wire(
                {"candidate": str(candidate), "archive": str(archived), "tree_sha256": fingerprint}
            ),
        )
        record["stage"] = "install"
        for p, raw in targets.items():
            changed.append(p)
            old.atomic(entry, p, raw, mode=0o644)
        entry.package()
        record["stage"] = "archive"
        candidate.rename(archived)
        moved = True
        old.sync(archived.parent)
        need(tree_fingerprint(archived) == fingerprint)
        entry.native(("/usr/bin/systemctl", "daemon-reload"))
        need(service_snapshot(entry) == before)
        record["stage"] = "authorize"
        permit = {
            "schema": 1,
            "request_id": RID,
            "original_result_sha256": sha(expected_result),
            "manifest_sha256": MANIFEST,
            "archive": "releases/failed-stage-" + RID,
        }
        entry.write_exclusive(STATE / (RID + ".repair-authorized.json"), wire(permit))
        permit_written = True
        # Intent is persisted before manager dispatch. An unknown start is never repeated.
        entry.write_exclusive(
            STATE / "repair-setuid-r1.dispatch-intent.json",
            wire({"request_id": RID, "unit": "api-quota-broker-maintenance@" + RID + ".service"}),
        )
        record.update(state="passed", stage="prepared")
    except BaseException:  # noqa: BLE001 - no native error strings escape
        if not record["dispatched"] and not permit_written:
            try:
                if moved:
                    need(
                        not os.path.lexists(candidate) and tree_fingerprint(archived) == fingerprint
                    )
                    archived.rename(candidate)
                    old.sync(candidate.parent)
                for p in reversed(changed):
                    old.atomic(entry, p, originals[p], mode=0o644)
                entry.native(("/usr/bin/systemctl", "daemon-reload"))
                entry.package()
                need(service_snapshot(entry) == before)
                record["rollback_verified"] = True
            except BaseException:  # noqa: BLE001 - stop on uncertain recovery
                record["stage"] = "rollback_unverified"
        elif record["dispatched"]:
            record["state"] = "unknown"
    return record


def dispatch_prepared(entry, record):
    # The parent operation lock MUST be released before the new unit takes it.
    if record["state"] == "passed" and record["stage"] == "prepared":
        record.update(state="unknown", stage="dispatch", dispatched=True)
        try:
            entry.native(
                (
                    "/usr/bin/systemctl",
                    "start",
                    "--no-block",
                    "api-quota-broker-maintenance@" + RID + ".service",
                )
            )
            record.update(state="passed", stage="dispatched")
        except BaseException:  # noqa: BLE001 - dispatch unknown is not retried
            record["state"] = "unknown"
    entry.write_exclusive(RESULT, wire(record))
    return record


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
        record = repair(entry, old, files)
    finally:
        os.close(fd)
    return dispatch_prepared(entry, record)
