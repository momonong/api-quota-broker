"""Build one local sealed readonly audit candidate. Never upload or run sudo."""

import hashlib
import io
import json
import os
import shlex
import tarfile
from pathlib import Path

REMOTE = "/var/tmp/api-quota-broker-history-audit-review-2026-10-07-r1"
UNIT = "api-quota-broker-history-audit"


def wrapper(expected):
    """The complete root parser is an immutable -c argument; stdin stays a TTY."""
    code = f"""import hashlib,io,json,os,pwd,stat,subprocess,tarfile,uuid
from pathlib import Path
expected={expected!r}
source=Path({REMOTE!r})
env={{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}}
def need(ok):
    if not ok: raise ValueError("audit_copy_gate")
def read(p):
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as f:
        s=os.fstat(f.fileno())
        need(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==1000 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o600 and s.st_size<=262144)
        raw=f.read(262145);after=os.fstat(f.fileno())
        need(len(raw)==s.st_size and (s.st_dev,s.st_ino,s.st_mtime_ns,s.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns))
        return raw
try:
    need(os.geteuid()==0 and os.uname().nodename=="asus-ubuntu2604-server" and pwd.getpwnam("morris").pw_uid==1000)
    tty=os.open("/dev/tty",os.O_RDWR|os.O_NOCTTY|os.O_NOFOLLOW)
    need(os.isatty(tty))
    parent=Path("/var/tmp").lstat()
    need(stat.S_ISDIR(parent.st_mode) and parent.st_uid==parent.st_gid==0 and stat.S_IMODE(parent.st_mode)==0o1777)
    s=source.lstat()
    need(stat.S_ISDIR(s.st_mode) and s.st_uid==s.st_gid==1000 and stat.S_IMODE(s.st_mode)==0o700)
    names=set(expected)|{{"seal.json","history-audit-once.sh"}}
    need({{p.name for p in source.iterdir()}}==names)
    data={{n:read(source/n) for n in names}}
    need(all(hashlib.sha256(data[n]).hexdigest()==h for n,h in expected.items()))
    seal=json.loads(data["seal.json"])
    need(set(seal)=={{"schema","mode","files","native_authorized","apply_supported"}} and seal["schema"]==1 and seal["mode"]=="private_history_readonly_review" and seal["native_authorized"] is False and seal["apply_supported"] is False)
    need(seal["files"]=={{n:h for n,h in expected.items() if n!="seal.json"}})
    unit=subprocess.run(("/usr/bin/systemctl","show",{UNIT!r}+".service","--property=LoadState","--value"),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,check=False,timeout=8)
    need(unit.returncode==0 and unit.stdout.strip()==b"not-found")
    with tarfile.open(fileobj=io.BytesIO(data["history-audit-payload.tar"]),mode="r:") as archive:
        rows=archive.getmembers();need(len(rows)==1)
        row=rows[0]
        need(row.name=="integrate_provider_pool.py" and row.isfile() and row.uid==row.gid==0 and row.mode==0o600 and row.mtime==0 and not row.pax_headers and row.size<=131072)
        raw=archive.extractfile(row).read();need(len(raw)==row.size)
    compile(raw,"<sealed-history-audit>","exec")
    target=Path("/var/tmp")/("aqb-history-audit-"+uuid.uuid4().hex)
    target.mkdir(mode=0o700);target.chmod(0o700)
    fd=os.open(target/"integrate_provider_pool.py",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as f:
        os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
    for number in (0,1,2): os.dup2(tty,number)
    if tty>2: os.close(tty)
    argv=("/usr/bin/systemd-run","--pipe","--quiet","--wait","--collect","--service-type=exec","--unit="+{UNIT!r},
          "--property=Slice=system.slice","--property=MemoryMax=384M","--property=MemorySwapMax=0","--property=LimitCORE=0",
          "--property=NoNewPrivileges=yes","--property=UMask=0077","--property=CPUQuota=50%","--property=TasksMax=32",
          "--property=RuntimeMaxSec=150","--property=TimeoutStopSec=15","--property=ProtectSystem=strict",
          "--property=ProtectHome=yes","--property=PrivateNetwork=yes","--property=RestrictAddressFamilies=AF_UNIX",
          "--property=IPAddressDeny=any","--property=ReadOnlyPaths=/var/lib/api-quota-broker /var/backups/api-quota-broker",
          "--property=StandardError=null","/usr/bin/python3.14","-I","-B","-S",str(target/"integrate_provider_pool.py"),"--audit-only")
    os.execve(argv[0],argv,env)
except BaseException:
    os.write(1,b'{{"status":"blocked","code":"audit_copy_or_tty_unverified","automatic_retry":false}}\\n')
    raise SystemExit(1)
"""
    compile(code, "<root-history-audit-copy>", "exec")
    return (
        "#!/bin/bash\n# LOCAL REVIEW CANDIDATE: native execution is not yet approved.\n"
        "set -euo pipefail\numask 077\n"
        'if [ "$#" -eq 0 ]; then\n'
        '  printf \'%s\\n\' \'{"mode":"history_audit_review_plan","native_executed":false,"credential_reads":0,"provider_calls":0}\'\n'
        "  exit 0\nfi\n"
        '[ "$#" -eq 1 ] && [ "$1" = --audit-only ]\n'
        '[ "$(/usr/bin/id -un)" = morris ]\n'
        '[ "$(/usr/bin/hostname)" = asus-ubuntu2604-server ]\n'
        "[ -t 0 ] && [ -t 1 ]\n"
        "/usr/bin/sudo -v\n"
        "exec /usr/bin/sudo -n -- /usr/bin/python3.14 -I -B -S -c "
        + shlex.quote(code)
        + " </dev/tty\n"
    ).encode()


def build(destination):
    source = Path(__file__).parent
    raw = (source / "integrate_provider_pool.py").read_bytes()
    compile(raw, "<history-audit-source>", "exec")
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        row = tarfile.TarInfo("integrate_provider_pool.py")
        row.mode, row.size = 0o600, len(raw)
        archive.addfile(row, io.BytesIO(raw))
    files = {
        "history-audit-payload.tar": memory.getvalue(),
        "verify_history_audit_offline.py": (
            source / "verify_history_audit_offline.py"
        ).read_bytes(),
    }
    base_hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    # Noncircular: seal pins payload/verifier; wrapper pins those plus the seal;
    # the external review receipt pins all four, including the wrapper.
    seal = {
        "schema": 1,
        "mode": "private_history_readonly_review",
        "files": base_hashes,
        "native_authorized": False,
        "apply_supported": False,
    }
    files["seal.json"] = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    files["history-audit-once.sh"] = wrapper(
        {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    )
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name, value in files.items():
        with (destination / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    return {
        "status": "sealed",
        "mode": "private_history_readonly_review",
        "review_directory": str(destination),
        "remote_directory_proposal": REMOTE,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        "uploaded": False,
        "native_authorized": False,
        "native_audit_executed": False,
        "apply_supported": False,
        "credential_reads": 0,
        "provider_calls": 0,
        "service_commands": 0,
        "production_db_writes": 0,
    }


if __name__ == "__main__":
    print('{"mode":"local_history_audit_builder_plan","host_changes":0,"credential_reads":0}')
