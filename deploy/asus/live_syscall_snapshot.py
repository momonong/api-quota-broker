"""Four-second aggregate-only strace of one pinned stalled Broker process."""

import ctypes
import json
import os
import re
import resource
import signal
import subprocess
import time
from pathlib import Path

UNIT = "api-quota-broker-syscalls-r1.service"
PID = 387484
PINS = {
    "api-quota-broker.service": ("387484", "1049144733993", "0"),
    "orderflow.service": ("240075", "690258498304", "0"),
    "ssh.service": ("172081", "515138274924", "0"),
}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def services():
    result = {}
    for name, expected in PINS.items():
        p = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                name,
                "--property=MainPID,ExecMainStartTimestampMonotonic,NRestarts,ActiveState",
            ],
            capture_output=True,
            env=ENV,
            timeout=5,
            check=True,
        )
        s = dict(line.split("=", 1) for line in p.stdout.decode("ascii").splitlines())
        assert (
            s["ActiveState"] == "active"
            and tuple(s[k] for k in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts"))
            == expected
        )
        result[name] = s
    return result


def tracer():
    s = dict(line.split(":", 1) for line in Path("/proc/387484/status").read_text().splitlines())
    return int(s["TracerPid"].strip())


def project(raw):
    assert len(raw) <= 32768
    output = {}
    for line in raw.decode("ascii").splitlines():
        m = re.fullmatch(
            r"\s*(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+)\s+(\d+)\s+(?:(\d+)\s+)?([a-z][a-z0-9_]+)", line
        )
        if m and m[6] != "total":
            output[m[6]] = {"calls": int(m[4]), "errors": int(m[5] or 0), "seconds": float(m[2])}
    return output


def main():
    assert os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + UNIT
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    assert libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(39, 0, 0, 0, 0) == 1
    before = services()
    assert tracer() == 0
    child = subprocess.Popen(
        ["/usr/bin/strace", "-f", "-c", "-qq", "-p", "387484"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=ENV,
        start_new_session=True,
    )
    attached = False
    try:
        end = time.monotonic() + 4
        while time.monotonic() < end:
            attached = attached or tracer() == child.pid
            time.sleep(0.1)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
        _, raw = child.communicate(timeout=8)
    detached = tracer() == 0
    after = services()
    return {
        "attached": attached,
        "detached": detached,
        "services_unchanged": before == after,
        "trace_rc": child.returncode,
        "aggregate": project(raw),
        "trace_seconds": 4,
        "provider_posts": 0,
        "database_writes": 0,
    }


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True), flush=True)
