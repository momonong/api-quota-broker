"""One-time account preparation candidate; default is a zero-effect plan.

Apply needs a human's root bootstrap, pinned root-private code and bounded unit.
It consumes the already generated ASUS password locally, leaving the new account
locked and expired. No sudoers, login, ops service, token or provider is enabled.
"""

import ctypes
import grp
import hashlib
import json
import os
import pwd
import re
import resource
import stat
import subprocess
import sys
from pathlib import Path

ACCOUNT = "broker-deploy"
UNIT = "api-quota-broker-ops-init.service"
SOURCE_HOME = Path("/home/morris")
SOURCE_PARTS = (".local", "share", "api-quota-broker-ops", "deploy-password.txt")
SOURCE_UID = SOURCE_GID = 1000
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}
CREATE = (
    "/usr/sbin/useradd",
    "--system",
    "--user-group",
    "--no-create-home",
    "--home-dir",
    "/nonexistent",
    "--shell",
    "/usr/sbin/nologin",
    "--expiredate",
    "1",
    "--",
    ACCOUNT,
)
SET_PASSWORD = ("/usr/sbin/chpasswd",)
LOCK = ("/usr/sbin/usermod", "--lock", "--expiredate", "1", "--", ACCOUNT)
STATUS = ("/usr/bin/passwd", "--status", ACCOUNT)
EXPIRY = ("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)
ERROR_CODES = {
    "wrong_host_or_uid",
    "root_private_code_required",
    "code_pin_mismatch",
    "account_or_group_exists",
    "source_user_drift",
    "core_limit_unverified",
    "initialization_unit_unverified",
    "swap_limit_unverified",
    "memory_limit_unverified",
    "dumpability_unverified",
    "source_input_unverified",
    "source_format_unverified",
    "native_command_failed",
    "new_account_identity_unverified",
    "new_account_identity_changed",
    "account_not_locked_and_expired",
    "existing_service_unhealthy",
    "existing_services_changed",
}


class Blocked(ValueError):
    """Fixed codes only, never private exception prose."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def wipe(value):
    if type(value) is bytearray:
        value[:] = b"\0" * len(value)


def source_password(anchor=SOURCE_HOME, *, owner=SOURCE_UID, group=SOURCE_GID):
    """One descriptor-based read; no password hash, output or new plaintext file.

    Anchor/owner injection exists for unprivileged synthetic fixtures only. CLI
    cannot choose a path, user, executable or group.
    """
    fd, raw = None, None
    try:
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in SOURCE_PARTS[:-1]:
            info = os.fstat(fd)
            require(
                info.st_uid == owner and info.st_gid == group and not info.st_mode & 0o022,
                "source_parent_untrusted",
            )
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        require(
            info.st_uid == owner and info.st_gid == group and stat.S_IMODE(info.st_mode) == 0o700,
            "source_directory_untrusted",
        )
        child = os.open(SOURCE_PARTS[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = child
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == owner
            and before.st_gid == group
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1
            and before.st_size == 41,
            "source_file_untrusted",
        )
        raw = bytearray(os.read(fd, 42))
        after = os.fstat(fd)
        require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "source_changed_during_read",
        )
        require(re.fullmatch(rb"[A-Za-z0-9_-]{40}\n", raw) is not None, "source_format_unverified")
        return raw
    except Exception:  # noqa: BLE001 - no path/body/exception text returned.
        wipe(raw)
        raise Blocked("source_input_unverified") from None
    finally:
        if fd is not None:
            os.close(fd)


def memory_guard():
    require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0), "core_limit_unverified")
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    require(
        len(lines) == 1 and lines[0] == "0::/system.slice/" + UNIT, "initialization_unit_unverified"
    )
    group = Path("/sys/fs/cgroup/system.slice") / UNIT
    require((group / "memory.swap.max").read_text().strip() == "0", "swap_limit_unverified")
    require((group / "memory.max").read_text().strip() == "134217728", "memory_limit_unverified")
    libc = ctypes.CDLL(None, use_errno=True)
    require(
        libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0, "dumpability_unverified"
    )


def pinned_execution():
    source = Path(__file__).absolute()
    parent = source.parent
    require(
        parent.parent == Path("/var/tmp")
        and re.fullmatch(r"aqb-ops-init-[a-f0-9]{32}", parent.name) is not None,
        "root_private_code_required",
    )
    info = parent.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == 0o700,
        "root_private_code_required",
    )
    values = []
    for path in (parent / "code.sha256", source):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and info.st_size < 65536,
                "root_private_code_required",
            )
            values.append(stream.read(65536))
    require(
        re.fullmatch(rb"[a-f0-9]{64}\n", values[0]) is not None
        and hashlib.sha256(values[1]).hexdigest().encode() + b"\n" == values[0],
        "code_pin_mismatch",
    )


class NativeOps:
    def __init__(self):
        self.create_completed = False
        self.new_uid = None

    def _run(self, argv, *, data=None, capture=False):
        require(
            argv in (CREATE, SET_PASSWORD, LOCK, STATUS, EXPIRY)
            or (
                len(argv) == 4
                and argv[:2] == ("/usr/bin/systemctl", "show")
                and argv[2] in ("api-quota-broker.service", "orderflow.service")
                and argv[3]
                == "--property=ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,NRestarts"
            ),
            "command_denied",
        )
        result = subprocess.run(
            argv,
            input=data,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(SAFE_ENV),
            timeout=10,
            check=False,
        )
        require(result.returncode == 0, "native_command_failed")
        if capture:
            require(len(result.stdout) <= 8192, "native_output_bound")
            return result.stdout.decode("ascii")
        return None

    def services(self):
        result = {}
        for name in ("api-quota-broker.service", "orderflow.service"):
            text = self._run(
                (
                    "/usr/bin/systemctl",
                    "show",
                    name,
                    "--property=ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,NRestarts",
                ),
                capture=True,
            )
            values = dict(v.split("=", 1) for v in text.splitlines())
            require(
                values.get("ActiveState") == "active"
                and values.get("SubState") == "running"
                and values.get("MainPID", "").isdigit()
                and int(values["MainPID"]) > 0,
                "existing_service_unhealthy",
            )
            result[name] = values
        return result

    def preflight(self):
        require(
            os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server",
            "wrong_host_or_uid",
        )
        pinned_execution()
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(ACCOUNT)
            except KeyError:
                continue
            raise Blocked("account_or_group_exists")
        require(pwd.getpwnam("morris").pw_uid == SOURCE_UID, "source_user_drift")
        runtime = pwd.getpwnam("api-quota-broker")
        self.runtime_uid, self.runtime_gid = runtime.pw_uid, runtime.pw_gid
        self.new_uid = None
        return self.services()

    def guard(self):
        memory_guard()

    def password(self):
        return source_password()

    def create(self):
        self._run(CREATE)
        # Successful native mutation precedes identity verification. Preserve
        # this evidence even if lookup/verification raises; never claim no user
        # was created merely because this method did not return.
        self.create_completed = True
        account = self._new_identity()
        self.new_uid = account.pw_uid

    def _new_identity(self):
        account = pwd.getpwnam(ACCOUNT)
        group = grp.getgrnam(ACCOUNT)
        require(
            0 < account.pw_uid < 1000
            and account.pw_uid != self.runtime_uid
            and account.pw_shell == "/usr/sbin/nologin"
            and account.pw_dir == "/nonexistent"
            and account.pw_gid == group.gr_gid
            and 0 < group.gr_gid < 1000
            and group.gr_gid != self.runtime_gid
            and not group.gr_mem
            and set(os.getgrouplist(ACCOUNT, group.gr_gid)) == {group.gr_gid},
            "new_account_identity_unverified",
        )
        return account

    def set_password(self, data):
        self._run(SET_PASSWORD, data=data)

    def lock(self):
        require(
            self.new_uid is not None and pwd.getpwnam(ACCOUNT).pw_uid == self.new_uid,
            "new_account_identity_changed",
        )
        self._run(LOCK)

    def verify(self):
        user = pwd.getpwnam(ACCOUNT)
        group = grp.getgrnam(ACCOUNT)
        require(
            user.pw_uid == self.new_uid
            and user.pw_gid == group.gr_gid
            and group.gr_gid != 0
            and not group.gr_mem
            and set(os.getgrouplist(ACCOUNT, group.gr_gid)) == {group.gr_gid}
            and user.pw_shell == "/usr/sbin/nologin"
            and user.pw_dir == "/nonexistent",
            "new_account_identity_unverified",
        )
        status = self._run(STATUS, capture=True).split()
        expiry = self._run(EXPIRY, capture=True)
        require(
            len(status) >= 2
            and status[:2] == [ACCOUNT, "L"]
            and re.search(r"(?m)^Account expires\s*:\s*1970-01-02\s*$", expiry),
            "account_not_locked_and_expired",
        )
        return {
            "uid": user.pw_uid,
            "gid": user.pw_gid,
            "shell": user.pw_shell,
            "locked": True,
            "expired": True,
            "supplementary_groups": [],
        }

    def rollback(self):
        try:
            if self.new_uid is None and self.create_completed:
                # One read-only identity probe after successful useradd. Never
                # lock a pre-existing/colliding/unverified account or retry create.
                self.new_uid = self._new_identity().pw_uid
            self.lock()
            self.verify()
            return True
        except Exception:  # noqa: BLE001 - fixed rollback evidence only.
            return False


def initialize(ops):
    stage = "preflight"
    raw, payload, created = None, None, False
    try:
        baseline = ops.preflight()
        stage = "memory_guard"
        ops.guard()
        stage = "source_input"
        raw = ops.password()
        require(
            type(raw) is bytearray and re.fullmatch(rb"[A-Za-z0-9_-]{40}\n", raw) is not None,
            "source_format_unverified",
        )
        stage = "account_create"
        ops.create()
        created = True
        payload = bytearray(ACCOUNT.encode() + b":")
        payload.extend(raw)
        stage = "password_set"
        ops.set_password(memoryview(payload))
        wipe(payload)
        wipe(raw)
        stage = "account_lock"
        ops.lock()
        stage = "account_verify"
        account = ops.verify()
        stage = "service_verify"
        require(ops.services() == baseline, "existing_services_changed")
        return {
            "status": "account_prepared_locked",
            "account": ACCOUNT,
            "identity": account,
            "source_cleanup_pending": True,
            "source_file_morris_readable": True,
            "doppler_value_match": "not_verified",
            "sudo_access_enabled": False,
            "persistent_ops_service": False,
            "provider_calls": 0,
            "doppler_calls": 0,
            "automatic_retry": False,
        }
    except Exception as error:  # noqa: BLE001 - do not render secret-bearing errors.
        try:
            locked = (
                ops.rollback()
                if created or getattr(ops, "create_completed", False) is True
                else None
            )
        except Exception:  # noqa: BLE001 - rollback diagnostics must never render exception text.
            locked = False
        return {
            "status": "blocked",
            "stage": stage,
            "code": str(error)
            if type(error) is Blocked and str(error) in ERROR_CODES
            else "initialization_unverified",
            "account_may_exist": created or stage == "account_create",
            "rollback_lock_verified": locked,
            "automatic_retry": False,
            "source_cleanup_pending": True,
            "sudo_access_enabled": False,
            "provider_calls": 0,
            "doppler_calls": 0,
        }
    finally:
        wipe(payload)
        wipe(raw)


def bootstrap_wrapper(source_sha, *, enabled=False):
    require(re.fullmatch(r"[a-f0-9]{64}", source_sha) is not None, "code_pin_invalid")
    # Literal Python is fully compiled before execution; neither root shell nor
    # initializer parses a human TTY. Only sudo's actual prompt reads the TTY.
    script = r"""
import hashlib,json,os,stat,subprocess,uuid
from pathlib import Path
try:
    if os.geteuid()!=0 or os.uname().nodename!="asus-ubuntu2604-server":raise ValueError()
    unit=subprocess.run(["/usr/bin/systemctl","show","api-quota-broker-ops-init.service","--property=LoadState","--value"],capture_output=True,text=True,timeout=5,check=True)
    if unit.stdout.strip()!="not-found":raise ValueError()
    source=Path("/var/tmp/api-quota-broker-ops-init-review-2026-10-05/initialize_broker_deploy.py")
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        v=os.fstat(stream.fileno())
        if not stat.S_ISREG(v.st_mode) or v.st_uid!=1000 or v.st_gid!=1000 or stat.S_IMODE(v.st_mode)!=0o600 or v.st_nlink!=1 or v.st_size>=65536:raise ValueError()
        raw=stream.read(65536)
    if hashlib.sha256(raw).hexdigest()!=SOURCE_SHA:raise ValueError()
    anchor=Path("/var/tmp");v=anchor.lstat()
    if not stat.S_ISDIR(v.st_mode) or v.st_uid!=0 or v.st_gid!=0 or stat.S_IMODE(v.st_mode)!=0o1777:raise ValueError()
    root=anchor/("aqb-ops-init-"+uuid.uuid4().hex);os.mkdir(root,0o700)
    for name,value in (("initialize_broker_deploy.py",raw),("code.sha256",(SOURCE_SHA+"\n").encode())):
        fd=os.open(root/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb") as stream:os.fchmod(stream.fileno(),0o600);stream.write(value);stream.flush();os.fsync(stream.fileno())
    private=root/"initialize_broker_deploy.py"
    if hashlib.sha256(private.read_bytes()).hexdigest()!=SOURCE_SHA:raise ValueError()
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
    argv=["/usr/bin/systemd-run","--pipe","--wait","--collect","--unit=api-quota-broker-ops-init","--service-type=exec","--property=MemoryMax=128M","--property=MemorySwapMax=0","--property=LimitCORE=0","--property=CPUQuota=25%","--property=RuntimeMaxSec=45s","/usr/bin/python3.14","-I","-B",str(private),"--apply"]
    os.execve(argv[0],argv,{"PATH":"/usr/bin:/bin","LANG":"C"})
except Exception:
    print(json.dumps({"status":"blocked","stage":"root_copy","code":"root_bootstrap_unverified","automatic_retry":False}),flush=True)
    raise SystemExit(2)
""".strip().replace("SOURCE_SHA", repr(source_sha))
    guard = (
        ""
        if enabled
        else "printf '%s\\n' 'BLOCKED: account initialization candidate not published.' >&2\nexit 1\n"
    )
    return f"""#!/bin/bash
# ONE-TIME LOCKED ACCOUNT PREPARATION; human root, no sudoers/ops activation.
{guard}set -eu
umask 077
/usr/bin/sudo -v
/usr/bin/sudo -n /usr/bin/python3.14 -I -B - <<'PYROOT'
{script}
PYROOT
"""


def plan():
    return {
        "mode": "review_only",
        "account": ACCOUNT,
        "proposed_result": "locked_and_expired",
        "source_password_file": str(SOURCE_HOME.joinpath(*SOURCE_PARTS)),
        "secret_value_reads": 0,
        "source_file_morris_readable": True,
        "source_cleanup": "requires separate explicit authorization; never automatic",
        "doppler_scope": "api-quota-broker-ops/dev; readonly, lifetime unselected",
        "doppler_value_match": "unknown",
        "token_creation": 0,
        "sudo_access_enabled": False,
        "persistent_ops_service": False,
        "host_changes": 0,
        "provider_calls": 0,
        "doppler_calls": 0,
    }


def main():
    if sys.argv[1:] not in ([], ["--apply"]):
        print(
            json.dumps({"status": "blocked", "code": "arguments_denied", "automatic_retry": False})
        )
        return 2
    apply = sys.argv[1:] == ["--apply"]
    result = initialize(NativeOps()) if apply else plan()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "account_prepared_locked" or not apply else 2


if __name__ == "__main__":
    raise SystemExit(main())
