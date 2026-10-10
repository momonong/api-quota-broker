"""Fixed read-only reduction of the failed ASUS stage's final child launch.

No installation or database access. Candidate Python executes only as the
existing application UID/GID, in a read-only, network-isolated diagnostic unit.
"""

import ctypes
import json
import os
import pwd
import resource
import subprocess
from pathlib import Path

UNIT = "api-quota-broker-stage-probe-r2.service"
CANDIDATE = "/opt/api-quota-broker/releases/release-20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def guard():
    assert os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + UNIT
    group = Path("/sys/fs/cgroup/system.slice") / UNIT
    assert (group / "memory.max").read_text().strip() == "402653184"
    assert (group / "memory.swap.max").read_text().strip() == "0"
    assert os.statvfs(CANDIDATE).f_flag & os.ST_RDONLY
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    assert libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(39, 0, 0, 0, 0) == 1


def probe():
    result = {
        "stage": "guard",
        "code": "blocked",
        "rc": None,
        "errno": None,
        "exception_type": None,
        "timeout": False,
        "root_candidate_execution": False,
        "file_writes": 0,
        "provider_posts": 0,
    }
    try:
        guard()
        result["stage"] = "account_lookup"
        account = pwd.getpwnam("api-quota-broker")
        assert account.pw_uid == 995 and account.pw_gid == 982
        result["stage"] = "child_launch"
        child = subprocess.run(
            [
                CANDIDATE + "/runtime/bin/python",
                "-I",
                "-B",
                "-c",
                "import quota_broker,importlib.metadata; assert importlib.metadata.version('api-quota-broker')=='1.0.0'; assert quota_broker.__version__ == '1.0.0'",
            ],
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=[],
            env=ENV,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        result.update(
            stage="child_exit",
            code="passed" if child.returncode == 0 else "failed",
            rc=child.returncode,
        )
    except BaseException as error:  # noqa: BLE001 - never serialize error text
        result["exception_type"] = (
            type(error).__name__
            if type(error).__name__
            in {
                "KeyError",
                "AssertionError",
                "PermissionError",
                "FileNotFoundError",
                "OSError",
                "TimeoutExpired",
                "BlockingIOError",
                "SubprocessError",
                "ValueError",
                "TypeError",
            }
            else "other"
        )
        result["timeout"] = isinstance(error, subprocess.TimeoutExpired)
        if isinstance(error, OSError):
            result["errno"] = error.errno if type(error.errno) is int else None
    return result


def syscall_probe():
    guard()
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    effective = int(status["CapEff"].strip(), 16)
    result = {
        "kind": "syscall_reduction",
        "cap_setgid": bool(effective & (1 << 6)),
        "cap_setuid": bool(effective & (1 << 7)),
        "nnp": int(status["NoNewPrivs"].strip()),
        "seccomp": int(status["Seccomp"].strip()),
        "cases": {},
        "candidate_execution": False,
        "provider_posts": 0,
        "file_writes": 0,
    }
    cases = {
        "baseline": {},
        "groups": {"extra_groups": []},
        "gid": {"group": 982},
        "uid": {"user": 995},
        "combined": {"user": 995, "group": 982, "extra_groups": []},
    }
    for name, identity in cases.items():
        record = {"rc": None, "errno": None, "timeout": False}
        try:
            child = subprocess.run(
                ["/usr/bin/true"],
                **identity,
                env=ENV,
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            record["rc"] = child.returncode
        except OSError as error:
            record["errno"] = error.errno
        except subprocess.TimeoutExpired:
            record["timeout"] = True
        result["cases"][name] = record
    code = """import os,json
result={"step":"setgroups","errno":None,"uid":os.getuid(),"gid":os.getgid(),"groups":len(os.getgroups())}
try:
 os.setgroups([]);result["step"]="setregid"
 os.setregid(982,982);result["step"]="setreuid"
 os.setreuid(995,995);result["step"]="done"
except OSError as error:result["errno"]=error.errno
result.update(uid=os.getuid(),gid=os.getgid(),groups=len(os.getgroups()))
print(json.dumps(result))
"""
    child = subprocess.run(
        ["/usr/bin/python3.14", "-I", "-B", "-S", "-c", code],
        env=ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0 and len(child.stdout) < 1024
    manual = json.loads(child.stdout)
    assert set(manual) == {"step", "errno", "uid", "gid", "groups"}
    assert manual["step"] in {"setgroups", "setregid", "setreuid", "done"}
    assert all(type(manual[k]) is int for k in ("uid", "gid", "groups"))
    assert manual["errno"] is None or type(manual["errno"]) is int
    result["manual"] = manual
    return result


if __name__ == "__main__":
    print(json.dumps(syscall_probe(), sort_keys=True))
