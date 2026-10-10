"""ASUS fixed operations: inetd-style socket worker and zero-argument helper.

No mode is selected by a remote path or executable. Default only prints a plan.
All secrets stay in disposable ASUS processes; exceptions are never rendered.
"""

import ctypes
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
import pwd
import re
import resource
import select
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
CONFIG = Path("/etc/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
CONTROL = "/usr/local/libexec/api-quota-broker-control"
SOCKET = "/run/api-quota-broker-ops/control.sock"
ACCOUNT = "broker-deploy"
SERVICE = "api-quota-broker.service"
SOCKET_UNIT = "api-quota-broker-ops.socket"
BOOT_UNIT = "api-quota-broker-ops-bootstrap.service"
RELEASE = "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
MANIFEST_SHA = RELEASE.rsplit("-", 1)[1]
UNIT_SHA = "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
READY = b"BROKER_AUTH_READY\n"
SERVICE_TOKEN_PATTERN = rb"dp\.st\.(?:[a-z0-9\-_]{2,35}\.)?[a-zA-Z0-9]{40,44}"
PROPS = "ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,NRestarts"

DIAGNOSTIC_STAGES = frozenset(
    (
        "entry",
        "worker_peer",
        "worker_request",
        "worker_prepare",
        "worker_package",
        "worker_policy",
        "worker_identity",
        "worker_guard",
        "worker_probe",
        "worker_credential",
        "worker_doppler",
        "worker_session",
        "worker_operation",
        "worker_cleanup",
        "helper_identity",
        "helper_package",
        "helper_policy",
        "helper_sudo_identity",
        "helper_guard",
        "helper_pins",
        "helper_request",
        "helper_operation",
        "client_identity",
        "client_socket",
        "client_connect",
        "client_response",
        "native_socket_start",
        "native_client",
        "native_worker_cleanup",
    )
)
DIAGNOSTIC_CODES = frozenset(
    (
        "arguments_denied",
        "authentication_bypass_or_probe_failure",
        "authentication_failed",
        "claim_storage_limit",
        "client_denied",
        "core_limit",
        "core_limit_set_failed",
        "core_limit_query_failed",
        "credential_acl",
        "credential_format",
        "credential_metadata",
        "credential_read_denied",
        "credential_scope",
        "diagnostic_unverified",
        "directory_mode_untrusted",
        "doppler_body_bound",
        "doppler_endpoint",
        "doppler_unavailable",
        "doppler_auth_denied",
        "doppler_rate_limited",
        "doppler_response_unverified",
        "dumpability",
        "duplicate_json",
        "file_mode_untrusted",
        "helper_identity",
        "identity_changed",
        "invalid_json",
        "memory_limit",
        "native_exit",
        "native_failed",
        "native_os_failure",
        "native_output_bound",
        "native_timeout",
        "operation_denied",
        "operation_lock_untrusted",
        "operation_unknown",
        "package_digest",
        "package_files",
        "package_loader",
        "package_schema",
        "path_denied",
        "peer_denied",
        "pin_changed",
        "pipe_bound",
        "pipe_timeout",
        "policy_fields",
        "policy_values",
        "release_changed",
        "release_link_untrusted",
        "release_path_untrusted",
        "request_bound",
        "request_denied",
        "request_fields",
        "request_id_invalid",
        "required_path_inaccessible",
        "required_path_missing",
        "response_bound",
        "response_unverified",
        "restart_not_enabled",
        "restart_unverified",
        "root_directory_untrusted",
        "root_file_changed",
        "root_file_untrusted",
        "runtime_file_type",
        "runtime_link_untrusted",
        "runtime_mutable",
        "service_denied",
        "service_unhealthy",
        "socket_type",
        "socket_untrusted",
        "state_unverified",
        "sudo_identity",
        "sudo_privilege_transition_unavailable",
        "swap_limit",
        "tls_key_logging_denied",
        "token_expired",
        "unit_dropins_changed",
        "unit_scope",
        "unverified_exception",
        "worker_cleanup_unverified",
        "worker_identity",
        "audit_unit_wait_unknown",
        "audit_result_untrusted",
        "audit_result_missing",
        "audit_request_replayed",
        "audit_busy",
        "audit_storage_bound",
        "audit_unit_failed",
    )
)
DIAGNOSTIC_FIELDS = frozenset(
    (
        "status",
        "stage",
        "code",
        "rc",
        "operation",
        "request_id",
        "service",
        "operation_may_have_completed",
        "cleanup_unverified",
        "automatic_retry",
    )
)


class Denied(ValueError):
    """Fixed diagnostic codes; never stringify external exceptions."""

    def __init__(self, code, *, rc=None, diagnostic=None):
        super().__init__(code)
        self.rc = rc
        self.diagnostic = diagnostic


def diagnostic(stage, error, *, req=None, rc=None, committed=False):
    """Project fixed values only. Unknown/native text never crosses the boundary."""
    require(stage in DIAGNOSTIC_STAGES, "diagnostic_unverified")
    if not (
        type(req) is dict
        and set(req) == {"operation", "request_id"}
        and req["operation"] in ("inspect", "restart", "history_audit")
        and type(req["request_id"]) is str
        and re.fullmatch("[a-f0-9]{32}", req["request_id"])
    ):
        req = None
    existing = getattr(error, "diagnostic", None) if type(error) is Denied else None
    if existing is not None:
        return validate_diagnostic(existing, req=req, allow_unbound=True)
    code = "unverified_exception"
    if (
        type(error) is Denied
        and len(error.args) == 1
        and type(error.args[0]) is str
        and error.args[0] in DIAGNOSTIC_CODES
    ):
        code = error.args[0]
        rc = error.rc if rc is None else rc
    elif isinstance(error, OSError):
        code = {2: "required_path_missing", 13: "required_path_inaccessible"}.get(
            error.errno, "native_os_failure"
        )
    return {
        "status": "blocked",
        "stage": stage,
        "code": code,
        "rc": rc if type(rc) is int and -64 <= rc <= 255 else None,
        "operation": req["operation"] if req else None,
        "request_id": req["request_id"] if req else None,
        "service": SERVICE,
        "operation_may_have_completed": bool(committed),
        "cleanup_unverified": False,
        "automatic_retry": False,
    }


def validate_diagnostic(value, *, req=None, allow_unbound=False):
    require(
        type(value) is dict
        and set(value) == DIAGNOSTIC_FIELDS
        and value["status"] == "blocked"
        and type(value["stage"]) is str
        and value["stage"] in DIAGNOSTIC_STAGES
        and type(value["code"]) is str
        and value["code"] in DIAGNOSTIC_CODES
        and value["service"] == SERVICE
        and (value["rc"] is None or type(value["rc"]) is int and -64 <= value["rc"] <= 255)
        and type(value["operation_may_have_completed"]) is bool
        and type(value["cleanup_unverified"]) is bool
        and value["automatic_retry"] is False,
        "diagnostic_unverified",
    )
    unbound = value["operation"] is None and value["request_id"] is None
    require(
        unbound
        and allow_unbound
        or value["operation"] in ("inspect", "restart", "history_audit")
        and type(value["request_id"]) is str
        and re.fullmatch("[a-f0-9]{32}", value["request_id"]),
        "diagnostic_unverified",
    )
    if req is not None:
        require(
            unbound
            and allow_unbound
            or (value["operation"], value["request_id"]) == (req["operation"], req["request_id"]),
            "diagnostic_unverified",
        )
    result = dict(value)
    if req is not None:
        result.update(operation=req["operation"], request_id=req["request_id"])
    return result


def deny_diagnostic(value):
    raise Denied(value["code"], rc=value["rc"], diagnostic=value)


def require(ok, code):
    if not ok:
        raise Denied(code)


def wipe(value):
    if isinstance(value, bytearray):
        value[:] = b"\0" * len(value)


def valid_service_token(value):
    # Official token-type syntax only; not proof of project/config/RO scope.
    return (
        type(value) in (bytes, bytearray) and re.fullmatch(SERVICE_TOKEN_PATTERN, value) is not None
    )


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(Denied("invalid_json")),
    )


