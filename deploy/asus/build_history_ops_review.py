"""Fresh local history capability review packet. No SSH, sudo, or host mutation."""

import hashlib
import importlib.util
import io
import json
import os
import shlex
import tarfile
from pathlib import Path

REMOTE = "/var/tmp/api-quota-broker-history-ops-review-2026-10-07-r15"


def wrapper(expected):
    code = f"""import hashlib,os,pwd,stat,subprocess,uuid
from pathlib import Path
expected={expected!r}
source=Path({REMOTE!r})
env={{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}}
def need(ok):
    if not ok: raise ValueError("history_upgrade_copy_gate")
try:
    need(os.geteuid()==0 and os.uname().nodename=="asus-ubuntu2604-server" and pwd.getpwnam("morris").pw_uid==1000)
    tty=os.open("/dev/tty",os.O_RDWR|os.O_NOCTTY|os.O_NOFOLLOW);need(os.isatty(tty))
    tmp=Path("/var/tmp").lstat();need(stat.S_ISDIR(tmp.st_mode) and tmp.st_uid==tmp.st_gid==0 and stat.S_IMODE(tmp.st_mode)==0o1777)
    meta=source.lstat();need(stat.S_ISDIR(meta.st_mode) and meta.st_uid==meta.st_gid==1000 and stat.S_IMODE(meta.st_mode)==0o700)
    need({{p.name for p in source.iterdir()}}==set(expected)|{{"history-ops-upgrade-once.sh"}})
    data={{}}
    for name,sha in expected.items():
        fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as f:
            a=os.fstat(f.fileno());need(stat.S_ISREG(a.st_mode) and a.st_uid==a.st_gid==1000 and a.st_nlink==1 and stat.S_IMODE(a.st_mode)==0o600 and a.st_size<=131072)
            raw=f.read(131073);b=os.fstat(f.fileno())
            need(len(raw)==a.st_size and hashlib.sha256(raw).hexdigest()==sha and (a.st_ino,a.st_mtime_ns,a.st_ctime_ns)==(b.st_ino,b.st_mtime_ns,b.st_ctime_ns))
            data[name]=raw
    p=subprocess.run(("/usr/bin/systemctl","show","api-quota-broker-ops-history-upgrade.service","--property=LoadState","--value"),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=8,check=False)
    need(p.returncode in (0,1) and p.stdout.strip()==b"not-found")
    target=Path("/var/tmp")/("aqb-history-ops-upgrade-"+uuid.uuid4().hex)
    target.mkdir(mode=0o700);target.chmod(0o700)
    for name,raw in data.items():
        fd=os.open(target/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb") as f:
            os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
    for n in (0,1,2):os.dup2(tty,n)
    if tty>2:os.close(tty)
    argv=("/usr/bin/systemd-run","--pipe","--quiet","--wait","--collect","--service-type=exec","--unit=api-quota-broker-ops-history-upgrade",
          "--property=Slice=system.slice","--property=MemoryMax=128M","--property=MemorySwapMax=0","--property=LimitCORE=0",
          "--property=CPUQuota=25%","--property=TasksMax=32","--property=UMask=0077","--property=NoNewPrivileges=yes",
          "--property=RuntimeMaxSec=360","--property=TimeoutStopSec=30","--property=PrivateNetwork=yes",
          "--property=RestrictAddressFamilies=AF_UNIX","--property=StandardError=null",
          "/usr/bin/python3.14","-I","-B","-S",str(target/"install_history_ops.py"),"--apply")
    os.execve(argv[0],argv,env)
except BaseException:
    os.write(1,b'{{"status":"blocked","code":"history_upgrade_copy_unverified","automatic_retry":false}}\\n')
    raise SystemExit(1)
"""
    compile(code, "<fixed-root-history-upgrade>", "exec")
    return (
        "#!/bin/bash\n# LOCAL REVIEW; upload/install/native acceptance not executed.\nset -euo pipefail\numask 077\n"
        'if [ "$#" -eq 0 ]; then printf \'%s\\n\' \'{"mode":"history_upgrade_review_plan","host_changes":0}\'; exit 0; fi\n'
        '[ "$#" -eq 1 ] && [ "$1" = --apply ]\n[ "$(/usr/bin/id -un)" = morris ]\n'
        '[ "$(/usr/bin/hostname)" = asus-ubuntu2604-server ]\n[ -t 0 ] && [ -t 1 ]\n'
        "/usr/bin/sudo -v\nexec /usr/bin/sudo -n -- /usr/bin/python3.14 -I -B -S -c "
        + shlex.quote(code)
        + " </dev/tty\n"
    ).encode()


def build(destination):
    source = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "public_history_install", source / "install_history_ops.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    raw = {
        name: (source / ("ops_entry.py" if name == "r14_ops_entry.py" else name)).read_bytes()
        for name in installer.NAMES
    }
    for name, value in raw.items():
        if len(value) > 131072:
            raise ValueError("source_bound")
        if name.endswith(".py"):
            compile(value, "<sealed-history-source>", "exec")
    seal = {
        "schema": 1,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
    }
    raw["seal.json"] = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    raw["history-ops-upgrade-once.sh"] = wrapper(
        {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
    )
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name, value in raw.items():
        with (destination / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, value in sorted(raw.items()):
            row = tarfile.TarInfo(name)
            row.mode, row.size = 0o600, len(value)
            archive.addfile(row, io.BytesIO(value))
    bundle_path = destination.with_suffix(".tar")
    with bundle_path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(bundle.getvalue())
    return {
        "status": "sealed",
        "review_directory": str(destination),
        "remote_directory_proposal": REMOTE,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
        "bundle_path": str(bundle_path),
        "bundle_sha256": hashlib.sha256(bundle.getvalue()).hexdigest(),
        "remote_bundle_proposal": REMOTE + ".tar",
        "uploaded": False,
        "installed": False,
        "native_inspect_executed": False,
        "native_history_audit_executed": False,
        "Doppler_GET": 0,
        "provider_calls": 0,
        "credential_reads": 0,
        "Broker_restart": False,
        "token_expiry_extended": False,
    }


if __name__ == "__main__":
    print('{"mode":"history_ops_builder_plan","host_changes":0,"credential_reads":0}')
