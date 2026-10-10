"""Fixed PID metadata only: descriptor count, soft limit, no descriptor targets."""

import ctypes
import json
import os
import resource
import subprocess
from pathlib import Path


def main():
    assert os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server"
    assert (
        Path("/proc/self/cgroup").read_text().strip()
        == "0::/system.slice/api-quota-broker-fd-r1.service"
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0

    def state():
        p = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                "api-quota-broker.service",
                "--property=MainPID,ExecMainStartTimestampMonotonic,NRestarts,ActiveState,LimitNOFILE",
            ],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            timeout=5,
            check=True,
        )
        s = dict(line.split("=", 1) for line in p.stdout.decode().splitlines())
        assert s == {
            "MainPID": "387484",
            "ExecMainStartTimestampMonotonic": "1049144733993",
            "NRestarts": "0",
            "ActiveState": "active",
            "LimitNOFILE": "128",
        }
        return s

    before = state()
    count = len(list(Path("/proc/387484/fd").iterdir()))
    status = dict(
        line.split(":", 1) for line in Path("/proc/387484/status").read_text().splitlines()
    )
    tracer = int(status["TracerPid"].strip())
    assert tracer == 0 and state() == before
    return {
        "pid": 387484,
        "fd_count": count,
        "nofile": 128,
        "tracer_pid": tracer,
        "pins_unchanged": True,
        "descriptor_targets_read": False,
        "provider_posts": 0,
        "database_writes": 0,
    }


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True), flush=True)
