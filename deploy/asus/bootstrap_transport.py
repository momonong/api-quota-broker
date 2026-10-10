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
PACKET = "/var/tmp/api-quota-broker-maintenance-review-2026-10-09-r1"
TAR_SHA = "60210b786d0d98f1c450c11bbcfa12c944d730c05041564fe6b89983a992c985"
WRAPPER_SHA = "afbc1db8e1596491d4d556d874eff7ba1a249982934c76f908cfd82d81ad7560"
PROMPT = b"AQB_SUDO_R1:morris:"
PROMPT_FORMAT = "AQB_SUDO_R1:%p:"
MAX_SECRET = 1024
READY = {"kind": "ready", "host": HOST, "principal": "morris", "wrapper_sha": WRAPPER_SHA}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}


class Denied(ValueError):
    pass


def need(condition):
    if not condition:
        raise Denied("transport_gate")


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


def read_line(stream, bound, seconds):
    deadline = time.monotonic() + seconds
    result = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            need(left > 0 and selector.select(left))
            char = os.read(stream.fileno(), 1)
            need(bool(char))
            if char == b"\n":
                return result
            result.extend(char)
            need(len(result) <= bound)


def terminate(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=5)


def capture(child, limit, seconds, password_supplier=None):
    """Bound both streams; send at most one line only for the exact sudo prompt."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    submitted = 0
    complete = False
    secret = bytearray()
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                selector.register(getattr(child, name), selectors.EVENT_READ, name)
            while selector.get_map():
                left = deadline - time.monotonic()
                need(left > 0)
                events = selector.select(left)
                need(events)
                for event, _ in events:
                    raw = os.read(event.fileobj.fileno(), 4096)
                    if not raw:
                        selector.unregister(event.fileobj)
                        continue
                    buf = buffers[event.data]
                    buf.extend(raw)
                    need(len(buf) <= limit)
                    if password_supplier is not None and event.data == "stderr" and not submitted:
                        # Refuse any unexpected pre-auth text, including a TTY requirement.
                        need(PROMPT.startswith(buf) or bytes(buf) == PROMPT)
                        if bytes(buf) == PROMPT:
                            secret = password_supplier()
                            secret_valid(secret)
                            submitted = 1
                            child.stdin.write(secret)
                            child.stdin.write(b"\n")
                            child.stdin.flush()
                            child.stdin.close()
                            erase(secret)
                    elif password_supplier is not None and submitted:
                        need(bytes(buf).count(PROMPT) <= 1)
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
        result = bootstrap_projection(raw)
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
    state = "blocked"

    def supply():
        emit(READY)  # Sent only after sudo requests authentication.
        return read_line(sys.stdin.buffer, MAX_SECRET, 30)

    try:
        guard()
        argv = target_command()
        state = "unknown"  # Any subsequent loss requires inspection, never resubmission.
        emit(run_sudo(argv, supply))
        return 0
    except BaseException:  # noqa: BLE001 - never reflect subprocess/error text
        emit({"kind": "result", "state": state})
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
        need(rc == 0)
        if raw.endswith(b"\n"):
            del raw[-1:]
        secret_valid(raw)
        return bytearray(raw)
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


def exchange(argv, fetch=fetch_secret, seconds=190):
    secret = bytearray()
    raw = bytearray()
    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ssh_environment(),
        start_new_session=True,
    )
    try:
        ready = read_line(child.stdout, 512, 20)
        first = json.loads(ready)
        if first == READY:
            secret = fetch()
            secret_valid(secret)
            child.stdin.write(secret)
            child.stdin.write(b"\n")
            child.stdin.flush()
            child.stdin.close()
            erase(secret)
            raw = read_line(child.stdout, 4096, seconds)
            result = json.loads(raw)
            expected_submissions = 1
        else:
            result = first
            expected_submissions = 0
        need(type(result) is dict and result.get("kind") == "result")
        if result.get("state") in ("unknown", "blocked"):
            return {"state": result["state"]}
        need(
            result.get("state") == "received"
            and result.get("authentication_submissions") == expected_submissions
        )
        # Validate without forwarding arbitrary keys or strings from remote output.
        projected = bootstrap_projection(
            json.dumps({**result["bootstrap"], "automatic_retry": False})
        )
        child.wait(timeout=5)
        need(child.returncode == 0)
        return {
            "state": "received",
            "authentication_submissions": expected_submissions,
            "bootstrap": projected,
        }
    finally:
        erase(secret)
        erase(raw)
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
        exclusive_json(claim, {"dispatch_intent": True, "wrapper_sha": WRAPPER_SHA})
        claimed = True
        out = {"state": "unknown", "reason": "transport_incomplete", "automatic_retry": False}
        out = {**exchange(argv), "automatic_retry": False}
    except BaseException:  # noqa: BLE001 - secret-containing errors must not escape
        out["error_redacted"] = True
    finally:
        if claimed:
            exclusive_json(result, out)
        emit(out)
    return 0 if out.get("bootstrap", {}).get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
