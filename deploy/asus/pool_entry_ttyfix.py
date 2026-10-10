"""Review-only pool entry repair. No sudo, deployment, credential or API calls.

The fixed root shell parses an immutable -c argument, never stdin. The legacy
payload, bootstrap path and once budget stay unchanged. This fresh-entry candidate
rejects an existing bootstrap. A separate pinned resume gate may be designed only
after private state/history/ledger prove that the once budget is unconsumed.
"""

import json
import shlex

REVIEW = "/var/tmp/api-quota-broker-provider-pool-review-2026-10-04-r1"
BOOTSTRAP = "/var/backups/api-quota-broker/pool-bootstrap-2026-10-04-r1"
BUNDLE_SHA = "76556abc1f334df60c217406e3f38683ccab84b20481800fe2987f03e52964ad"
VERIFIER_SHA = "de436865f8f72007244f08ee29b29e3a1096d85e08c5cb199fb3246b4b78ed1b"


# No -s and no shell source obtained from a terminal. This producer is local
# review code, not a sudo allowlist or a privileged installed helper.
def root_argv(code, *args):
    return ("/usr/bin/bash", "--noprofile", "--norc", "-c", code, "pool-root", *args)


def bootstrap_python():
    return r"""
import hashlib,io,json,os,stat,sys,tarfile
from pathlib import Path
source,destination=map(Path,sys.argv[1:])
stage="bootstrap_metadata"
def metadata(path):
    try:
        value=path.lstat()
    except FileNotFoundError:
        return {"exists":False}
    return {"exists":True,"uid":value.st_uid,"gid":value.st_gid,
            "mode":format(stat.S_IMODE(value.st_mode),"04o"),
            "type":"directory" if stat.S_ISDIR(value.st_mode) else
                   "symlink" if stat.S_ISLNK(value.st_mode) else
                   "regular" if stat.S_ISREG(value.st_mode) else "other"}
def fail(code,actual=None,expected=None):
    value={"mode":"pool_entry_ttyfix","status":"blocked","stage":stage,
           "code":code,"automatic_retry":False,"this_entry_provider_dispatch":False}
    if actual is not None:value["actual"]=actual
    if expected is not None:value["expected"]=expected
    print(json.dumps(value,sort_keys=True),flush=True)
    raise SystemExit(2)
try:
    if os.geteuid()!=0:fail("root_required")
    for parent in destination.parents:
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            fail("bootstrap_ancestor_untrusted",metadata(parent),"root-owned real directory, not group/other writable")
    info=destination.parent.lstat()
    if info.st_uid!=0 or info.st_gid!=0 or stat.S_IMODE(info.st_mode)!=0o700:
        fail("backup_parent_contract",metadata(destination.parent),"root:root 0700")
    stage="once_state"
    for name in ("provider-pool-2026-10-04-r1.claim.json","provider-pool-2026-10-04-r1.json"):
        existing=metadata(destination.parent/name)
        if existing["exists"]:fail("once_state_present",existing,"absent; private history review required")
    stage="bootstrap_collision"
    existing=metadata(destination)
    if existing["exists"]:fail("bootstrap_path_present",existing,"absent; preserve and inspect, never rename or delete")
    stage="bundle_verify"
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=1000 or info.st_gid!=1000 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>=4194304:
            fail("bundle_metadata_untrusted")
        raw=stream.read(4194304)
    if hashlib.sha256(raw).hexdigest()!="76556abc1f334df60c217406e3f38683ccab84b20481800fe2987f03e52964ad":fail("bundle_sha_mismatch")
    stage="bootstrap_stage"
    os.mkdir(destination,0o700)
    expected={"source.tar","enable_provider_pool.py","provider_pool_plan.py","build_asus_release.py","policy.json"}
    seen=set()
    with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as bundle:
        for member in bundle:
            if member.name not in expected or member.name in seen or not member.isfile() or member.mode!=0o600 or member.uid!=0 or member.gid!=0 or member.mtime!=0 or member.pax_headers:
                fail("bundle_member_untrusted")
            seen.add(member.name)
            value=bundle.extractfile(member).read()
            if len(value)!=member.size:fail("bundle_member_size")
            fd=os.open(destination/member.name,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600)
            with os.fdopen(fd,"wb") as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(value);stream.flush();os.fsync(stream.fileno())
    if seen!=expected:fail("bundle_members_incomplete")
    policy=json.loads((destination/"policy.json").read_bytes())
    for name,field in (("source.tar","source_archive_sha256"),("enable_provider_pool.py","operator_sha256"),("provider_pool_plan.py","planner_sha256"),("build_asus_release.py","verifier_sha256")):
        if hashlib.sha256((destination/name).read_bytes()).hexdigest()!=policy[field]:fail("staged_sha_mismatch")
    for directory in (destination,destination.parent):
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
except SystemExit:
    raise
except Exception:
    fail("bootstrap_boundary_unverified")
""".strip()


