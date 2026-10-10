"""One reviewed human-root bootstrap. Installs capabilities, never deploys Broker."""

import fcntl
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
SYSTEM = Path("/etc/systemd/system")
CLIENT = Path("/usr/local/libexec/api-quota-broker-ops-client")
CLAIM = STATE / "maintenance-bootstrap-r1.claim.json"
RESULT = STATE / "maintenance-bootstrap-r1.result.json"
BACKUP = STATE / "maintenance-bootstrap-r1.backup"
EXPECTED_MANIFEST = "abde6f2e9afcc3aeedcf26484500e063737ac2b13aaff0ecac327bb793c1ba30"
EXPECTED_POLICY = "e62d5f6f97dc18ce48328b822629e16a536218a60e309ee54b65ddd344516a4a"
GATE = "identity"


def need(ok):
    if not ok:
        raise ValueError("bootstrap_gate")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def service_snapshot(entry, name):
    # Old r15 service_state intentionally excludes SSH. Read these three fixed
    # units directly without widening the old helper's mutation permissions.
    need(name in ("api-quota-broker.service", "orderflow.service", "ssh.service"))
    raw = entry.native(("/usr/bin/systemctl", "show", name, "--property=" + entry.PROPS))
    state = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
    need(set(state) == set(entry.PROPS.split(",")))
    need(state["ActiveState"] == "active" and state["SubState"] == "running")
    need(
        all(state[k].isdigit() for k in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic"))
    )
    need(int(state["MainPID"]) > 0)
    return state


def sources():
    global GATE
    GATE = "packet"
    root = Path(__file__).parent
    meta = root.lstat()
    need(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o700)

    def read(name):
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            need(
                s.st_uid == 0
                and s.st_nlink == 1
                and stat.S_ISREG(s.st_mode)
                and stat.S_IMODE(s.st_mode) == 0o600
                and s.st_size <= 2097152
            )
            raw = f.read(2097153)
            need(len(raw) == s.st_size)
            return raw

    seal = json.loads(read("seal.json"))
    need({p.name for p in root.iterdir()} == set(seal) | {"seal.json"})
    files = {n: read(n) for n in seal}
    need(all(sha(files[n]) == s for n, s in seal.items()))
    return files


def install(files):
    global GATE
    GATE = "legacy_entry"
    # Import only the already installed r15 whose full entry hash is independently pinned.
    raw = (BASE / "ops_entry.py").read_bytes()
    meta = (BASE / "ops_entry.py").lstat()
    need(
        meta.st_uid == 0
        and stat.S_ISREG(meta.st_mode)
        and stat.S_IMODE(meta.st_mode) == 0o644
        and meta.st_nlink == 1
        and sha(raw) == "ab54fbba16232c173e7838510f1f7c387dcf2e6d40bb20eea01d377f31bd2a88"
    )
    spec = importlib.util.spec_from_file_location("aqb_bootstrap_old", BASE / "ops_entry.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    old.prevent_process_dumps()
    GATE = "legacy_package"
    old.package()
    GATE = "legacy_policy"
    policy = old.runtime_policy()
    GATE = "legacy_pins"
    old.broker_pins(policy["config_sha256"])
    need(sha(old.read_root(BASE / "manifest.json")) == EXPECTED_MANIFEST)
    need(sha(old.read_root(Path("/etc/api-quota-broker-ops/policy.json"))) == EXPECTED_POLICY)
    GATE = "prior_receipts"
    old.root_dir(STATE, mode=0o700)
    need(not any(os.path.lexists(p) for p in (CLAIM, RESULT, BACKUP, STATE / "maintenance")))
    # No new credentials, account, sudoers or SSH configuration is installed.
    untouched = [
        Path("/etc/sudoers.d/api-quota-broker-ops"),
        Path("/etc/api-quota-broker-ops/policy.json"),
        Path("/usr/local/libexec/api-quota-broker-control"),
        SYSTEM / "api-quota-broker-ops.socket",
    ]
    GATE = "untouched_files"
    pins = {str(p): sha(old.read_root(p)) for p in untouched}
    GATE = "service_snapshot"
    services = {
        n: service_snapshot(old, n)
        for n in ("api-quota-broker.service", "orderflow.service", "ssh.service")
    }
    GATE = "operation_lock"
    fd = os.open(STATE / "operation.lock", os.O_RDWR | os.O_NOFOLLOW)
    record = {
        "status": "blocked",
        "stage": "preflight",
        "Broker_restarts": 0,
        "provider_posts": 0,
        "credentials_read": 0,
        "account_changes": 0,
        "sudoers_changes": 0,
        "ssh_changes": 0,
        "automatic_retry": False,
        "rollback_verified": False,
    }
    installed = {}
    originals = {}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active = old.native(
            (
                "/usr/bin/systemctl",
                "list-units",
                "--no-legend",
                "--plain",
                "--state=active,activating",
                "api-quota-broker-ops@*.service",
                "api-quota-broker-history-audit@*.service",
            )
        )
        need(not active.strip())
        manifest = json.loads(files["manifest.json"])
        mapping = {BASE / n: (files[n], 0o644) for n in manifest["files"]}
        mapping.update({SYSTEM / n: (files[n], 0o644) for n in manifest["units"]})
        mapping[CLIENT] = (files["api-quota-broker-ops-client"], 0o755)
        mapping[Path("/etc/api-quota-broker-ops/active-release.json")] = (
            files["active-release.json"],
            0o644,
        )
        mapping[BASE / "manifest.json"] = (files["manifest.json"], 0o644)  # commit last
        previous = json.loads(old.read_root(BASE / "manifest.json"))
        existing = (
            {BASE / n for n in previous["files"]}
            | {SYSTEM / n for n in previous["units"]}
            | {CLIENT, BASE / "manifest.json"}
        )
        for p in mapping:
            if p in existing:
                originals[p] = old.read_root(p)
            else:
                need(not os.path.lexists(p))
        old.write_exclusive(CLAIM, json.dumps({"schema": 1, "dispatch_intent": True}).encode())
        BACKUP.mkdir(mode=0o700)
        for i, (p, raw) in enumerate(originals.items()):
            old.write_exclusive(BACKUP / str(i), raw)
        old.write_exclusive(
            BACKUP / "index.json",
            json.dumps(
                {
                    str(p): {"file": str(i), "sha256": sha(raw)}
                    for i, (p, raw) in enumerate(originals.items())
                }
            ).encode(),
        )
        (STATE / "maintenance").mkdir(mode=0o700)
        record["stage"] = "install"
        for p, (raw, mode) in mapping.items():
            if p in originals:
                need(sha(old.read_root(p)) == sha(originals[p]))
                temp = p.parent / (".maintenance-bootstrap-" + os.urandom(16).hex())
                old.write_exclusive(temp, raw, mode=mode)
                os.replace(temp, p)
            else:
                old.write_exclusive(p, raw, mode=mode)
            installed[p] = sha(raw)
            directory = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        record["stage"] = "verify"
        old.native(
            (
                "/usr/bin/systemd-analyze",
                "verify",
                str(SYSTEM / "api-quota-broker-maintenance@.service"),
                str(SYSTEM / "api-quota-broker-ops@.service"),
            ),
            timeout=30,
        )
        old.native(("/usr/bin/systemctl", "daemon-reload"), timeout=30)
        # Newly installed entry is reviewed bootstrap code, never application payload.
        spec = importlib.util.spec_from_file_location("aqb_bootstrap_new", BASE / "ops_entry.py")
        new = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(new)
        new.package()
        new.runtime_policy()
        new.broker_pins(policy["config_sha256"])
        need(pins == {str(p): sha(old.read_root(p)) for p in untouched})
        need(services == {n: service_snapshot(old, n) for n in services})
        need(
            old.native(("/usr/bin/systemctl", "is-active", "api-quota-broker-ops.socket")).strip()
            == b"active"
        )
        record.update(
            status="passed",
            stage="complete",
            maintenance_installed=True,
            normal_v1_deployed=False,
            native_doppler_auth_verified=False,
        )
    except BaseException:  # noqa: BLE001 - immutable safe receipt only
        if installed:
            try:
                for p in reversed(installed):
                    need(sha(old.read_root(p)) == installed[p])
                    if p in originals:
                        temp = p.parent / (".maintenance-restore-" + os.urandom(16).hex())
                        mode = 0o755 if p == CLIENT else 0o644
                        old.write_exclusive(temp, originals[p], mode=mode)
                        os.replace(temp, p)
                    else:
                        p.unlink()
                # Keep bootstrap claims/backups; only remove our empty work directory.
                (STATE / "maintenance").rmdir()
                old.native(("/usr/bin/systemctl", "daemon-reload"), timeout=30)
                old.package()
                old.broker_pins(policy["config_sha256"])
                record["rollback_verified"] = True
            except BaseException:  # noqa: BLE001
                record["rollback_verified"] = False
    finally:
        os.close(fd)
    if CLAIM.exists():
        old.write_exclusive(RESULT, json.dumps(record, sort_keys=True).encode())
    return record


if __name__ == "__main__":
    try:
        need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server")
        outcome = install(sources())
    except BaseException:  # noqa: BLE001
        outcome = {
            "status": "blocked",
            "stage": "bootstrap_gate",
            "gate": GATE,
            "automatic_retry": False,
        }
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit(0 if outcome["status"] == "passed" else 1)
