"""Once reviewed history capability upgrade. Default has zero host effects.

Only ops source/manifest/two units and root-owned audit records change. Never
opens encrypted credentials, gets a Token, changes accounts/policy/SSH/sudoers,
dispatches a provider, starts an audit, or restarts the Broker.
"""

import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import resource
import signal
import stat
import sys
import time
import uuid
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
CONFIG = Path("/etc/api-quota-broker-ops")
SYSTEM = Path("/etc/systemd/system")
AUDIT = STATE / "history-audit"
CLAIM = STATE / "history-upgrade-r1.claim.json"
RESULT = STATE / "history-upgrade-r1.result.json"
OLD_ENTRY_SHA = "0f2e75149567b27e2184ccbc89ba0e7da222a2a9cb6aa20268a6b757062492dc"
OLD_AUTH_SHA = "30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522"
OLD_WORKER_SHA = "b0e592cfc7874948af3a655138f9bff56f7c0dd00d5f4ddaee3c2b9ce5f71554"
MAPPING = {
    "history_ops_entry.py": BASE / "ops_entry.py",
    "history_audit_reader.py": BASE / "history_audit_reader.py",
    "history_audit_protocol.py": BASE / "history_audit_protocol.py",
    "ops_history_projection.py": BASE / "ops_history_projection.py",
    "api-quota-broker-history-audit@.service": SYSTEM / "api-quota-broker-history-audit@.service",
    "history-api-quota-broker-ops@.service": SYSTEM / "api-quota-broker-ops@.service",
}
NAMES = {
    *MAPPING,
    "broker_ops_policy.py",
    "r14_ops_entry.py",
    "install_history_ops.py",
    "verify_history_ops_offline.py",
}


