"""Fixed read-only native test of retaining CAP_SETUID in the root verifier."""

import ctypes
import json
import os
import resource
import subprocess
from pathlib import Path

UNIT = "api-quota-broker-stage-probe-r3.service"
CANDIDATE = "/opt/api-quota-broker/releases/release-20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
# Identity/capability checks happen before importing candidate package code.
CHILD = """import os,json
from pathlib import Path
assert os.getresuid()==(995,995,995) and os.getresgid()==(982,982,982) and os.getgroups()==[]
s=dict(line.split(":",1) for line in Path("/proc/self/status").read_text().splitlines())
assert all(int(s[k].strip(),16)==0 for k in ("CapEff","CapPrm","CapAmb"))
assert int(s["NoNewPrivs"].strip())==1
import quota_broker,importlib.metadata
assert quota_broker.__version__=="1.0.0" and importlib.metadata.version("api-quota-broker")=="1.0.0"
print(json.dumps({"uid":995,"gid":982,"groups":0,"effective_caps":0,"permitted_caps":0,"ambient_caps":0,"nnp":1,"version":"1.0.0"}))
"""


def probe():
    assert os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + UNIT
    group = Path("/sys/fs/cgroup/system.slice") / UNIT
    assert (group / "memory.max").read_text().strip() == "402653184"
    assert (group / "memory.swap.max").read_text().strip() == "0"
    assert os.statvfs(CANDIDATE).f_flag & os.ST_RDONLY
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    assert libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(39, 0, 0, 0, 0) == 1
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    effective = int(status["CapEff"].strip(), 16)
    result = {
        "kind": "identity_probe",
        "root_setuid": bool(effective & 128),
        "root_setgid": bool(effective & 64),
        "rc": None,
        "errno": None,
        "timeout": False,
        "child": None,
        "provider_posts": 0,
        "file_writes": 0,
    }
    try:
        child = subprocess.run(
            [CANDIDATE + "/runtime/bin/python", "-I", "-B", "-c", CHILD],
            user=995,
            group=982,
            extra_groups=[],
            env=ENV,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        result["rc"] = child.returncode
        if child.returncode == 0:
            assert len(child.stdout) < 1024
            expected = {
                "uid": 995,
                "gid": 982,
                "groups": 0,
                "effective_caps": 0,
                "permitted_caps": 0,
                "ambient_caps": 0,
                "nnp": 1,
                "version": "1.0.0",
            }
            assert json.loads(child.stdout) == expected
            result["child"] = expected
    except OSError as error:
        result["errno"] = error.errno
    except subprocess.TimeoutExpired:
        result["timeout"] = True
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
