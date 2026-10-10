"""Local nonsecret sealing only. Never uploads or calls sudo/systemd/Doppler."""

import hashlib
import json
import os
from pathlib import Path

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
    "broker_ops.ssh-deny.proposal",
)
REMOTE = "/var/tmp/api-quota-broker-ops-socket-review-2026-10-07-r13-preflight"
AGENT_REMOTE = "/var/tmp/api-quota-broker-ops-socket-review-2026-10-07-r14-agent-inspect"


def wrapper(seal, *, mode):
    """Inline Python is parsed in full, then restores TTY before --pty exec."""
    expected = dict(seal["files"])
    if mode not in ("--diagnose-retained-inspect", "--restore-agent-inspect"):
        raise ValueError("review_mode_unverified")
    remote = AGENT_REMOTE if mode == "--restore-agent-inspect" else REMOTE
    seal_data = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    expected["seal.json"] = hashlib.sha256(seal_data).hexdigest()
    code = f"""import hashlib,json,os,pwd,stat,subprocess,uuid
from pathlib import Path
expected={expected!r}
source=Path({remote!r})
env={{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}}
def need(ok):
    if not ok: raise ValueError("bootstrap_gate")
def private_read(p,owner,mode):
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as f:
        s=os.fstat(f.fileno())
        need(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==owner and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==mode and s.st_size<=131072)
        return f.read(131073)
try:
    need(os.geteuid()==0 and os.uname().nodename=="asus-ubuntu2604-server" and pwd.getpwnam("morris").pw_uid==1000)
    tty=os.open("/dev/tty",os.O_RDWR|os.O_NOCTTY|os.O_NOFOLLOW)
    need(os.isatty(tty))
    tmp=Path("/var/tmp").lstat()
    need(stat.S_ISDIR(tmp.st_mode) and tmp.st_uid==tmp.st_gid==0 and stat.S_IMODE(tmp.st_mode)==0o1777)
    s=source.lstat()
    need(stat.S_ISDIR(s.st_mode) and s.st_uid==s.st_gid==1000 and stat.S_IMODE(s.st_mode)==0o700)
    unit=subprocess.run(("/usr/bin/systemctl","show","api-quota-broker-ops-bootstrap.service","--property=LoadState","--value"),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,check=False,timeout=8)
    need(unit.returncode==0 and unit.stdout.strip()==b"not-found")
    target=Path("/var/tmp")/("aqb-ops-bootstrap-"+uuid.uuid4().hex)
    target.mkdir(mode=0o700);target.chmod(0o700)
    for name,sha in expected.items():
        raw=private_read(source/name,1000,0o600)
        need(hashlib.sha256(raw).hexdigest()==sha)
        fd=os.open(target/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb") as f:
            os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
        need(hashlib.sha256(private_read(target/name,0,0o600)).hexdigest()==sha)
    fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
    # Python source was already fully compiled. Restoring stdin cannot turn
    # terminal input into shell source, unlike the disabled historical bash -s.
    for number in (0,1,2): os.dup2(tty,number)
    if tty>2: os.close(tty)
    argv=("/usr/bin/systemd-run","--pty","--wait","--collect","--service-type=exec","--unit=api-quota-broker-ops-bootstrap",
          "--property=Slice=system.slice","--property=MemoryMax=128M","--property=MemorySwapMax=0","--property=LimitCORE=0",
          "--property=NoNewPrivileges=no","--property=UMask=0077","--property=CPUQuota=25%","--property=TasksMax=32","--property=RuntimeMaxSec=300",
          "--property=TimeoutStopSec=60",
          "/usr/bin/python3.14","-I","-B","-S",str(target/"install_ops.py"),{mode!r})
    os.execve(argv[0],argv,env)
except BaseException:
    os.write(1,b'{{"status":"blocked","code":"root_copy_or_tty_unverified","automatic_retry":false}}\\n')
    raise SystemExit(1)
"""
    compile(code, "<sealed-root-copy>", "exec")
    return (
        "#!/bin/bash\nset -euo pipefail\numask 077\n"
        "/usr/bin/sudo -v\n"
        "/usr/bin/sudo -n -- /usr/bin/python3.14 -I -B -S - <<'PYROOT'\n" + code + "PYROOT\n"
    ).encode()


def build(destination, *, mode):
    """Fresh directory only, preserve existing artifacts and all dirty work."""
    source = Path(__file__).parent
    data = {n: (source / n).read_bytes() for n in NAMES}
    seal = {"schema": 1, "files": {n: hashlib.sha256(raw).hexdigest() for n, raw in data.items()}}
    data["seal.json"] = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    data["ops-bootstrap-once.sh"] = wrapper(seal, mode=mode)
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name, raw in data.items():
        with (destination / name).open("xb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(raw)
    return {
        "status": "sealed",
        "review_directory": str(destination),
        "remote_directory": AGENT_REMOTE if mode == "--restore-agent-inspect" else REMOTE,
        "files": {n: hashlib.sha256(raw).hexdigest() for n, raw in data.items()},
        "uploaded": False,
        "native_initialized": False,
        "native_inspect": False,
        "native_restart": False,
        "token_created": False,
        "provider_calls": 0,
        "entry_mode": mode,
    }


if __name__ == "__main__":
    print(json.dumps({"mode": "local_review_builder", "host_changes": 0, "secret_reads": 0}))