class Blocked(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Blocked(code)


def present(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def load_sources():
    root = Path(__file__).absolute().parent
    meta = root.lstat()
    need(
        root.parent == Path("/var/tmp")
        and re.fullmatch(r"aqb-history-ops-upgrade-[a-f0-9]{32}", root.name)
        and stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == 0
        and stat.S_IMODE(meta.st_mode) == 0o700,
        "upgrade_source_untrusted",
    )
    raw = {}
    for name in NAMES | {"seal.json"}:
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 131072,
                "upgrade_source_untrusted",
            )
            value = stream.read(131073)
            after = os.fstat(stream.fileno())
            need(
                len(value) == info.st_size
                and (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
                "upgrade_source_changed",
            )
            raw[name] = value
    seal = json.loads(raw.pop("seal.json"))
    need(
        set(seal) == {"schema", "files"} and seal["schema"] == 1 and set(seal["files"]) == NAMES,
        "upgrade_seal_untrusted",
    )
    need(
        all(hashlib.sha256(raw[name]).hexdigest() == sha for name, sha in seal["files"].items()),
        "upgrade_source_changed",
    )
    need(
        hashlib.sha256(raw["r14_ops_entry.py"]).hexdigest() == OLD_ENTRY_SHA
        and hashlib.sha256(raw["broker_ops_policy.py"]).hexdigest() == OLD_AUTH_SHA,
        "upgrade_basis_changed",
    )
    spec = importlib.util.spec_from_file_location(
        "aqb_upgrade_r14_basis", root / "r14_ops_entry.py"
    )
    need(spec is not None and spec.loader is not None, "upgrade_source_untrusted")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return raw, entry


def upgrade(ops):
    """Shared real transaction ordering; fixtures inject only OS adapters."""
    changed = False
    try:
        ops.preflight()
        ops.claim_and_backup()
        changed = True
        ops.prepare_additions()
        changed = True  # The manifest remains old until every candidate file is ready.
        ops.publish()
        ops.validate()
        ops.verify_preservation()
        return {
            "status": "passed",
            "mode": "history_ops_upgrade",
            "installed": True,
            "policy_cipher_account_expiry_preserved": True,
            "inspect_entry_contract_preserved": True,
            "ops_socket_state_preserved": True,
            "native_inspect_executed": False,
            "restart_enabled": False,
            "Broker_restart": False,
            "SSH_reload": False,
            "Doppler_GET": 0,
            "credential_reads": 0,
            "provider_calls": 0,
            "native_history_audit_executed": False,
            "automatic_retry": False,
        }
    except BaseException as error:  # noqa: BLE001 - fixed codes, no raw exception.
        code = (
            error.args[0]
            if type(error) is Blocked and len(error.args) == 1
            else "upgrade_unverified"
        )
        allowed = {
            "upgrade_busy",
            "upgrade_collision",
            "upgrade_pin_drift",
            "upgrade_expiry_drift",
            "upgrade_budget",
            "upgrade_unverified",
            "upgrade_validation",
            "upgrade_preservation",
        }
        code = code if code in allowed else "upgrade_unverified"
        restored = None
        if ops.claimed:
            try:
                ops.rollback()
                restored = True
            except BaseException:  # noqa: BLE001 - preserve claims/backups; no retry.
                restored = False
        return {
            "status": "blocked",
            "mode": "history_ops_upgrade",
            "code": code,
            "public_files_may_have_changed": changed,
            "rollback_verified": restored,
            "automatic_retry": False,
            "Broker_restart": False,
            "SSH_reload": False,
            "Doppler_GET": 0,
            "credential_reads": 0,
            "provider_calls": 0,
        }
    finally:
        ops.close()


class Native:
    def __init__(self, source, entry):
        self.source, self.e = source, entry
        self.claimed, self.lockfd = False, None
        self.originals, self.additions, self.replaced, self.staged = {}, {}, {}, {}
        self.audit_created = False
        self.end = time.monotonic() + 360

    def budget(self, minimum=60):
        need(self.end - time.monotonic() >= minimum, "upgrade_budget")

    def state_snapshot(self):
        result = {}
        for path in STATE.iterdir():
            if (
                path in (CLAIM, RESULT)
                or (getattr(self, "backup", None) is not None and path == self.backup)
                or path == AUDIT
            ):
                continue
            need(
                path.name == "operation.lock"
                or path.name
                in {"retained-inspect.claim.json", "agent-inspect-activation.claim.json"}
                or re.fullmatch(r"[a-f0-9]{32}\.(claim|result)\.json", path.name),
                "upgrade_pin_drift",
            )
            raw = self.e.read_root(path, mode=0o600, limit=32768)
            result[path.name] = hashlib.sha256(raw).hexdigest()
            need(len(result) <= 4096, "upgrade_pin_drift")
        return result

    def account_expiry(self):
        raw = self.e.native(("/usr/bin/chage", "--list", "--iso8601", "broker-deploy"), limit=4096)
        dates = [
            line.split(":", 1)[1].strip()
            for line in raw.decode().splitlines()
            if line.startswith("Account expires")
        ]
        need(dates == ["2026-11-05"], "upgrade_expiry_drift")

    def invariants(self):
        policy = self.e.read_root(CONFIG / "policy.json", mode=0o644)
        d = self.e.strict_json(policy)
        need(
            d["enabled"] is False
            and d["expires_at"] == "2026-11-05T04:33:31+00:00"
            and d["ops_uid"] == 994
            and d["ops_gid"] == 981
            and d["peer_uid"] == 1000,
            "upgrade_expiry_drift",
        )
        self.e.broker_pins(d["config_sha256"])
        cipher = (
            CONFIG / "ops_doppler.cred"
        ).lstat()  # Metadata only, never open/copy/hash values.
        need(
            stat.S_ISREG(cipher.st_mode)
            and cipher.st_uid == cipher.st_gid == 0
            and stat.S_IMODE(cipher.st_mode) == 0o600
            and cipher.st_nlink == 1
            and 0 < cipher.st_size <= 16384,
            "upgrade_pin_drift",
        )
        self.account_expiry()
        services = {}
        for name in ("api-quota-broker.service", "orderflow.service", "ssh.service"):
            raw = self.e.native(
                (
                    "/usr/bin/systemctl",
                    "show",
                    name,
                    "--property=ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,NRestarts",
                )
            )
            row = dict(line.split("=", 1) for line in raw.decode().splitlines())
            need(
                set(row)
                == {
                    "ActiveState",
                    "SubState",
                    "MainPID",
                    "ExecMainStartTimestampMonotonic",
                    "NRestarts",
                }
                and row["ActiveState"] == "active"
                and row["SubState"] == "running"
                and all(
                    row[k].isascii() and row[k].isdigit() and len(row[k]) <= 24
                    for k in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts")
                ),
                "upgrade_preservation",
            )
            services[name] = row
        fixed = {}
        for path, mode in (
            (Path("/usr/local/libexec/api-quota-broker-control"), 0o755),
            (Path("/usr/local/libexec/api-quota-broker-ops-client"), 0o755),
            (Path("/etc/sudoers.d/api-quota-broker-ops"), 0o440),
            (SYSTEM / "api-quota-broker-ops.socket", 0o644),
        ):
            fixed[str(path)] = hashlib.sha256(self.e.read_root(path, mode=mode)).hexdigest()
        socket = self.e.native(
            (
                "/usr/bin/systemctl",
                "show",
                "api-quota-broker-ops.socket",
                "--property=ActiveState,SubState,UnitFileState",
            )
        )
        need(
            dict(line.split("=", 1) for line in socket.decode().splitlines())
            == {"ActiveState": "active", "SubState": "listening", "UnitFileState": "enabled"},
            "upgrade_preservation",
        )
        return {
            "policy": hashlib.sha256(policy).hexdigest(),
            "cipher": (
                cipher.st_dev,
                cipher.st_ino,
                cipher.st_uid,
                cipher.st_gid,
                cipher.st_mode,
                cipher.st_size,
                cipher.st_mtime_ns,
                cipher.st_ctime_ns,
            ),
            "services": services,
            "fixed": fixed,
            "state": self.state_snapshot(),
        }

    def no_workers(self):
        raw = self.e.native(
            (
                "/usr/bin/systemctl",
                "list-units",
                "--no-legend",
                "--plain",
                "--state=active,activating,deactivating",
                "api-quota-broker-ops@*.service",
                "api-quota-broker-history-audit@*.service",
                "api-quota-broker-history-audit.service",
            )
        )
        need(not raw.strip(), "upgrade_busy")

    def preflight(self):
        self.budget(300)
        self.e.package()  # Precisely the installed r14 two-file contract.
        self.e.runtime_policy()
        self.e.read_root(BASE / "ops_entry.py", sha=OLD_ENTRY_SHA, mode=0o644)
        self.e.read_root(BASE / "broker_ops_policy.py", sha=OLD_AUTH_SHA, mode=0o644)
        self.e.read_root(SYSTEM / "api-quota-broker-ops@.service", sha=OLD_WORKER_SHA, mode=0o644)
        self.e.root_dir(STATE, mode=0o700)
        need(not present(CLAIM) and not present(RESULT) and not present(AUDIT), "upgrade_collision")
        self.lockfd = os.open(STATE / "operation.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(self.lockfd)
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "upgrade_pin_drift",
        )
        try:
            fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Blocked("upgrade_busy") from error
        self.no_workers()
        for parent in (
            SYSTEM,
            Path("/usr/lib/systemd/system"),
            Path("/usr/local/lib/systemd/system"),
        ):
            need(
                not present(parent / "api-quota-broker-history-audit@.service"), "upgrade_collision"
            )
            need(
                not present(parent / "api-quota-broker-history-audit@.service.d"),
                "upgrade_collision",
            )
        self.before = self.invariants()
        for path in MAPPING.values():
            if path in (BASE / "ops_entry.py", SYSTEM / "api-quota-broker-ops@.service"):
                self.originals[path] = self.e.read_root(path, mode=0o644)
            else:
                need(not present(path), "upgrade_collision")
        self.originals[BASE / "manifest.json"] = self.e.read_root(
            BASE / "manifest.json", mode=0o644
        )

    def claim_and_backup(self):
        self.budget(260)
        self.e.write_exclusive(
            CLAIM,
            b'{"schema":1,"upgrade":"history_audit","dispatch_intent":true,"automatic_retry":false}\n',
        )
        self.claimed = True
        self.backup = STATE / ("history-upgrade-" + uuid.uuid4().hex)
        self.backup.mkdir(mode=0o700)
        self.backup.chmod(0o700)
        for path, raw in self.originals.items():
            self.e.write_exclusive(self.backup / path.name, raw)

    def prepare_additions(self):
        self.budget(245)
        AUDIT.mkdir(mode=0o700)
        self.audit_created = True
        AUDIT.chmod(0o700)
        self.e.write_exclusive(AUDIT / "reader.lock", b"")
        for name, path in MAPPING.items():
            if path not in self.originals:
                self.additions[path] = hashlib.sha256(self.source[name]).hexdigest()
                self.e.write_exclusive(path, self.source[name], mode=0o644)

    def replace(self, path, raw, expected):
        self.e.read_root(path, sha=expected, mode=0o644)
        temporary = path.with_name(path.name + ".history-upgrade-stage")
        need(not present(temporary), "upgrade_collision")
        self.e.write_exclusive(temporary, raw, mode=0o644)
        self.staged[temporary] = hashlib.sha256(raw).hexdigest()
        self.e.read_root(path, sha=expected, mode=0o644)
        os.replace(temporary, path)
        self.staged.pop(temporary, None)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish(self):
        self.budget(230)
        self.no_workers()
        for name, path in MAPPING.items():
            if path in self.originals:
                new = self.source[name]
                self.replaced[path] = hashlib.sha256(new).hexdigest()
                self.replace(path, new, hashlib.sha256(self.originals[path]).hexdigest())
        files = {
            "ops_entry.py": self.source["history_ops_entry.py"],
            "broker_ops_policy.py": self.source["broker_ops_policy.py"],
            **{
                name: self.source[name]
                for name in (
                    "history_audit_reader.py",
                    "history_audit_protocol.py",
                    "ops_history_projection.py",
                )
            },
        }
        manifest = {
            "schema": 2,
            "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
            "units": {
                "api-quota-broker-ops@.service": hashlib.sha256(
                    self.source["history-api-quota-broker-ops@.service"]
                ).hexdigest(),
                "api-quota-broker-history-audit@.service": hashlib.sha256(
                    self.source["api-quota-broker-history-audit@.service"]
                ).hexdigest(),
            },
        }
        raw = json.dumps(manifest, sort_keys=True).encode() + b"\n"
        path = BASE / "manifest.json"
        self.replaced[path] = hashlib.sha256(raw).hexdigest()
        self.replace(path, raw, hashlib.sha256(self.originals[path]).hexdigest())

    def validate(self):
        self.budget(210)
        self.e.native(
            (
                "/usr/bin/systemd-analyze",
                "verify",
                str(SYSTEM / "api-quota-broker-ops@.service"),
                str(SYSTEM / "api-quota-broker-history-audit@.service"),
            ),
            timeout=15,
        )
        self.e.native(("/usr/bin/systemctl", "daemon-reload"), timeout=15)
        raw = self.e.native(("/usr/bin/python3.14", "-I", "-B", "-S", str(BASE / "ops_entry.py")))
        plan = self.e.strict_json(raw)
        need(
            plan["operations"] == ["inspect", "history_audit"] and plan["restart_enabled"] is False,
            "upgrade_validation",
        )

    def verify_preservation(self):
        self.no_workers()
        need(self.invariants() == self.before, "upgrade_preservation")

    def rollback(self):
        # Restore only our exact hashes; never overwrite a concurrent actor's work.
        for path, sha in list(self.staged.items()):
            self.e.read_root(path, sha=sha, mode=0o644)
            path.unlink()
            self.staged.pop(path)
        for path, sha in reversed(list(self.replaced.items())):
            current = self.e.read_root(path, mode=0o644)
            if current != self.originals[path]:
                self.replace(path, self.originals[path], sha)
        for path, sha in self.additions.items():
            self.e.read_root(path, sha=sha, mode=0o644)
            path.unlink()  # Only transaction-created public source/unit bytes.
        if self.audit_created:
            need({p.name for p in AUDIT.iterdir()} == {"reader.lock"}, "upgrade_preservation")
            self.e.read_root(AUDIT / "reader.lock", mode=0o600, limit=0)
            (AUDIT / "reader.lock").unlink()
            AUDIT.rmdir()
        self.e.native(("/usr/bin/systemctl", "daemon-reload"), timeout=15)
        self.e.package()
        need(self.invariants() == self.before, "upgrade_preservation")

    def close(self):
        if self.lockfd is not None:
            os.close(self.lockfd)


def main():
    if sys.argv[1:] == []:
        print(
            '{"mode":"history_ops_upgrade_review","host_changes":0,"credential_reads":0,"native_audit_executed":false}'
        )
        return 0
    try:
        need(
            sys.argv[1:] == ["--apply"]
            and os.geteuid() == 0
            and os.uname().nodename == "asus-ubuntu2604-server",
            "upgrade_source_untrusted",
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        libc = ctypes.CDLL(None)
        need(
            libc.prctl(4, 0, 0, 0, 0) == 0
            and libc.prctl(3, 0, 0, 0, 0) == 0
            and resource.getrlimit(resource.RLIMIT_CORE) == (0, 0),
            "upgrade_source_untrusted",
        )
        name = "api-quota-broker-ops-history-upgrade.service"
        need(
            Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + name,
            "upgrade_source_untrusted",
        )
        group = Path("/sys/fs/cgroup/system.slice") / name
        need(
            (group / "memory.max").read_text().strip() == "134217728"
            and (group / "memory.swap.max").read_text().strip() == "0",
            "upgrade_source_untrusted",
        )
        source, entry = load_sources()
        previous = signal.signal(
            signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Blocked("upgrade_budget"))
        )
        signal.alarm(240)  # 120-second cleanup reserve within the 360-second unit.
        try:
            ops = Native(source, entry)
            result = upgrade(ops)
            if ops.claimed:
                try:
                    entry.write_exclusive(
                        RESULT, json.dumps(result, sort_keys=True).encode() + b"\n"
                    )
                except BaseException:  # noqa: BLE001 - persistent outcome unknown, retain claim.
                    result = {
                        "status": "blocked",
                        "code": "upgrade_receipt_unverified",
                        "automatic_retry": False,
                        "installed_state_unknown": True,
                        "Broker_restart": False,
                        "Doppler_GET": 0,
                    }
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    except BaseException:  # noqa: BLE001 - no raw privileged exception output.
        result = {
            "status": "blocked",
            "code": "upgrade_preflight_unverified",
            "automatic_retry": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
