"""Local new once packet; never deploys, creates a token, or calls a provider."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import shlex
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

REMOTE = "/var/tmp/api-quota-broker-seven-pool-review-2026-10-08-r2"


def load(path, label):
    spec = importlib.util.spec_from_file_location(label, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[label] = module
    spec.loader.exec_module(module)
    return module


def wrapper(expected):
    code = f"""import hashlib,json,os,pwd,stat,subprocess,uuid
from pathlib import Path
expected={expected!r}
source=Path({REMOTE!r})
env={{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}}
def need(ok):
    if not ok:raise ValueError("sealed_two_phase_gate")
try:
    need(os.geteuid()==0 and os.uname().nodename=="asus-ubuntu2604-server" and pwd.getpwnam("morris").pw_uid==1000)
    tty=os.open("/dev/tty",os.O_RDWR|os.O_NOFOLLOW|os.O_NOCTTY);need(os.isatty(tty))
    info=source.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==info.st_gid==1000 and stat.S_IMODE(info.st_mode)==0o700)
    need({{p.name for p in source.iterdir()}}==set(expected)|{{"seven-pool-once.sh","verify_seven_pool_offline.py"}})
    tmp=Path("/var/tmp").lstat();need(stat.S_ISDIR(tmp.st_mode) and tmp.st_uid==0 and stat.S_IMODE(tmp.st_mode)==0o1777)
    target=Path("/var/tmp")/("aqb-seven-pool-"+uuid.uuid4().hex);target.mkdir(mode=0o700);target.chmod(0o700)
    for name,sha in expected.items():
        fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as f:
            a=os.fstat(f.fileno());need(stat.S_ISREG(a.st_mode) and a.st_uid==a.st_gid==1000 and a.st_nlink==1 and stat.S_IMODE(a.st_mode)==0o600 and a.st_size<=134217728)
            raw=f.read(134217729);b=os.fstat(f.fileno());need(len(raw)==a.st_size and hashlib.sha256(raw).hexdigest()==sha and (a.st_ino,a.st_mtime_ns,a.st_ctime_ns)==(b.st_ino,b.st_mtime_ns,b.st_ctime_ns))
        fd=os.open(target/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb") as f:os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
    common=("/usr/bin/systemd-run","--pipe","--quiet","--wait","--collect","--service-type=exec","--property=Slice=system.slice","--property=MemoryMax=384M","--property=MemorySwapMax=0","--property=LimitCORE=0","--property=CPUQuota=50%","--property=TasksMax=32","--property=UMask=0077","--property=NoNewPrivileges=yes","--property=StandardError=null")
    for unit in ("api-quota-broker-seven-audit.service","api-quota-broker-seven-pool.service"):
        p=subprocess.run(("/usr/bin/systemctl","show",unit,"--property=LoadState","--value"),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=8,check=False)
        need(p.returncode in (0,1) and p.stdout.strip()==b"not-found")
    audit=common+("--unit=api-quota-broker-seven-audit","--property=RuntimeMaxSec=90","--property=TimeoutStopSec=15","--property=ProtectSystem=strict","--property=PrivateNetwork=yes","--property=ReadOnlyPaths=/var/lib/api-quota-broker /var/backups/api-quota-broker","--property=InaccessiblePaths=/etc/api-quota-broker/credentials /etc/api-quota-broker-ops/ops_doppler.cred /run/credentials","/usr/bin/python3.14","-I","-B","-S",str(target/"seven_pool_operator.py"),"--audit")
    p=subprocess.run(audit,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=110,check=False)
    need(p.returncode==0 and len(p.stdout)<=32768)
    gate=json.loads(p.stdout);need(gate["status"]=="passed" and gate["mode"]=="seven_pool_private_readonly_gate" and gate["credential_reads"]==gate["provider_posts"]==gate["db_write"]==0)
    fd=os.open(target/"audit.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as f:os.fchmod(f.fileno(),0o600);f.write(json.dumps(gate,sort_keys=True).encode()+b"\\n");f.flush();os.fsync(f.fileno())
    for n in (0,1,2):os.dup2(tty,n)
    if tty>2:os.close(tty)
    # Only the verified audit permits this credential injection. It never
    # unlocks arbitrary commands, units, SQL, paths, or old once entries.
    argv=common+("--unit=api-quota-broker-seven-pool","--property=RuntimeMaxSec=1200","--property=TimeoutStopSec=60","--property=LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred","/usr/bin/python3.14","-I","-B","-S",str(target/"seven_pool_operator.py"),"--apply")
    os.execve(argv[0],argv,env)
except BaseException:
    os.write(1,b'{{"status":"blocked","code":"seven_pool_outer_gate","automatic_retry":false,"provider_posts":0}}\\n')
    raise SystemExit(1)
"""
    compile(code, "<sealed-two-phase-root>", "exec")
    return (
        "#!/bin/bash\n# Review only; old r1 once is never invoked.\nset -euo pipefail\numask 077\n"
        'if [ "$#" -eq 0 ]; then printf \'%s\\n\' \'{"mode":"seven_pool_review_plan","provider_posts":0}\'; exit 0; fi\n'
        '[ "$#" -eq 1 ] && [ "$1" = --apply ]\n[ -t 0 ] && [ -t 1 ]\n'
        '[ "$(/usr/bin/id -un)" = morris ]\n[ "$(/usr/bin/hostname)" = asus-ubuntu2604-server ]\n'
        "/usr/bin/sudo -v\nexec /usr/bin/sudo -n -- /usr/bin/python3.14 -I -B -S -c "
        + shlex.quote(code)
        + " </dev/tty\n"
    ).encode()


def build(destination):
    directory = Path(__file__).parent
    root = directory.parents[1]
    builder = load(root / "scripts/build_asus_release.py", "release_builder")
    # Runtime matches the project Python contract; imports are offline only.
    artifact = builder.build_release(root)
    plan = load(directory / "seven_pool_plan.py", "new_plan")
    checked_at = datetime.now(UTC)
    reviewed, validity = checked_at.isoformat(), (checked_at + timedelta(days=14)).isoformat()
    admission = {
        provider: {
            "model": model,
            "key_ref": ref,
            "account_scope": "asus-dev-" + provider + "-key",
            "human_free_no_payment_details": True,
            "account_basis": "existing_human_free_declaration_same_key_slot",
            "account_reviewed_at": reviewed,
            "account_valid_until": validity,
            "official_url": plan.OFFICIAL[provider],
            "official_reviewed_at": reviewed,
            "official_valid_until": validity,
        }
        for provider, (model, ref, _) in plan.PROVIDERS.items()
    }
    # Keep the previously accepted synthetic PNG exact; not a user document.
    png = "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAsCAAAAADwNOEjAAAASElEQVR4nGP4TyFgGDVg2BnAgATQxXHxh5MBeBXhYI9EA9DVjDQDsGkeaQZgUzMSDcCqbhgZAJPElmjwGj7MDCAHjBowLAwAAM/oF2guJ3zDAAAAAElFTkSuQmCC"
    if len(base64.b64decode(png, validate=True)) != 129:
        raise ValueError("synthetic_fixture_invalid")
    r15 = json.loads((root / "docs/evidence/asus-history-ops/r15-seal-2026-10-07.json").read_text())
    policy = {
        "schema": 1,
        "source_archive_sha256": artifact.manifest["archive_sha256"],
        "payload_manifest_sha256": artifact.manifest["payload_manifest_sha256"],
        "admission": admission,
        "synthetic_ocr_png": png,
        "r15_entry_sha256": r15["files"]["history_ops_entry.py"],
        "Groq_new_posts": 0,
        "provider_posts_max": 7,
        "local_validity_basis": "14 days local review budget; not official account expiry or remaining",
    }
    files = {
        "source.tar": artifact.archive,
        "policy.json": (json.dumps(policy, sort_keys=True, indent=2) + "\n").encode(),
        "pool_base.py": (directory / "enable_provider_pool.py").read_bytes(),
        "pool_base_plan.py": (directory / "provider_pool_plan.py").read_bytes(),
        "build_asus_release.py": (root / "scripts/build_asus_release.py").read_bytes(),
        **{
            name: (directory / name).read_bytes()
            for name in (
                "seven_pool_operator.py",
                "seven_pool_plan.py",
                "ops_history_projection.py",
            )
        },
    }
    seal = {
        "schema": 1,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    files["seal.json"] = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    files["seven-pool-once.sh"] = wrapper(
        {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    )
    files["verify_seven_pool_offline.py"] = (
        directory / "verify_seven_pool_offline.py"
    ).read_bytes()
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name, raw in files.items():
        with (destination / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, raw in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.mode = 0o600
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    bundle = destination.with_suffix(".tar")
    with bundle.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(archive.getvalue())
    return {
        "status": "sealed",
        "review_directory": str(destination),
        "remote_directory_proposal": REMOTE,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "bundle_path": str(bundle),
        "bundle_sha256": hashlib.sha256(archive.getvalue()).hexdigest(),
        "release": artifact.manifest,
        "uploaded": False,
        "root_deployed": False,
        "provider_posts": 0,
        "credential_reads": 0,
        "service_changes": 0,
    }


if __name__ == "__main__":
    print('{"mode":"seven_pool_local_builder","host_changes":0,"provider_posts":0}')