def root_code():
    return "\n".join(
        (
            "set -eu",
            "umask 077",
            "TASK_REVIEW=$1; TASK_BOOTSTRAP=$2",
            'if [ "$(/usr/bin/systemctl show api-quota-broker-pool.service --property=LoadState --value)" != not-found ]; then',
            '  /usr/bin/printf \'%s\\n\' \'{"status":"blocked","stage":"once_state","code":"pool_unit_present","automatic_retry":false}\'; exit 2',
            "fi",
            '/usr/bin/python3.14 -I -B - "$TASK_REVIEW/provider-pool-r1.tar" "$TASK_BOOTSTRAP" <<\'PYROOT\'',
            bootstrap_python(),
            "PYROOT",
            "# Parser source is the -c argument; stdin is exclusively child/application input.",
            "exec </dev/tty >/dev/tty 2>/dev/tty",
            'exec /usr/bin/systemd-run --pty --wait --collect --unit=api-quota-broker-pool --service-type=exec --slice=system.slice --property=MemoryMax=384M --property=MemorySwapMax=0 --property=LimitCORE=0 --property=CPUQuota=50% --property=LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred /usr/bin/python3.14 -I -B "$TASK_BOOTSTRAP/enable_provider_pool.py" --apply',
        )
    )


def wrapper():
    # The explicit guard prevents publication of an entry that could bypass the
    # unknown private execution history. Root/state review must precede resealing.
    return "\n".join(
        (
            "#!/bin/bash",
            "# REVIEW ONLY; old entry disabled, private bootstrap/claim history pending.",
            "printf '%s\\n' 'BLOCKED: private-state review required; do not run this candidate.' >&2",
            "exit 1",
            "set -eu",
            "umask 077",
            '[ "$(/usr/bin/id -un)" = morris ]',
            '[ "$(/usr/bin/hostname)" = asus-ubuntu2604-server ]',
            "[ -t 0 ] && [ -t 1 ]",
            f"TASK_REVIEW={shlex.quote(REVIEW)}",
            f"TASK_BOOTSTRAP={shlex.quote(BOOTSTRAP)}",
            f'[ "$(/usr/bin/sha256sum "$TASK_REVIEW/provider-pool-r1.tar" | /usr/bin/cut -d " " -f1)" = {shlex.quote(BUNDLE_SHA)} ]',
            f'[ "$(/usr/bin/sha256sum "$TASK_REVIEW/verify_pool_offline.py" | /usr/bin/cut -d " " -f1)" = {shlex.quote(VERIFIER_SHA)} ]',
            "/usr/bin/sudo -v",
            "exec /usr/bin/sudo -n "
            + shlex.join(root_argv(root_code()))
            + ' "$TASK_REVIEW" "$TASK_BOOTSTRAP" </dev/tty',
            "",
        )
    )


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "mode": "review_only",
                "enabled": False,
                "private_state": "unknown",
                "root_parser": "bash -c immutable argument",
                "provider_calls": 0,
                "host_changes": 0,
            }
        )
    )
