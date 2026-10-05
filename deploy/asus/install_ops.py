"""Reviewable complete ops bootstrap. Default never mutates the host.

Human root TTY + sealed private copy + bounded transient unit required for apply.
The original morris password file is never opened, moved, copied or deleted.
"""

import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

BOOT_UNIT = "api-quota-broker-ops-bootstrap.service"
ACCOUNT = "broker-deploy"
BASE = Path("/usr/local/lib/api-quota-broker-ops")
CONFIG = Path("/etc/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
RUN = Path("/run/api-quota-broker-ops")
SUDOERS = Path("/etc/sudoers.d/api-quota-broker-ops")
LIBEXEC = Path("/usr/local/libexec")
SYSTEM = Path("/etc/systemd/system")
NAMES = (
    "ops_entry.py",
    "broker_ops_policy.py",
    "install_ops.py",
    "api-quota-broker-ops.socket",
    "api-quota-broker-ops@.service",
    "api-quota-broker-control",
    "api-quota-broker-ops-client",
    "broker_ops.sudoers.proposal",
    "api-quota-broker-ops.tmpfiles",
)
SAFE_CODES = {
    "host_identity",
    "account_exists",
    "peer_identity",
    "ops_artifact_exists",
    "sudo_version",
    "ops_unit_exists",
    "ssh_authentication_not_isolated",
    "ops_home_exists",
    "ops_job_exists",
    "native_tty_required",
    "human_scope_attestation",
    "expiry_invalid",
    "token_format",
    "account_identity",
    "password_inactive",
    "host_key_metadata_unverified",
    "effective_sudo_policy_unverified",
    "wrong_password_accepted",
    "unauthorized_or_cached_access",
    "home_created",
    "native_inspect",
    "worker_cleanup_unverified",
    "policy_drift",
    "activation_unverified",
    "services_changed",
    "account_expiry_active",
}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
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
LOCK = ("/usr/sbin/usermod", "--lock", "--expiredate", "1", "--", ACCOUNT)


class Blocked(ValueError):
    """Only static error codes reach receipts."""


def require(ok, code):
    if not ok:
        raise Blocked(code)


def sealed_sources():
    root = Path(__file__).absolute().parent
    s = root.lstat()
    require(
        root.parent == Path("/var/tmp")
        and re.fullmatch("aqb-ops-bootstrap-[a-f0-9]{32}", root.name)
        and stat.S_ISDIR(s.st_mode)
        and s.st_uid == s.st_gid == 0
        and stat.S_IMODE(s.st_mode) == 0o700,
        "private_bootstrap_required",
    )
    raw = {}
    for name in (*NAMES, "seal.json"):
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            require(
                stat.S_ISREG(s.st_mode)
                and s.st_uid == s.st_gid == 0
                and s.st_nlink == 1
                and stat.S_IMODE(s.st_mode) == 0o600
                and s.st_size <= 131072,
                "private_source_untrusted",
            )
            raw[name] = f.read(131073)
    seal = json.loads(raw.pop("seal.json"))
    require(
        set(seal) == {"schema", "files"}
        and seal["schema"] == 1
        and set(seal["files"]) == set(NAMES),
        "seal_contract",
    )
    for name in NAMES:
        require(hashlib.sha256(raw[name]).hexdigest() == seal["files"][name], "source_pin_mismatch")
    spec = importlib.util.spec_from_file_location("aqb_bootstrap_entry", root / "ops_entry.py")
    require(spec and spec.loader, "sealed_loader")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return raw, mod


def expiry(value, now):
    d = datetime.fromisoformat(value)
    require(
        d.tzinfo is not None and now + timedelta(minutes=15) < d <= now + timedelta(days=30),
        "expiry_invalid",
    )
    return d.astimezone(UTC).isoformat()


def tty_line(fd, prompt, *, hidden=False, limit=300):
    require(os.isatty(fd), "native_tty_required")
    settings = termios.tcgetattr(fd)
    try:
        if hidden:
            changed = settings[:]
            changed[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(fd, termios.TCSAFLUSH, changed)
            require(not termios.tcgetattr(fd)[3] & termios.ECHO, "tty_noecho_failed")
        os.write(fd, prompt.encode("ascii"))
        raw = bytearray()
        while len(raw) <= limit:
            b = os.read(fd, 1)
            require(b != b"", "tty_eof")
            if b == b"\n":
                return raw
            raw.extend(b)
        raise Blocked("tty_bound")
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, settings)
        if hidden:
            os.write(fd, b"\n")


def initialize(ops):
    """Fixed transaction, effect journal before every fallible mutation."""
    token, password = None, None
    stage = "preflight"
    try:
        ops.preflight()
        stage = "memory_guard"
        ops.guard()
        stage = "human_scope_and_token"
        token, expires = ops.input_token()
        stage = "fresh_doppler"
        password = ops.fetch_password(token)
        stage = "account_create"
        ops.create_account()
        stage = "account_password"
        ops.set_password(password)
        stage = "sealed_publish"
        ops.publish(token, expires)
        stage = "native_authorization_checks"
        ops.native_checks()
        stage = "native_socket_inspect"
        ops.inspect_once()
        stage = "activation"
        ops.activate()
        stage = "preservation_verify"
        ops.preservation()
        receipt = {
            "status": "passed",
            "mode": "asus_ops_socket_initialized",
            "native_inspect_verified": True,
            "native_restart_executed": False,
            "source_password_file_used": False,
            "source_cleanup_pending": True,
            "scope_verification": "human_dashboard_attestation_only",
            "operations": ["inspect", "restart"],
            "provider_calls": 0,
            "automatic_retry": False,
        }
        ops.receipt(receipt)
        return receipt
    except BaseException as error:  # noqa: BLE001 - rollback/redaction includes interruption.
        try:
            rolled_back = ops.rollback()
        except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
            rolled_back = False
        receipt = {
            "status": "blocked",
            "mode": "asus_ops_socket_initialization",
            "stage": stage,
            "code": str(error)
            if type(error) is Blocked and str(error) in SAFE_CODES
            else "initialization_unverified",
            "rollback_verified": rolled_back,
            "manual_recovery_required": not rolled_back,
            "automatic_retry": False,
            "native_restart_executed": False,
            "provider_calls": 0,
        }
        try:
            ops.receipt(receipt)
        except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
            receipt["receipt_persistence_unverified"] = True
        return receipt
    finally:
        for value in (token, password):
            if isinstance(value, bytearray):
                value[:] = b"\0" * len(value)


class NativeBootstrap:
    def __init__(self, source, entry):
        self.source, self.e = source, entry
        self.created = False
        self.creation_attempted = False
        self.uid = self.gid = None
        self.published = {}
        self.receipt_dir = None
        self.socket_started = False
        self.enabled_link = False
        self.daemon_changed = False

    def run(self, argv, *, data=None, limit=16384, timeout=15):
        return self.e.native(argv, data=data, limit=limit, timeout=timeout)

    def preflight(self):
        require(
            os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server", "host_identity"
        )
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(ACCOUNT)
            except KeyError:
                continue
            raise Blocked("account_exists")
        user = pwd.getpwnam("morris")
        require(user.pw_uid == user.pw_gid == 1000, "peer_identity")
        for p in (
            BASE,
            CONFIG,
            STATE,
            RUN,
            SUDOERS,
            SYSTEM / "api-quota-broker-ops.socket",
            SYSTEM / "api-quota-broker-ops@.service",
            LIBEXEC / "api-quota-broker-control",
            LIBEXEC / "api-quota-broker-ops-client",
            Path("/etc/tmpfiles.d/api-quota-broker-ops.conf"),
            SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket",
        ):
            require(not p.exists() and not p.is_symlink(), "ops_artifact_exists")
        for p in (
            BASE.parent,
            CONFIG.parent,
            STATE.parent,
            RUN.parent,
            SUDOERS.parent,
            LIBEXEC.parent,
            SYSTEM,
            Path("/etc/tmpfiles.d"),
            Path("/var/backups/api-quota-broker"),
        ):
            self.e.root_dir(p)
        if LIBEXEC.exists():
            self.e.root_dir(LIBEXEC)
        require(
            self.run(("/usr/bin/sudo", "--version")).startswith(b"sudo-rs 0.2.13"), "sudo_version"
        )
        for unit in ("api-quota-broker-ops.socket", "api-quota-broker-ops@.service"):
            require(
                self.run(
                    ("/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value")
                ).strip()
                == b"not-found",
                "ops_unit_exists",
            )
        self.run(("/usr/sbin/visudo", "-c"))
        self.baseline = {
            name: self.e.service_state(name) for name in (self.e.SERVICE, "orderflow.service")
        }
        self.config_sha = hashlib.sha256(
            self.e.read_root("/etc/api-quota-broker/gateway.json")
        ).hexdigest()
        self.e.broker_pins(self.config_sha)
        self.verify_ssh()
        require(not Path("/nonexistent").exists(), "ops_home_exists")
        for p in (Path("/etc/crontab"), Path("/etc/cron.d"), Path("/var/spool/cron/crontabs")):
            if p.exists():
                self.e.root_dir(p if p.is_dir() else p.parent)
                if p.is_dir():
                    require(not any(q.name == ACCOUNT for q in p.iterdir()), "ops_job_exists")
        self.receipt_dir = Path("/var/backups/api-quota-broker") / (
            "ops-bootstrap-" + os.urandom(16).hex()
        )
        self.receipt_dir.mkdir(mode=0o700)
        self.receipt(
            {"status": "prepared", "mode": "asus_ops_socket_bootstrap", "provider_calls": 0}
        )

    def verify_ssh(self):
        # Repeat after account creation: Match Group may now match its private group.
        raw = self.run(
            ("/usr/sbin/sshd", "-T", "-C", "user=broker-deploy,host=localhost,addr=127.0.0.1")
        )
        ssh = dict(l.split(" ", 1) for l in raw.decode().splitlines() if " " in l)
        require(
            ssh.get("passwordauthentication") == "no"
            and ssh.get("kbdinteractiveauthentication") == "no"
            and ssh.get("authorizedkeyscommand") == "none"
            and ssh.get("trustedusercakeys") == "none"
            and ssh.get("authorizedkeysfile")
            in (".ssh/authorized_keys .ssh/authorized_keys2", "none"),
            "ssh_authentication_not_isolated",
        )

    def guard(self):
        self.e.memory_guard(bootstrap=True)
        require(os.isatty(0) and os.isatty(1) and os.isatty(2), "native_tty_required")

    def input_token(self):
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NOFOLLOW)
        token = None
        try:
            raw = tty_line(
                fd,
                "Dashboard: api-quota-broker-ops/dev, READ ONLY, expiry <=30 days. Type READONLY30: ",
                limit=16,
            )
            require(raw == b"READONLY30", "human_scope_attestation")
            exp = tty_line(fd, "Actual Dashboard expiry UTC (YYYY-MM-DDTHH:MM:SSZ): ", limit=40)
            expires = expiry(exp.decode("ascii"), datetime.now(UTC))
            token = tty_line(fd, "Ops Service Token (hidden, one input): ", hidden=True)
            require(re.fullmatch(rb"dp\.st\.dev\.[A-Za-z0-9_-]{32,256}", token), "token_format")
            return token, expires
        except BaseException:
            self.e.wipe(token)
            raise
        finally:
            os.close(fd)

    def fetch_password(self, token):
        root = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "aqb_bootstrap_policy", root / "broker_ops_policy.py"
        )
        require(spec and spec.loader, "policy_loader")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        return policy.password_from_doppler(token, self.e.doppler_transport)

    def account_identity(self):
        a, g = pwd.getpwnam(ACCOUNT), grp.getgrnam(ACCOUNT)
        runtime = pwd.getpwnam("api-quota-broker")
        require(
            0 < a.pw_uid < 1000
            and a.pw_uid != runtime.pw_uid
            and 0 < g.gr_gid < 1000
            and g.gr_gid != runtime.pw_gid
            and a.pw_gid == g.gr_gid
            and not g.gr_mem
            and set(os.getgrouplist(ACCOUNT, g.gr_gid)) == {g.gr_gid}
            and a.pw_dir == "/nonexistent"
            and a.pw_shell == "/usr/sbin/nologin",
            "account_identity",
        )
        return a.pw_uid, a.pw_gid

    def create_account(self):
        self.creation_attempted = True
        self.run(CREATE)
        self.created = True
        self.uid, self.gid = self.account_identity()

    def set_password(self, password):
        payload = bytearray(ACCOUNT.encode() + b":")
        payload.extend(password)
        payload.extend(b"\n")
        try:
            self.run(("/usr/sbin/chpasswd",), data=memoryview(payload))
            # PAM authentication must be active during native validation.
            self.run(("/usr/sbin/usermod", "--expiredate", "", "--", ACCOUNT))
            require(
                self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
                == [ACCOUNT.encode(), b"P"],
                "password_inactive",
            )
        finally:
            self.e.wipe(payload)

    def install(self, path, raw, mode=0o644):
        # Track intent first: partial writes still need rollback ownership checks.
        self.published[path] = hashlib.sha256(raw).hexdigest()
        self.e.write_exclusive(path, raw, mode=mode)

    def policy_bytes(self, *, enabled):
        return (
            json.dumps(
                {
                    "schema": 1,
                    "expires_at": self.expires,
                    "issued_at": self.issued,
                    "config_sha256": self.config_sha,
                    "ops_uid": self.uid,
                    "ops_gid": self.gid,
                    "peer_uid": 1000,
                    "scope_verification": "human_dashboard_attestation_only",
                    "enabled": enabled,
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )

    def publish(self, token, expires):
        self.expires = expires
        self.issued = datetime.now(UTC).isoformat()
        for p in (BASE, CONFIG, STATE):
            mode = 0o700 if p == STATE else 0o755
            p.mkdir(mode=mode)
            p.chmod(mode)
        RUN.mkdir(mode=0o750)
        RUN.chmod(0o750)
        os.chown(RUN, 0, 1000)
        if not LIBEXEC.exists():
            LIBEXEC.mkdir(mode=0o755)
            LIBEXEC.chmod(0o755)
        self.install(
            Path("/etc/tmpfiles.d/api-quota-broker-ops.conf"),
            self.source["api-quota-broker-ops.tmpfiles"],
        )
        for name in ("ops_entry.py", "broker_ops_policy.py"):
            self.install(BASE / name, self.source[name])
        self.install(
            BASE / "manifest.json",
            json.dumps(
                {
                    "schema": 1,
                    "files": {
                        n: hashlib.sha256(self.source[n]).hexdigest()
                        for n in ("ops_entry.py", "broker_ops_policy.py")
                    },
                },
                sort_keys=True,
            ).encode()
            + b"\n",
        )
        self.install(CONFIG / "policy.json", self.policy_bytes(enabled=False))
        self.install(STATE / "operation.lock", b"", 0o600)
        # Host key already exists from Broker deployment. Never create/replace it.
        key = Path("/var/lib/systemd/credential.secret")
        self.e.root_dir(key.parent)
        s = key.lstat()
        require(
            stat.S_ISREG(s.st_mode)
            and s.st_uid == s.st_gid == 0
            and s.st_nlink == 1
            and stat.S_IMODE(s.st_mode) == 0o600,
            "host_key_metadata_unverified",
        )
        ciphertext = self.run(
            (
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                "--name=ops_doppler",
                "-",
                "-",
            ),
            data=memoryview(token),
            limit=16384,
        )
        self.install(CONFIG / "ops_doppler.cred", ciphertext, 0o600)
        for name in ("api-quota-broker-control", "api-quota-broker-ops-client"):
            self.install(LIBEXEC / name, self.source[name], 0o755)
        for name in ("api-quota-broker-ops.socket", "api-quota-broker-ops@.service"):
            self.install(SYSTEM / name, self.source[name])
        self.install(SUDOERS, self.source["broker_ops.sudoers.proposal"], 0o440)
        self.run(("/usr/sbin/visudo", "-c"))
        self.run(
            (
                "/usr/bin/systemd-analyze",
                "verify",
                str(SYSTEM / "api-quota-broker-ops.socket"),
                str(SYSTEM / "api-quota-broker-ops@.service"),
            )
        )
        self.daemon_changed = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))

    def as_ops(self, argv, *, data=None, timeout=20):
        return self.run(
            ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *argv), data=data, timeout=timeout
        )

    def native_checks(self):
        self.verify_ssh()
        require(
            re.search(
                rb"(?m)^Account expires\s*:\s*never\s*$",
                self.run(("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)),
            ),
            "account_expiry_active",
        )
        # All policy listing is non-executing. No arbitrary privileged program.
        raw = self.run(("/usr/bin/sudo", "-l", "-U", ACCOUNT), limit=16384).decode("ascii")
        lines = [l.strip() for l in raw.splitlines() if l.lstrip().startswith("(")]
        require(
            len(lines) == 1
            and re.fullmatch(
                r'\(root\s*:\s*root\)\s+(?:PASSWD:\s*)?/usr/local/libexec/api-quota-broker-control(?:\s+""|)',
                lines[0],
            ),
            "effective_sudo_policy_unverified",
        )
        # Wrong password is public synthetic input; never read the real password
        # into a diagnostic child or execute a privileged operation on failure.
        p = subprocess.run(
            ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *self.e.sudo_argv()),
            input=b"INVALID_PUBLIC_OPS_FIXTURE\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(ENV),
            timeout=15,
            check=False,
        )
        require(p.returncode != 0 and self.e.READY not in p.stdout, "wrong_password_accepted")
        for argv in (
            self.e.sudo_argv(True),
            ("/usr/bin/sudo", "-k", "-n", "-p", "", "--", self.e.CONTROL, "inspect"),
            ("/usr/bin/sudo", "-k", "-n", "-p", "", "--", "/usr/bin/true"),
        ):
            p = subprocess.run(
                ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *argv),
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(ENV),
                timeout=10,
                check=False,
            )
            require(
                p.returncode != 0 and self.e.READY not in p.stdout, "unauthorized_or_cached_access"
            )
        for command in ((self.e.CONTROL, "inspect"), ("/usr/bin/true",)):
            p = subprocess.run(
                ("/usr/bin/sudo", "-l", "-U", ACCOUNT, "--", *command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(ENV),
                timeout=10,
                check=False,
            )
            require(p.returncode != 0, "unauthorized_or_cached_access")
        # Absence of authorized keys plus effective SSH auth restrictions gates
        # password/SFTP/forwarding admission; no sshd or PAM modifications.
        require(not Path("/nonexistent").exists(), "home_created")
        self.run(("/usr/bin/passwd", "--status", ACCOUNT))

    def inspect_once(self):
        self.socket_started = True
        self.run(("/usr/bin/systemctl", "start", "api-quota-broker-ops.socket"))
        p = self.run(
            (
                "/usr/sbin/runuser",
                "-u",
                "morris",
                "--",
                str(LIBEXEC / "api-quota-broker-ops-client"),
                "inspect",
            ),
            timeout=45,
        )
        result = self.e.strict_json(p)
        require(
            result.get("status") == "passed" and result.get("operation") == "inspect",
            "native_inspect",
        )
        # Socket activation jobs must fully exit before considering readiness.
        end = time.monotonic() + 8
        while time.monotonic() < end:
            active = self.run(
                (
                    "/usr/bin/systemctl",
                    "list-units",
                    "api-quota-broker-ops@*.service",
                    "--state=active,activating,deactivating",
                    "--no-legend",
                    "--no-pager",
                )
            )
            if not active.strip():
                break
            time.sleep(0.2)
        else:
            raise Blocked("worker_cleanup_unverified")
        self.preservation()

    def activate(self):
        old = self.e.read_root(CONFIG / "policy.json", mode=0o644)
        require(
            hashlib.sha256(old).hexdigest() == self.published[CONFIG / "policy.json"],
            "policy_drift",
        )
        new = self.policy_bytes(enabled=True)
        stage = CONFIG / "policy.ready.json"
        self.e.write_exclusive(stage, new, mode=0o644)
        os.replace(stage, CONFIG / "policy.json")
        fd = os.open(CONFIG, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.published[CONFIG / "policy.json"] = hashlib.sha256(new).hexdigest()
        self.enabled_link = True
        self.run(("/usr/bin/systemctl", "enable", "api-quota-broker-ops.socket"))
        require(
            self.run(("/usr/bin/systemctl", "is-active", "api-quota-broker-ops.socket")).strip()
            == b"active",
            "activation_unverified",
        )

    def preservation(self):
        self.e.broker_pins(self.config_sha)
        require(
            {n: self.e.service_state(n) for n in self.baseline} == self.baseline, "services_changed"
        )

    def rollback(self):
        ok = True
        if self.socket_started or self.enabled_link:
            try:
                self.run(("/usr/bin/systemctl", "disable", "--now", "api-quota-broker-ops.socket"))
                self.run(("/usr/bin/systemctl", "stop", "api-quota-broker-ops@*.service"))
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        if self.created or self.creation_attempted:
            try:
                actual = self.account_identity()
                require(self.uid is None or actual == (self.uid, self.gid), "rollback_uid_drift")
                self.uid, self.gid = actual
                self.run(LOCK)
                require(
                    self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
                    == [ACCOUNT.encode(), b"L"],
                    "rollback_lock",
                )
                require(
                    re.search(
                        rb"(?m)^Account expires\s*:\s*1970-01-02\s*$",
                        self.run(("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)),
                    ),
                    "rollback_expiry",
                )
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        # Revoke only newly published access artifacts with exact ownership/pins.
        # Preserve account, encrypted token, safe receipts and replay history.
        revoke = (
            SUDOERS,
            LIBEXEC / "api-quota-broker-control",
            LIBEXEC / "api-quota-broker-ops-client",
            SYSTEM / "api-quota-broker-ops.socket",
            SYSTEM / "api-quota-broker-ops@.service",
            Path("/etc/tmpfiles.d/api-quota-broker-ops.conf"),
        )
        for p in revoke:
            if p in self.published and p.exists():
                try:
                    raw = self.e.read_root(p, sha=self.published[p])
                    require(
                        hashlib.sha256(raw).hexdigest() == self.published[p],
                        "rollback_artifact_drift",
                    )
                    p.unlink()
                except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                    ok = False
        if self.daemon_changed:
            try:
                self.run(("/usr/bin/systemctl", "daemon-reload"))
                self.run(("/usr/sbin/visudo", "-c"))
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        if hasattr(self, "baseline"):
            try:
                self.preservation()
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        return ok

    def receipt(self, result):
        if self.receipt_dir is not None:
            p = self.receipt_dir / ("receipt-" + os.urandom(8).hex() + ".json")
            self.e.write_exclusive(p, json.dumps(result, sort_keys=True).encode() + b"\n")


def main():
    if len(sys.argv) == 1:
        print(
            json.dumps(
                {
                    "mode": "asus_ops_complete_bootstrap_candidate",
                    "apply": False,
                    "source_password_file_used": False,
                    "token_days_max": 30,
                    "native_restart_executed": False,
                    "host_changes": 0,
                    "provider_calls": 0,
                }
            )
        )
        return 0
    try:
        require(sys.argv[1:] == ["--apply"], "arguments_denied")
        source, entry = sealed_sources()
        result = initialize(NativeBootstrap(source, entry))
    except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
        result = {"status": "blocked", "code": "bootstrap_unverified", "automatic_retry": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