def request(raw):
    require(type(raw) is bytes and 0 < len(raw) <= 192, "request_bound")
    try:
        d = strict_json(raw)
        require(type(d) is dict and set(d) == {"operation", "request_id"}, "request_fields")
        require(d["operation"] in ("inspect", "restart", "history_audit"), "operation_denied")
        require(
            type(d["request_id"]) is str and re.fullmatch("[a-f0-9]{32}", d["request_id"]),
            "request_id_invalid",
        )
        return d
    except Exception:  # noqa: BLE001 - external errors may contain secrets.
        raise Denied("request_denied") from None


def root_dir(path, mode=None):
    """Verify the complete absolute ancestor chain, without following symlinks."""
    path = Path(path)
    require(path.is_absolute(), "path_denied")
    for p in reversed((path, *path.parents)):
        s = p.lstat()
        require(
            stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
            "root_directory_untrusted",
        )
    if mode is not None:
        require(stat.S_IMODE(path.lstat().st_mode) == mode, "directory_mode_untrusted")


def read_root(path, *, limit=131072, sha=None, mode=None):
    path = Path(path)
    root_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == 0
            and before.st_nlink == 1
            and not before.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
            and 0 <= before.st_size <= limit,
            "root_file_untrusted",
        )
        require(mode is None or stat.S_IMODE(before.st_mode) == mode, "file_mode_untrusted")
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(16384, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)
        require(
            len(data) == before.st_size
            and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
            "root_file_changed",
        )
        require(sha is None or hashlib.sha256(data).hexdigest() == sha, "pin_changed")
        return bytes(data)
    finally:
        os.close(fd)


