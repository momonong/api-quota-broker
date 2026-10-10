"""One-use management-side transport for the already sealed ASUS bootstrap.

Run only after a human confirms the dedicated secret was saved. No secret is
accepted on the command line. The remote adapter runs as morris, never root;
only the exact pinned bootstrap's original root command is passed to sudo.
"""

import ctypes
import hashlib
import json
import os
import pwd
import resource
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

HOST = "asus-ubuntu2604-server"
PROJECT = "asus-maintenance-bootstrap"
KEY = "ASUS_MORRIS_SUDO_PASSWORD"
PACKET = "/var/tmp/api-quota-broker-maintenance-review-2026-10-09-r2"
TAR_SHA = "b131507c3e980b66591fe4d4582590e9441269861cd0e8f22020c0db0afbac12"
WRAPPER_SHA = "ec4cdb4d9d709e1b8c91403907d75db3d27497f1f501a3dab27cf297ee1dfb9e"
PROMPT = b"AQB_SUDO_R1:morris:"
PROMPT_FORMAT = "AQB_SUDO_R1:%p:"
PROMPTS = (PROMPT, b"[sudo: " + PROMPT + b"] Password: ")
MAX_SECRET = 1024
READY = {"kind": "ready", "host": HOST, "principal": "morris", "wrapper_sha": WRAPPER_SHA}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}


class Denied(ValueError):
    pass


def need(condition):
    if not condition:
        raise Denied("transport_gate")


STAGES = {
    "local_guard",
    "ssh_connection",
    "host_gate",
    "prompt_gate",
    "secret_name_check",
    "doppler_fetch",
    "input_write",
    "sudo_auth",
    "command_dispatch",
    "result_framing",
}
CODES = {
    "gate_rejected",
    "timeout",
    "eof",
    "output_bound",
    "prompt_unrecognized",
    "second_prompt",
    "cli_exit",
    "missing_name",
    "invalid_secret_shape",
    "io_error",
    "invalid_frame",
    "child_exit",
}


class TransportFailure(Denied):
    def __init__(self, stage, code, *, rc=None, timeout=False, possible_dispatch=True):
        need(stage in STAGES and code in CODES)
        need(rc is None or type(rc) is int)
        self.diagnostic = {
            "stage": stage,
            "code": code,
            "rc": rc,
            "timeout": bool(timeout),
            "possible_dispatch": bool(possible_dispatch),
        }
        super().__init__("classified_transport_failure")


def failure(error, stage, possible_dispatch=True, rc=None):
    if isinstance(error, TransportFailure):
        diag = error.diagnostic
    else:
        code = (
            "timeout"
            if isinstance(error, subprocess.TimeoutExpired)
            else ("io_error" if isinstance(error, OSError) else "gate_rejected")
        )
        diag = TransportFailure(
            stage, code, rc=rc, timeout=code == "timeout", possible_dispatch=possible_dispatch
        ).diagnostic
    return {"state": "unknown" if diag["possible_dispatch"] else "blocked", "diagnostic": diag}


def safe_diagnostic(obj):
    need(type(obj) is dict and set(obj) == {"stage", "code", "rc", "timeout", "possible_dispatch"})
    need(obj["stage"] in STAGES and obj["code"] in CODES)
    need(obj["rc"] is None or type(obj["rc"]) is int)
    need(type(obj["timeout"]) is bool and type(obj["possible_dispatch"]) is bool)
    return dict(obj)


def guard():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    need(libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(3, 0, 0, 0, 0) == 0)
    group = Path("/proc/self/cgroup").read_text().strip()
    need(group.startswith("0::/") and "\n" not in group)
    relative = group[4:]
    need(".." not in Path(relative).parts)
    root = Path("/sys/fs/cgroup") / relative
    need((root / "memory.swap.max").read_text().strip() == "0")
    need((root / "memory.swap.current").read_text().strip() == "0")
    need(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))


