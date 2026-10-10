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


class Denied(ValueError):
    """Fixed diagnostic codes; never stringify external exceptions."""


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
        require(d["operation"] in ("inspect", "restart"), "operation_denied")
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


def package():
    manifest = strict_json(read_root(BASE / "manifest.json", mode=0o644))
    require(set(manifest) == {"files", "schema"} and manifest["schema"] == 1, "package_schema")
    require(set(manifest["files"]) == {"ops_entry.py", "broker_ops_policy.py"}, "package_files")
    for n, digest in manifest["files"].items():
        require(re.fullmatch("[a-f0-9]{64}", digest), "package_digest")
        read_root(BASE / n, sha=digest, mode=0o644)
    source = BASE / "broker_ops_policy.py"
    spec = importlib.util.spec_from_file_location("aqb_fixed_ops_policy", source)
    require(spec is not None and spec.loader is not None, "package_loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def memory_guard(*, bootstrap=False):
    require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0), "core_limit")
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
    require(libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0, "dumpability")
    # sudo needs setuid; reject accidental systemd hardening that disables it.
    require(libc.prctl(39, 0, 0, 0, 0) == 0, "sudo_privilege_transition_unavailable")


def native(argv, *, data=None, limit=16384, timeout=10):
    """Caller constructs fixed commands. Never forward external stderr/argv."""
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
        require(p.returncode == 0 and len(out) <= limit, "native_failed")
        return out
    except BaseException as error:  # cleanup also covers interruption.
        p.kill()
        p.communicate()
        if not isinstance(error, Exception):
            raise
        raise Denied("native_failed") from None


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
        require(pipe_line(self.p.stdout.fileno(), limit=64) == READY, "authentication_failed")
        self.p.stdin.write(json.dumps(req, separators=(",", ":")).encode() + b"\n")
        self.p.stdin.close()
        self.p.stdin = None
        raw = pipe_line(self.p.stdout.fileno(), limit=4096, seconds=20)
        tail, _ = self.p.communicate(timeout=5)
        require(self.p.returncode == 0 and tail == b"", "operation_unknown")
        result = strict_json(raw)
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
    try:
        request(json.dumps(req).encode())
        guard()
        probe()
        token = load()
        password = policy_module.password_from_doppler(token, transport)
        session = session_factory()
        # Any failure after this call can include dispatch. Never retry.
        committed = True
        result = session.execute(password, req)
    except Exception:  # noqa: BLE001 - external errors may contain secrets.
        result = {
            "status": "blocked",
            "code": "authentication_or_operation_unverified",
            "operation_may_have_completed": committed,
            "automatic_retry": False,
        }
    finally:
        wipe(token)
        wipe(password)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - external errors may contain secrets.
                result = {
                    "status": "blocked",
                    "code": "worker_cleanup_unverified",
                    "operation_may_have_completed": True,
                    "automatic_retry": False,
                }
    return result


def worker_connection(conn, *, prepare, run):
    try:
        require(conn.family == socket.AF_UNIX and conn.type == socket.SOCK_STREAM, "socket_type")
        _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(uid == 1000, "peer_denied")
        conn.settimeout(5)
        raw = bytearray()
        while len(raw) <= 192:
            part = conn.recv(193 - len(raw))
            if not part:
                break
            raw.extend(part)
            # Require client SHUT_WR; reject concatenated/replayed requests.
        req = request(bytes(raw))
        prepare()
        result = run(req)
    except Exception:  # noqa: BLE001 - external errors may contain secrets.
        result = {
            "status": "blocked",
            "code": "request_or_worker_unverified",
            "automatic_retry": False,
        }
    conn.sendall(json.dumps(result, separators=(",", ":")).encode() + b"\n")


def worker():
    policy = package()

    def prepare():
        d = runtime_policy()
        require(os.getuid() == d["ops_uid"] and os.geteuid() == os.getuid(), "worker_identity")
        memory_guard()

    with socket.socket(fileno=os.dup(0)) as conn:
        worker_connection(
            conn,
            prepare=prepare,
            run=lambda req: manage(req, token_read, doppler_transport, policy),
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


def helper_operation(req, *, state=STATE, pins, inspect, restart, package_check=lambda: None):
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


def helper():
    require(
        len(sys.argv) == 2
        and os.geteuid() == 0
        and os.uname().nodename == "asus-ubuntu2604-server",
        "helper_identity",
    )
    package()
    d = runtime_policy()
    require(os.environ.get("SUDO_UID") == str(d["ops_uid"]), "sudo_identity")
    # Native wrong-password/bypass validation runs only inside the root-owned
    # bounded bootstrap unit; normal operations run in the socket instance.
    bootstrap = Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + BOOT_UNIT
    memory_guard(bootstrap=bootstrap)
    # Verify pins before READY or input. EOF is an effect-free auth probe.
    broker_pins(d["config_sha256"])
    os.write(1, READY)
    raw = pipe_line(0, limit=192)
    if raw == b"":
        return
    req = request(raw)
    require(req["operation"] != "restart" or d["enabled"], "restart_not_enabled")
    result = helper_operation(
        req,
        pins=lambda: broker_pins(d["config_sha256"]),
        inspect=service_state,
        restart=lambda: native(("/usr/bin/systemctl", "restart", SERVICE), timeout=20),
        package_check=package,
    )
    os.write(1, json.dumps(result, sort_keys=True).encode() + b"\n")


def client(operation):
    require(operation in ("inspect", "restart") and os.getuid() == 1000, "client_denied")
    root_dir(Path(SOCKET).parent)
    s = Path(SOCKET).lstat()
    require(
        stat.S_ISSOCK(s.st_mode)
        and s.st_uid == 0
        and s.st_gid == 1000
        and stat.S_IMODE(s.st_mode) == 0o660,
        "socket_untrusted",
    )
    req = {"operation": operation, "request_id": uuid.uuid4().hex}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(40)
        conn.connect(SOCKET)
        conn.sendall(json.dumps(req, separators=(",", ":")).encode())
        conn.shutdown(socket.SHUT_WR)
        raw = bytearray()
        while len(raw) <= 4096:
            part = conn.recv(4097 - len(raw))
            if not part:
                break
            raw.extend(part)
        require(len(raw) <= 4096, "response_bound")
        result = strict_json(raw)
        require(
            type(result) is dict and result.get("status") in ("passed", "blocked"),
            "response_unverified",
        )
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
            result = {
                "status": "blocked",
                "code": "operation_unverified",
                "request_id": req["request_id"],
                "automatic_retry": False,
            }
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "passed" else 1


def plan():
    return {
        "mode": "asus_ops_socket_candidate",
        "selected": True,
        "apply": False,
        "operations": ["inspect", "restart"],
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
            helper()
        elif len(sys.argv) == 3 and sys.argv[1] == "client":
            return client(sys.argv[2])
        else:
            raise Denied("arguments_denied")
        return 0
    except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
        # No raw native/HTTP/password exception messages, including CLI args.
        os.write(1, b'{"status":"blocked","code":"entry_unverified","automatic_retry":false}\n')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