PACKAGE_FILES = {
    "ops_entry.py",
    "broker_ops_policy.py",
    "history_audit_protocol.py",
    "history_audit_reader.py",
    "ops_history_projection.py",
}
PACKAGE_UNITS = {"api-quota-broker-ops@.service", "api-quota-broker-history-audit@.service"}


def package():
    manifest = strict_json(read_root(BASE / "manifest.json", mode=0o644))
    require(
        set(manifest) == {"files", "units", "schema"} and manifest["schema"] == 2, "package_schema"
    )
    require(
        set(manifest["files"]) == PACKAGE_FILES and set(manifest["units"]) == PACKAGE_UNITS,
        "package_files",
    )
    for name, digest in manifest["files"].items():
        require(type(digest) is str and re.fullmatch("[a-f0-9]{64}", digest), "package_digest")
        read_root(BASE / name, sha=digest, mode=0o644)
    for name, digest in manifest["units"].items():
        require(type(digest) is str and re.fullmatch("[a-f0-9]{64}", digest), "package_digest")
        read_root(Path("/etc/systemd/system") / name, sha=digest, mode=0o644)
    return load_public_module("broker_ops_policy.py", "aqb_fixed_ops_policy")


def load_public_module(name, label):
    require(name in PACKAGE_FILES, "package_files")
    spec = importlib.util.spec_from_file_location(label, BASE / name)
    require(spec is not None and spec.loader is not None, "package_loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def history_protocol():
    package()
    return load_public_module("history_audit_protocol.py", "aqb_history_protocol")


def prevent_process_dumps():
    """Re-establish process protection after exec/PAM, before any secret input."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # noqa: BLE001 - only a fixed code, never syscall text.
        raise Denied("core_limit_set_failed") from None
    try:
        limits = resource.getrlimit(resource.RLIMIT_CORE)
    except Exception:  # noqa: BLE001
        raise Denied("core_limit_query_failed") from None
    require(limits == (0, 0), "core_limit")
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0, "dumpability")


def memory_guard(*, bootstrap=False):
    prevent_process_dumps()
    cgroup = Path("/proc/self/cgroup").read_text().strip()
    prefix = "0::/system.slice/"
    require(cgroup.startswith(prefix), "unit_scope")
    name = cgroup[len(prefix) :]
    allowed = (
        (name == BOOT_UNIT)
        if bootstrap
        else bool(re.fullmatch(r"api-quota-broker-ops@[^/]+\.service", name))
    )
    require(allowed, "unit_scope")
    p = Path("/sys/fs/cgroup/system.slice") / name
    require((p / "memory.swap.max").read_text().strip() == "0", "swap_limit")
    require((p / "memory.max").read_text().strip() == "134217728", "memory_limit")
    libc = ctypes.CDLL(None, use_errno=True)
    # sudo needs setuid; reject accidental systemd hardening that disables it.
    require(libc.prctl(39, 0, 0, 0, 0) == 0, "sudo_privilege_transition_unavailable")


def native_result(argv, *, data=None, limit=16384, timeout=10):
    """Bounded fixed-command capture; callers validate bytes before projection."""
    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(ENV),
        close_fds=True,
    )
    try:
        out, _ = p.communicate(data, timeout=timeout)
        require(len(out) <= limit, "native_output_bound")
        return p.returncode, out
    except BaseException as error:
        p.kill()
        p.communicate()
        if not isinstance(error, Exception):
            raise
        if type(error) is Denied:
            raise
        if isinstance(error, subprocess.TimeoutExpired):
            raise Denied("native_timeout") from None
        raise Denied("native_failed") from None


def native(argv, *, data=None, limit=16384, timeout=10):
    rc, out = native_result(argv, data=data, limit=limit, timeout=timeout)
    if rc != 0:
        raise Denied("native_exit", rc=rc)
    return out


def service_state(name=SERVICE):
    require(name in (SERVICE, "orderflow.service"), "service_denied")
    raw = native(("/usr/bin/systemctl", "show", name, "--property=" + PROPS))
    d = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
    require(
        set(d) == set(PROPS.split(","))
        and d["ActiveState"] == "active"
        and d["SubState"] == "running"
        and d["MainPID"].isdigit()
        and int(d["MainPID"]) > 0
        and d["NRestarts"].isdigit()
        and d["ExecMainStartTimestampMonotonic"].isdigit(),
        "service_unhealthy",
    )
    return d


def broker_pins(config_sha):
    require(os.readlink("/opt/api-quota-broker/current") == RELEASE, "release_changed")
    root_dir("/opt/api-quota-broker")
    link = Path("/opt/api-quota-broker/current").lstat()
    require(link.st_uid == 0 and stat.S_ISLNK(link.st_mode), "release_link_untrusted")
    release = Path("/opt/api-quota-broker") / RELEASE
    manifest = strict_json(read_root(release / "release-manifest.json", sha=MANIFEST_SHA))
    for item in manifest["files"]:
        relative = item["path"]
        require(
            type(relative) is str
            and not relative.startswith("/")
            and all(
                re.fullmatch("[A-Za-z0-9_.-]+", part) and part not in (".", "..")
                for part in relative.split("/")
            ),
            "release_path_untrusted",
        )
        read_root(release / relative, sha=item["sha256"], limit=16 * 1024 * 1024)
    # Runtime is installed separately: forbid any user-writable file/dir or
    # escaping symlink. Internal venv/bin symlinks may only resolve inside the
    # fixed runtime or to the root-protected native Python interpreter.
    runtime = release / "runtime"
    root_dir(runtime)
    for parent, dirs, files in os.walk(runtime, followlinks=False):
        for n in dirs + files:
            path = Path(parent) / n
            s = path.lstat()
            require(
                s.st_uid == 0 and (stat.S_ISLNK(s.st_mode) or not s.st_mode & 0o022),
                "runtime_mutable",
            )
            if stat.S_ISLNK(s.st_mode):
                target = path.resolve(strict=True)
                require(
                    target.is_relative_to(runtime) or str(target) == "/usr/bin/python3.14",
                    "runtime_link_untrusted",
                )
            else:
                require(stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode), "runtime_file_type")
    read_root("/etc/systemd/system/" + SERVICE, sha=UNIT_SHA)
    drops = native(("/usr/bin/systemctl", "show", SERVICE, "--property=DropInPaths", "--value"))
    require(drops.strip() == b"", "unit_dropins_changed")
    read_root("/etc/api-quota-broker/gateway.json", sha=config_sha)


def runtime_policy():
    d = strict_json(read_root(CONFIG / "policy.json", mode=0o644))
    require(
        set(d)
        == {
            "expires_at",
            "config_sha256",
            "ops_uid",
            "ops_gid",
            "peer_uid",
            "schema",
            "scope_verification",
            "enabled",
            "issued_at",
        },
        "policy_fields",
    )
    require(
        d["schema"] == 1
        and d["peer_uid"] == 1000
        and 0 < d["ops_uid"] < 1000
        and 0 < d["ops_gid"] < 1000
        and re.fullmatch("[a-f0-9]{64}", d["config_sha256"])
        and d["scope_verification"] == "human_dashboard_attestation_only"
        and type(d["enabled"]) is bool,
        "policy_values",
    )
    expiry = datetime.fromisoformat(d["expires_at"])
    issued = datetime.fromisoformat(d["issued_at"])
    require(
        expiry.tzinfo is not None
        and issued.tzinfo is not None
        and 0 < (expiry - issued).total_seconds() <= 30 * 86400
        and issued <= datetime.now(UTC) < expiry,
        "token_expired",
    )
    a = pwd.getpwnam(ACCOUNT)
    require(
        a.pw_uid == d["ops_uid"]
        and a.pw_gid == d["ops_gid"]
        and a.pw_shell == "/usr/sbin/nologin"
        and a.pw_dir == "/nonexistent"
        and set(os.getgrouplist(ACCOUNT, a.pw_gid)) == {a.pw_gid},
        "identity_changed",
    )
    return d


def token_read():
    # Fixed systemd-provided credential path, never a caller-supplied env path.
    cgroup = Path("/proc/self/cgroup").read_text().strip().split("/")[-1]
    require(re.fullmatch(r"api-quota-broker-ops@[^/]+\.service", cgroup), "credential_scope")
    p = Path("/run/credentials") / cgroup / "ops_doppler"
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    raw = None
    try:
        info = os.fstat(fd)
        acl = None
        if info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o440:
            acl = os.getxattr(fd, "system.posix_acl_access")
        credential_metadata(info, os.getuid(), acl)
        raw = bytearray(os.read(fd, 301))
        require(valid_service_token(raw), "credential_format")
        return raw
    except BaseException:
        wipe(raw)
        raise
    finally:
        os.close(fd)


def credential_metadata(info, uid, acl=None):
    """Only service-owned 0400 or exact root-owned named-user read ACL.

    Modern systemd may use root:root 0440 + ACL. The group mode bits are
    the ACL mask, not permission for the root group; validate every entry.
    """
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 300,
        "credential_metadata",
    )
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == uid and mode == 0o400:
        return
    require(
        info.st_uid == info.st_gid == 0
        and mode == 0o440
        and type(acl) is bytes
        and len(acl) == 44
        and struct.unpack("<I", acl[:4])[0] == 2,
        "credential_acl",
    )
    entries = [struct.unpack("<HHI", acl[i : i + 8]) for i in range(4, len(acl), 8)]
    undefined = 0xFFFFFFFF
    require(
        len(set(entries)) == 5
        and set(entries)
        == {
            (1, 4, undefined),
            (2, 4, uid),
            (4, 0, undefined),
            (16, 4, undefined),
            (32, 0, undefined),
        },
        "credential_acl",
    )


def doppler_transport(path, headers):
    # stdlib HTTPSConnection ignores proxy environment and never follows 3xx.
    # Avoid create_default_context's SSLKEYLOGFILE environment support.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile="/etc/ssl/certs/ca-certificates.crt")
    require(context.keylog_filename is None, "tls_key_logging_denied")
    conn = http.client.HTTPSConnection("api.doppler.com", timeout=8, context=context)
    try:
        require(
            path
            == "/v3/configs/config/secret?project=api-quota-broker-ops&config=dev&name=ASUS_BROKER_DEPLOY_PASSWORD",
            "doppler_endpoint",
        )
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read(8193)
        require(len(raw) <= 8192, "doppler_body_bound")
        return response.status, raw
    except Exception:  # noqa: BLE001 - external errors may contain secrets.
        raise Denied("doppler_unavailable") from None
    finally:
        conn.close()


def pipe_line(fd, *, limit=4096, seconds=10):
    end = time.monotonic() + seconds
    raw = bytearray()
    while len(raw) <= limit:
        remaining = end - time.monotonic()
        require(remaining > 0 and select.select([fd], [], [], remaining)[0], "pipe_timeout")
        part = os.read(fd, 1)
        if not part:
            return bytes(raw)
        raw.extend(part)
        if part == b"\n":
            return bytes(raw)
    raise Denied("pipe_bound")


def sudo_argv(probe=False):
    return ("/usr/bin/sudo", "-k", "-n" if probe else "-S", "-p", "", "--", CONTROL)


class SudoSession:
    def __init__(self):
        self.p = subprocess.Popen(
            sudo_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(ENV),
            close_fds=True,
        )

    def execute(self, password, req):
        self.p.stdin.write(memoryview(password))
        self.p.stdin.write(b"\n")
        self.p.stdin.flush()
        first = pipe_line(self.p.stdout.fileno(), limit=4096)
        if first != READY:
            tail, _ = self.p.communicate(timeout=5)
            if first and tail == b"":
                value = validate_diagnostic(strict_json(first), req=req, allow_unbound=True)
                require(self.p.returncode == 1, "diagnostic_unverified")
                deny_diagnostic(value)
            raise Denied("authentication_failed", rc=self.p.returncode)
        self.p.stdin.write(json.dumps(req, separators=(",", ":")).encode() + b"\n")
        self.p.stdin.close()
        self.p.stdin = None
        raw = pipe_line(
            self.p.stdout.fileno(),
            limit=32768 if req["operation"] == "history_audit" else 4096,
            seconds=135 if req["operation"] == "history_audit" else 20,
        )
        tail, _ = self.p.communicate(timeout=5)
        require(tail == b"", "operation_unknown")
        result = strict_json(raw)
        if (
            req["operation"] == "history_audit"
            and type(result) is dict
            and ("summary" in result or "check" in result)
        ):
            require(self.p.returncode == 0, "operation_unknown")
            try:
                return history_protocol().validate(result, req["request_id"])
            except (ValueError, TypeError, KeyError):
                raise Denied("audit_result_untrusted") from None
        if type(result) is dict and result.get("status") == "blocked":
            value = validate_diagnostic(result, req=req)
            require(self.p.returncode == 1, "diagnostic_unverified")
            deny_diagnostic(value)
        require(self.p.returncode == 0, "operation_unknown")
        require(
            type(result) is dict
            and result.get("status") == "passed"
            and result.get("operation") == req["operation"]
            and result.get("request_id") == req["request_id"]
            and result.get("service") == SERVICE,
            "operation_unknown",
        )
        # Never relay an unvalidated root subprocess output field.
        state = result.get("state")
        require(
            type(state) is dict
            and set(state) == set(PROPS.split(","))
            and state["ActiveState"] == "active"
            and state["SubState"] == "running"
            and all(
                type(state[n]) is str and state[n].isdigit()
                for n in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic")
            ),
            "state_unverified",
        )
        return {
            "status": "passed",
            "operation": req["operation"],
            "request_id": req["request_id"],
            "service": SERVICE,
            "state": state,
            "automatic_retry": False,
        }

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.communicate(timeout=5)


def sudo_probe():
    p = subprocess.run(
        sudo_argv(True),
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(ENV),
        timeout=8,
        check=False,
    )
    require(p.returncode == 1 and p.stdout == b"", "authentication_bypass_or_probe_failure")


def manage(
    req,
    load,
    transport,
    policy_module,
    *,
    guard=memory_guard,
    probe=sudo_probe,
    session_factory=SudoSession,
):
    token, password, session = None, None, None
    committed = False
    doppler_code = "doppler_unavailable"

    def checked_transport(path, headers):
        nonlocal doppler_code
        status, body = transport(path, headers)
        if status in (401, 403):
            doppler_code = "doppler_auth_denied"
        elif status == 429:
            doppler_code = "doppler_rate_limited"
        elif status != 200:
            doppler_code = "doppler_response_unverified"
        return status, body

    stage = "worker_request"
    try:
        request(json.dumps(req).encode())
        stage = "worker_guard"
        guard()
        stage = "worker_probe"
        probe()
        stage = "worker_credential"
        token = load()
        stage = "worker_doppler"
        password = policy_module.password_from_doppler(token, checked_transport)
        stage = "worker_session"
        session = session_factory()
        stage = "worker_operation"
        committed = True
        result = session.execute(password, req)
    except Exception as error:  # noqa: BLE001 - only safe projection is emitted.
        selected_error = Denied(doppler_code) if stage == "worker_doppler" else error
        result = diagnostic(stage, selected_error, req=req, committed=committed)
    finally:
        wipe(token)
        wipe(password)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                primary = result if result.get("status") == "blocked" else None
                result = diagnostic(
                    "worker_cleanup", Denied("worker_cleanup_unverified"), req=req, committed=True
                )
                # Keep the primary rejection without any raw cleanup exception.
                if primary is not None and set(primary) == DIAGNOSTIC_FIELDS:
                    result = dict(primary)
                    result["cleanup_unverified"] = True

    return result


def worker_connection(conn, *, prepare, run):
    req = None
    stage = "worker_peer"
    try:
        require(conn.family == socket.AF_UNIX and conn.type == socket.SOCK_STREAM, "socket_type")
        _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(uid == 1000, "peer_denied")
        stage = "worker_request"
        conn.settimeout(5)
        raw = bytearray()
        while len(raw) <= 192:
            part = conn.recv(193 - len(raw))
            if not part:
                break
            raw.extend(part)
        req = request(bytes(raw))
        stage = "worker_prepare"
        prepare()
        stage = "worker_operation"
        result = run(req)
    except Exception as error:  # noqa: BLE001
        result = diagnostic(stage, error, req=req)
    conn.sendall(json.dumps(result, separators=(",", ":")).encode() + b"\n")


def worker():
    policy = None

    def prepare():
        nonlocal policy
        stage = "worker_package"
        try:
            policy = package()
            stage = "worker_policy"
            d = runtime_policy()
            stage = "worker_identity"
            require(os.getuid() == d["ops_uid"] and os.geteuid() == os.getuid(), "worker_identity")
            stage = "worker_guard"
            memory_guard()
        except Exception as error:  # noqa: BLE001
            deny_diagnostic(diagnostic(stage, error))

    def execute_request(req):
        previous = signal.signal(
            signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Denied("pipe_timeout"))
        )
        signal.alarm(175 if req["operation"] == "history_audit" else 35)
        try:
            return manage(req, token_read, doppler_transport, policy)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    with socket.socket(fileno=os.dup(0)) as conn:
        worker_connection(
            conn,
            prepare=prepare,
            run=execute_request,
        )


def write_exclusive(path, raw, *, mode=0o600, uid=0, gid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def helper_operation(
    req, *, state=STATE, pins, inspect, restart, package_check=lambda: None, audit=None
):
    """Persist dispatch intent before action, preserve every claim on failure."""
    request(json.dumps(req).encode())
    package_check()
    pins()
    if req["operation"] == "inspect":
        return {
            "status": "passed",
            "operation": "inspect",
            "request_id": req["request_id"],
            "service": SERVICE,
            "state": inspect(),
            "automatic_retry": False,
        }
    if req["operation"] == "history_audit":
        require(audit is not None, "operation_denied")
        return audit(req)
    root_dir(state, mode=0o700)
    lockfd = os.open(state / "operation.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(lockfd)
        require(
            s.st_uid == 0
            and stat.S_ISREG(s.st_mode)
            and s.st_nlink == 1
            and stat.S_IMODE(s.st_mode) == 0o600,
            "operation_lock_untrusted",
        )
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(sum(1 for _ in state.iterdir()) <= 8190, "claim_storage_limit")
        claim = state / (req["request_id"] + ".claim.json")
        write_exclusive(
            claim,
            json.dumps(
                {"schema": 1, "operation": req["operation"], "dispatch_intent": True}
            ).encode()
            + b"\n",
        )
        before = inspect()
        restart()
        pins()
        after = inspect()
        require(
            int(after["ExecMainStartTimestampMonotonic"])
            > int(before["ExecMainStartTimestampMonotonic"]),
            "restart_unverified",
        )
        result = {
            "status": "passed",
            "operation": req["operation"],
            "request_id": req["request_id"],
            "service": SERVICE,
            "state": after,
            "automatic_retry": False,
        }
        write_exclusive(
            state / (req["request_id"] + ".result.json"),
            json.dumps(result, sort_keys=True).encode() + b"\n",
        )
        return result
    finally:
        os.close(lockfd)


def fixed_history_audit(
    req,
    *,
    state=STATE,
    run=native_result,
    read=read_root,
    writer=write_exclusive,
    root_check=root_dir,
    protocol=None,
    owner=0,
):
    """Only a nonce-bound installed static unit; no caller SQL/path/unit/env/argv."""
    request(json.dumps(req).encode())
    require(req["operation"] == "history_audit", "operation_denied")
    protocol = history_protocol() if protocol is None else protocol
    root_check(state, mode=0o700)
    directory = state / "history-audit"
    root_check(directory, mode=0o700)
    lockfd = os.open(state / "operation.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(lockfd)
        require(
            meta.st_uid == owner
            and stat.S_ISREG(meta.st_mode)
            and meta.st_nlink == 1
            and stat.S_IMODE(meta.st_mode) == 0o600,
            "operation_lock_untrusted",
        )
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return protocol.blocked("audit_busy", "result", req["request_id"])
        names = {p.name for p in directory.iterdir()}
        require(
            len(names) <= 1537 and sum(name.endswith(".claim.json") for name in names) < 512,
            "audit_storage_bound",
        )
        require(
            all(
                name == "reader.lock"
                or re.fullmatch(r"[a-f0-9]{32}\.(claim|reader|result)\.json", name)
                for name in names
            ),
            "audit_result_untrusted",
        )
        key = req["request_id"]
        require(
            key + ".claim.json" not in names and key + ".result.json" not in names,
            "audit_request_replayed",
        )
        unit = "api-quota-broker-history-audit@" + key + ".service"
        active = run(
            (
                "/usr/bin/systemctl",
                "list-units",
                "--no-legend",
                "--plain",
                "--state=active,activating,deactivating",
                "api-quota-broker-history-audit@*.service",
            ),
            timeout=8,
        )
        require(active[0] == 0 and active[1].strip() == b"", "audit_busy")
        info = run(
            (
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,ExecMainStartTimestampMonotonic,DropInPaths",
            ),
            timeout=8,
        )
        require(info[0] == 0, "audit_unit_failed")
        values = dict(line.split("=", 1) for line in info[1].decode().splitlines())
        require(
            values
            == {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "ExecMainStartTimestampMonotonic": "0",
                "DropInPaths": "",
            },
            "audit_request_replayed",
        )
        claim = {
            "schema": 1,
            "operation": "history_audit",
            "request_id": key,
            "dispatch_intent": True,
            "credential_auth_verified": True,
        }
        writer(
            directory / (key + ".claim.json"), json.dumps(claim, sort_keys=True).encode() + b"\n"
        )
        writer(directory / (key + ".result.json"), b"")
        try:
            status, _ = run(("/usr/bin/systemctl", "start", unit), timeout=100)
        except Denied as error:
            if error.args == ("native_timeout",):
                return protocol.blocked("audit_unit_wait_unknown", "result", key)
            raise
        raw = read(directory / (key + ".result.json"), mode=0o600, limit=32768)
        if not raw:
            return protocol.blocked(
                "audit_unit_failed" if status else "audit_result_missing", "result", key
            )
        try:
            value = protocol.validate(strict_json(raw), key)
        except (ValueError, TypeError, KeyError):
            return protocol.blocked("audit_result_untrusted", "result", key)
        require(status == (0 if value["status"] == "passed" else 1), "audit_unit_failed")
        return value
    finally:
        os.close(lockfd)


def helper():
    req = None
    stage = "helper_identity"
    try:
        require(
            len(sys.argv) == 2
            and os.geteuid() == 0
            and os.uname().nodename == "asus-ubuntu2604-server",
            "helper_identity",
        )
        # Do this before package/policy parsing and before READY/stdin input.
        stage = "helper_guard"
        prevent_process_dumps()
        stage = "helper_package"
        package()
        stage = "helper_policy"
        d = runtime_policy()
        stage = "helper_sudo_identity"
        require(os.environ.get("SUDO_UID") == str(d["ops_uid"]), "sudo_identity")
        stage = "helper_guard"
        bootstrap = Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + BOOT_UNIT
        memory_guard(bootstrap=bootstrap)
        stage = "helper_pins"
        broker_pins(d["config_sha256"])
        os.write(1, READY)
        stage = "helper_request"
        raw = pipe_line(0, limit=192)
        if raw == b"":
            return 0
        req = request(raw)
        require(req["operation"] != "restart", "restart_not_enabled")
        stage = "helper_operation"
        result = helper_operation(
            req,
            pins=lambda: broker_pins(d["config_sha256"]),
            inspect=service_state,
            restart=lambda: native(("/usr/bin/systemctl", "restart", SERVICE), timeout=20),
            package_check=package,
            audit=fixed_history_audit,
        )
        os.write(1, json.dumps(result, sort_keys=True).encode() + b"\n")
        return 0
    except BaseException as error:  # noqa: BLE001 - includes interruption.
        result = diagnostic(
            stage,
            error,
            req=req,
            committed=stage == "helper_operation" and req["operation"] == "restart",
        )
        if result["rc"] is None:
            result["rc"] = 1
        os.write(1, json.dumps(result, sort_keys=True).encode() + b"\n")
        return 1


def client(operation):
    req = (
        {"operation": operation, "request_id": uuid.uuid4().hex}
        if operation in ("inspect", "restart", "history_audit")
        else None
    )
    stage = "client_identity"
    try:
        require(
            operation in ("inspect", "restart", "history_audit") and os.getuid() == 1000,
            "client_denied",
        )
        stage = "client_socket"
        root_dir(Path(SOCKET).parent)
        s = Path(SOCKET).lstat()
        require(
            stat.S_ISSOCK(s.st_mode)
            and s.st_uid == 0
            and s.st_gid == 1000
            and stat.S_IMODE(s.st_mode) == 0o660,
            "socket_untrusted",
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(210 if operation == "history_audit" else 40)
            stage = "client_connect"
            conn.connect(SOCKET)
            conn.sendall(json.dumps(req, separators=(",", ":")).encode())
            conn.shutdown(socket.SHUT_WR)
            stage = "client_response"
            limit = 32768 if operation == "history_audit" else 4096
            raw = bytearray()
            while len(raw) <= limit:
                part = conn.recv(limit + 1 - len(raw))
                if not part:
                    break
                raw.extend(part)
            require(len(raw) <= limit, "response_bound")
            result = strict_json(raw)
            require(
                type(result) is dict and result.get("status") in ("passed", "blocked"),
                "response_unverified",
            )
            if operation == "history_audit" and ("summary" in result or "check" in result):
                try:
                    value = history_protocol().validate(result, req["request_id"])
                except (ValueError, TypeError, KeyError):
                    raise Denied("audit_result_untrusted") from None
                print(json.dumps(value, sort_keys=True))
                return 0 if value["status"] == "passed" else 1
            if result["status"] == "passed":
                require(
                    set(result)
                    == {"status", "operation", "request_id", "service", "state", "automatic_retry"}
                    and result["operation"] == operation
                    and result["request_id"] == req["request_id"]
                    and result["service"] == SERVICE
                    and result["automatic_retry"] is False,
                    "response_unverified",
                )
            else:
                result = validate_diagnostic(result, req=req, allow_unbound=True)
            if result["status"] == "passed":
                state = result["state"]
                require(
                    type(state) is dict
                    and set(state) == set(PROPS.split(","))
                    and state["ActiveState"] == "active"
                    and state["SubState"] == "running"
                    and all(
                        type(state[n]) is str and len(state[n]) <= 20 and state[n].isdigit()
                        for n in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic")
                    )
                    and int(state["MainPID"]) > 0,
                    "state_unverified",
                )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "passed" else 1
    except BaseException as error:  # noqa: BLE001 - includes interruption without raw output.
        print(json.dumps(diagnostic(stage, error, req=req), sort_keys=True))
        return 1


def plan():
    return {
        "mode": "asus_ops_socket_candidate",
        "selected": True,
        "apply": False,
        "operations": ["inspect", "history_audit"],
        "restart_enabled": False,
        "history_audit_private_sql_or_path_arguments": False,
        "token_days_max": 30,
        "activation": "Accept=yes; one disposable worker per connection",
        "native_pam_verified": False,
        "provider_calls": 0,
        "secret_reads": 0,
        "source_password_file_used": False,
        "automatic_retry": False,
    }


def main():
    try:
        if len(sys.argv) == 1:
            print(json.dumps(plan(), sort_keys=True))
            return 0
        if sys.argv[1:] == ["worker"]:
            worker()
        elif sys.argv[1:] == ["helper"]:
            return helper()
        elif len(sys.argv) == 3 and sys.argv[1] == "client":
            return client(sys.argv[2])
        else:
            raise Denied("arguments_denied")
        return 0
    except BaseException as error:  # noqa: BLE001 - includes interruption.
        result = diagnostic("entry", error)
        os.write(1, json.dumps(result, sort_keys=True).encode() + b"\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