def read_pinned(path, expected, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        need(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_uid == 1000)
        need(stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_size <= maximum)
        raw = stream.read(maximum + 1)
        need(len(raw) == meta.st_size and hashlib.sha256(raw).hexdigest() == expected)
        return raw


def target_command():
    need(os.getuid() == os.geteuid() == 1000)
    need(pwd.getpwuid(os.geteuid()).pw_name == "morris" and os.uname().nodename == HOST)
    directory = Path(PACKET).lstat()
    need(stat.S_ISDIR(directory.st_mode) and directory.st_uid == 1000)
    need(stat.S_IMODE(directory.st_mode) == 0o700)
    read_pinned(PACKET + ".tar", TAR_SHA, 16 * 1024 * 1024)
    wrapper = read_pinned(PACKET + "/bootstrap-once.sh", WRAPPER_SHA, 32768)
    return sudo_command(wrapper)


def sudo_command(wrapper):
    need(hashlib.sha256(wrapper).hexdigest() == WRAPPER_SHA)
    prefix = '#!/bin/sh\nset -eu\ntest "$#" -eq 0\n'
    text = wrapper.decode("utf-8")
    need(text.startswith(prefix))
    parts = shlex.split(text[len(prefix) :])
    need(parts[:8] == ["exec", "sudo", "/usr/bin/python3.14", "-I", "-B", "-S", "-c", parts[-1]])
    need(len(parts) == 8)
    return ["/usr/bin/sudo", "-S", "-k", "-p", PROMPT_FORMAT, "--", *parts[2:]]


def erase(value):
    value[:] = b"\0" * len(value)


def secret_valid(secret):
    need(0 < len(secret) <= MAX_SECRET)
    need(not any(c in secret for c in (0, 10, 13)))


def read_line(stream, bound, seconds, stage="result_framing", possible_dispatch=True):
    deadline = time.monotonic() + seconds
    result = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(left):
                raise TransportFailure(
                    stage, "timeout", timeout=True, possible_dispatch=possible_dispatch
                )
            char = os.read(stream.fileno(), 1)
            if not char:
                raise TransportFailure(stage, "eof", possible_dispatch=possible_dispatch)
            if char == b"\n":
                return result
            result.extend(char)
            if len(result) > bound:
                raise TransportFailure(stage, "output_bound", possible_dispatch=possible_dispatch)


def terminate(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=5)


def capture(child, limit, seconds, password_supplier=None):
    """Bound both streams; send at most one line only for the exact sudo prompt."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    submitted = 0
    prompted = False
    complete = False
    secret = bytearray()
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                selector.register(getattr(child, name), selectors.EVENT_READ, name)
            while selector.get_map():
                left = deadline - time.monotonic()
                events = selector.select(max(0, left)) if left > 0 else []
                if not events:
                    raise TransportFailure(
                        "sudo_auth" if password_supplier else "doppler_fetch",
                        "timeout",
                        timeout=True,
                        possible_dispatch=bool(submitted)
                        or (password_supplier is not None and not prompted),
                    )
                for event, _ in events:
                    raw = os.read(event.fileobj.fileno(), 4096)
                    if not raw:
                        selector.unregister(event.fileobj)
                        continue
                    buf = buffers[event.data]
                    buf.extend(raw)
                    if len(buf) > limit:
                        raise TransportFailure(
                            "result_framing",
                            "output_bound",
                            possible_dispatch=bool(submitted)
                            or (password_supplier is not None and not prompted),
                        )
                    if password_supplier is not None and event.data == "stderr" and not submitted:
                        # Refuse any unexpected pre-auth text, including a TTY requirement.
                        if not any(prompt.startswith(buf) for prompt in PROMPTS):
                            raise TransportFailure("prompt_gate", "prompt_unrecognized")
                        if bytes(buf) in PROMPTS:
                            prompted = True
                            secret = password_supplier()
                            secret_valid(secret)
                            submitted = 1
                            try:
                                child.stdin.write(secret)
                                child.stdin.write(b"\n")
                                child.stdin.flush()
                                child.stdin.close()
                            except OSError:
                                raise TransportFailure("input_write", "io_error") from None
                            erase(secret)
                    elif password_supplier is not None and submitted:
                        if bytes(buf).count(PROMPT) > 1:
                            raise TransportFailure(
                                "sudo_auth", "second_prompt", possible_dispatch=False
                            )
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        complete = True
        return child.returncode, buffers["stdout"], submitted
    finally:
        terminate(child)
        erase(buffers["stderr"])
        erase(secret)
        if not complete:
            erase(buffers["stdout"])


def bootstrap_projection(raw):
    obj = json.loads(raw)
    need(type(obj) is dict)
    need(obj.get("status") in ("passed", "blocked"))
    need(obj.get("stage") in ("preflight", "install", "verify", "complete", "bootstrap_gate"))
    out = {"status": obj["status"], "stage": obj["stage"]}
    if "gate" in obj:
        need(
            obj["gate"]
            in {
                "identity",
                "packet",
                "legacy_entry",
                "legacy_package",
                "legacy_policy",
                "legacy_pins",
                "prior_receipts",
                "untouched_files",
                "service_snapshot",
                "operation_lock",
            }
        )
        out["gate"] = obj["gate"]
    for key in ("rollback_verified", "maintenance_installed", "normal_v1_deployed"):
        if key in obj:
            need(type(obj[key]) is bool)
            out[key] = obj[key]
    for key in (
        "Broker_restarts",
        "provider_posts",
        "account_changes",
        "sudoers_changes",
        "ssh_changes",
    ):
        if key in obj:
            need(type(obj[key]) is int and obj[key] == 0)
            out[key] = 0
    need(obj.get("automatic_retry") is False)
    if out["status"] == "passed":
        need(out.get("maintenance_installed") is True and out["stage"] == "complete")
        need(out.get("normal_v1_deployed") is False)
    return out


def run_sudo(argv, password_supplier, seconds=150):
    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ENV,
        start_new_session=True,
    )
    raw = bytearray()
    try:
        rc, raw, count = capture(child, 32768, seconds, password_supplier)
        need(count in (0, 1))
        try:
            result = bootstrap_projection(raw)
        except (ValueError, TypeError):
            raise TransportFailure("result_framing", "invalid_frame", rc=rc) from None
        need((rc == 0) == (result["status"] == "passed"))
        return {
            "kind": "result",
            "state": "received",
            "authentication_submissions": count,
            "bootstrap": result,
        }
    finally:
        erase(raw)


def emit(obj):
    print(json.dumps(obj, sort_keys=True), flush=True)


def remote_main():
    stage = "local_guard"
    possible = False

    def supply():
        emit(READY)  # Sent only after sudo requests authentication.
        return read_line(sys.stdin.buffer, MAX_SECRET, 30, "input_write", False)

    try:
        guard()
        stage = "host_gate"
        argv = target_command()
        stage = "command_dispatch"
        possible = True
        emit(run_sudo(argv, supply))
        return 0
    except BaseException as error:  # noqa: BLE001 - fixed classification only
        emit({"kind": "result", **failure(error, stage, possible)})
        return 1


def fetch_secret():
    # This command does not write Doppler fallback files or inject an environment.
    argv = [
        str(Path.home() / ".local/bin/doppler"),
        "--no-check-version",
        "--no-read-env",
        "--attempts",
        "1",
        "secrets",
        "get",
        KEY,
        "--plain",
        "--project",
        PROJECT,
        "--config",
        "dev",
    ]
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**ENV, "HOME": str(Path.home())},
        start_new_session=True,
    )
    raw = bytearray()
    try:
        rc, raw, _ = capture(child, MAX_SECRET + 1, 20)
        if rc != 0:
            raise TransportFailure("doppler_fetch", "cli_exit", rc=rc, possible_dispatch=False)
        if raw.endswith(b"\n"):
            del raw[-1:]
        try:
            secret_valid(raw)
        except Denied:
            raise TransportFailure(
                "doppler_fetch", "invalid_secret_shape", possible_dispatch=False
            ) from None
        return bytearray(raw)
    finally:
        erase(raw)


def check_secret_name():
    argv = [
        str(Path.home() / ".local/bin/doppler"),
        "--no-check-version",
        "--no-read-env",
        "--attempts",
        "1",
        "secrets",
        "--only-names",
        "--project",
        PROJECT,
        "--config",
        "dev",
        "--json",
    ]
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**ENV, "HOME": str(Path.home())},
        start_new_session=True,
    )
    raw = bytearray()
    try:
        rc, raw, _ = capture(child, 16384, 20)
        if rc != 0:
            raise TransportFailure("secret_name_check", "cli_exit", rc=rc, possible_dispatch=False)
        obj = json.loads(raw)
        if type(obj) is not dict or KEY not in obj:
            raise TransportFailure(
                "secret_name_check", "missing_name", rc=rc, possible_dispatch=False
            )
        return {"stage": "secret_name_check", "name_present": True, "secret_values_read": 0}
    finally:
        erase(raw)


def remote_argv(expected_sha=None):
    # Source and argv are public. A fresh Python interpreter receives no secret in argv.
    raw = Path(__file__).read_bytes()
    if expected_sha is not None:
        need(hashlib.sha256(raw).hexdigest() == expected_sha)
    source = raw.decode("utf-8").rsplit('if __name__ == "__main__":', 1)[0]
    command = [
        "systemd-run",
        "--user",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "LimitCORE=0",
        "-p",
        "RuntimeMaxSec=200",
        "/usr/bin/python3.14",
        "-I",
        "-B",
        "-S",
        "-c",
        source + "\nsys.exit(remote_main())",
    ]
    return [
        "/usr/bin/ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "asus-server",
        shlex.join(command),
    ]


def ssh_environment():
    environment = {**ENV, "HOME": str(Path.home())}
    # Existing SSH agent socket path is a reference, never a credential value.
    if "SSH_AUTH_SOCK" in os.environ:
        environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return environment


def exchange(
    argv, fetch=fetch_secret, seconds=190, *, kind="result", field="bootstrap", projection=None
):
    secret = bytearray()
    raw = bytearray()
    child = None
    stage = "ssh_connection"
    possible = True  # A remote NOPASSWD rule could dispatch before any READY frame.
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=ssh_environment(),
            start_new_session=True,
        )
        ready = read_line(child.stdout, 4096, 20, stage, possible)
        first = json.loads(ready)
        expected_submissions = 0
        if first == READY:
            possible = False  # Exact authenticated SSH peer is waiting for a password.
            stage = "doppler_fetch"
            secret = fetch()
            try:
                secret_valid(secret)
            except Denied:
                raise TransportFailure(
                    stage, "invalid_secret_shape", possible_dispatch=False
                ) from None
            stage = "input_write"
            possible = True  # Mark intent before the first byte; partial writes are unknown.
            child.stdin.write(secret)
            child.stdin.write(b"\n")
            child.stdin.flush()
            child.stdin.close()
            erase(secret)
            stage = "result_framing"
            raw = read_line(child.stdout, 4096, seconds, stage, possible)
            result = json.loads(raw)
            expected_submissions = 1
        else:
            stage = "result_framing"
            result = first
        need(type(result) is dict and result.get("kind") == kind)
        if result.get("state") in ("unknown", "blocked"):
            return {"state": result["state"], "diagnostic": safe_diagnostic(result["diagnostic"])}
        need(result.get("state") == "received")
        need(result.get("authentication_submissions") == expected_submissions)
        projected = (
            projection(result[field])
            if projection
            else bootstrap_projection(json.dumps({**result[field], "automatic_retry": False}))
        )
        child.wait(timeout=5)
        if child.returncode != 0:
            raise TransportFailure(
                stage, "child_exit", rc=child.returncode, possible_dispatch=possible
            )
        return {
            "state": "received",
            "authentication_submissions": expected_submissions,
            field: projected,
        }
    except BaseException as error:  # noqa: BLE001 - fixed stages only
        rc = child.poll() if child else None
        if stage == "ssh_connection" and child and rc is None:
            try:
                rc = child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                rc = None
        if isinstance(error, json.JSONDecodeError):
            return failure(
                TransportFailure(stage, "invalid_frame", rc=rc, possible_dispatch=possible), stage
            )
        if stage == "ssh_connection" and rc == 255:
            return failure(
                TransportFailure(stage, "child_exit", rc=rc, possible_dispatch=False), stage
            )
        if isinstance(error, TransportFailure):
            return failure(error, stage, possible, rc)
        return failure(error, stage, possible, rc)
    finally:
        erase(secret)
        erase(raw)
        if child is not None:
            terminate(child)


def exclusive_json(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(obj, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    # This is an operator gate, not an assertion that user consent was obtained.
    if (
        len(sys.argv) != 4
        or sys.argv[1:3] != ["--execute-after-user-confirmed", "--expected-transport-sha256"]
        or len(sys.argv[3]) != 64
        or any(c not in "0123456789abcdef" for c in sys.argv[3])
    ):
        emit({"state": "blocked", "reason": "human_saved_secret_gate"})
        return 1
    root = Path(__file__).resolve().parents[2] / "docs/evidence/asus-maintenance"
    claim = root / "bootstrap-transport-r1.claim.json"
    result = root / "bootstrap-transport-r1.result.json"
    claimed = False
    out = {"state": "blocked", "reason": "local_guard_or_existing_claim", "automatic_retry": False}
    try:
        argv = remote_argv(sys.argv[3])
        guard()
        check_secret_name()
        exclusive_json(claim, {"dispatch_intent": True, "wrapper_sha": WRAPPER_SHA})
        claimed = True
        out = {"state": "unknown", "reason": "transport_incomplete", "automatic_retry": False}
        out = {**exchange(argv), "automatic_retry": False}
    except BaseException as error:  # noqa: BLE001 - fixed stages only
        out.update(failure(error, "local_guard", claimed))
    finally:
        if claimed:
            exclusive_json(result, out)
        emit(out)
    return 0 if out.get("bootstrap", {}).get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
