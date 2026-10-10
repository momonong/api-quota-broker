"""Fixed review transfer proposal. Default has no IO; explicit approved phase only.

Never sudo, root, native audit, Broker HTTP, Doppler, service mutation, or retry.
Local claim is permanent after dispatch intent, including unknown transfer result.
"""

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "docs/evidence/asus-history-audit/review-2026-10-07-r1"
REMOTE = "/var/tmp/api-quota-broker-history-audit-review-2026-10-07-r1"
EVIDENCE = ROOT / "docs/evidence/asus-history-audit"
CLAIM = EVIDENCE / "upload-offline-r1.claim.json"
RECEIPT = EVIDENCE / "upload-offline-r1.result.json"
FILES = {
    "history-audit-payload.tar": "df4a032ba7e3eb55a9a9653c1d0b740f3296138454e0d5097155a7a5a544b149",
    "verify_history_audit_offline.py": "9b1560ad5410c8d948d1f2ad6a330a6af0c661269629ea758fb52052918dc245",
    "history-audit-once.sh": "9b4d466cb044e9c3cffa8ef8e763cdc61278966301aa893a3f79325dc35b1f9c",
    "seal.json": "d676dd5185dba50df3d87eeb00fabf73c5d046576be38a7499ba6cc115942ef1",
}


class Blocked(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Blocked(code)


def local_packet(path):
    path = Path(path)
    info = path.lstat()
    need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "local_metadata_untrusted",
    )
    need({p.name for p in path.iterdir()} == set(FILES), "local_files_unverified")
    for name, sha in FILES.items():
        fd = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(before.st_mode)
                and before.st_uid == before.st_gid == os.getuid()
                and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_size <= 262144,
                "local_metadata_untrusted",
            )
            raw = stream.read(262145)
            after = os.fstat(stream.fileno())
            need(
                len(raw) == before.st_size
                and hashlib.sha256(raw).hexdigest() == sha
                and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
                "local_files_unverified",
            )


def batch(review):
    # SFTP batch syntax is distinct from shell quoting. Only the fixed filenames
    # and one verified absolute directory are accepted; no wildcard or overwrite.
    review = Path(review)
    need(
        review.is_absolute() and not any(c in str(review) for c in ('"', "\n", "\r", "\\")),
        "local_path_untrusted",
    )
    rows = [f'mkdir "{REMOTE}"', f'chmod 700 "{REMOTE}"']
    for name in sorted(FILES):
        rows.extend((f'put "{review / name}" "{REMOTE}/{name}"', f'chmod 600 "{REMOTE}/{name}"'))
    return ("\n".join(rows) + "\n").encode()


def remote_code(*, offline=False, before=None):
    # This runs as morris only, and reads only public service state and review
    # bytes. It never imports/executes the private audit source or entry wrapper.
    common = f"""import hashlib,json,os,stat,subprocess
from pathlib import Path
remote=Path({REMOTE!r})
files={FILES!r}
env={{"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}}
def need(ok):
    if not ok: raise ValueError("remote_review_gate")
def snapshot():
    services={{}}
    for name in ("api-quota-broker.service","orderflow.service","ssh.service"):
        p=subprocess.run(("/usr/bin/systemctl","show",name,"--property=ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic,NRestarts"),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=8,check=False)
        need(p.returncode==0 and len(p.stdout)<=1024)
        row=dict(line.split("=",1) for line in p.stdout.decode().splitlines())
        need(set(row)=={{"ActiveState","SubState","MainPID","ExecMainStartTimestampMonotonic","NRestarts"}} and row["ActiveState"]=="active" and row["SubState"]=="running")
        need(all(row[k].isdigit() for k in ("MainPID","ExecMainStartTimestampMonotonic","NRestarts")))
        services[name]=row
    pins={{}}
    for name in ("/etc/systemd/system/api-quota-broker.service","/usr/local/lib/api-quota-broker-ops/ops_entry.py","/etc/api-quota-broker-ops/policy.json"):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as f:
            s=os.fstat(f.fileno());need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022 and s.st_nlink==1 and s.st_size<=131072)
            raw=f.read(131073);need(len(raw)==s.st_size)
            pins[name]=hashlib.sha256(raw).hexdigest()
    current=Path("/opt/api-quota-broker/current")
    s=current.lstat();need(stat.S_ISLNK(s.st_mode) and s.st_uid==0)
    release=str(current.readlink())
    need(release=="releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770")
    need(pins["/etc/systemd/system/api-quota-broker.service"]=="6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060")
    need(pins["/usr/local/lib/api-quota-broker-ops/ops_entry.py"]=="0f2e75149567b27e2184ccbc89ba0e7da222a2a9cb6aa20268a6b757062492dc")
    return {{"services":services,"public_pins":pins,"current":release}}
try:
    need(os.geteuid()==os.getuid()==1000 and os.getgid()==1000 and os.uname().nodename=="asus-ubuntu2604-server")
    s=Path("/var/tmp").lstat()
    need(stat.S_ISDIR(s.st_mode) and s.st_uid==s.st_gid==0 and stat.S_IMODE(s.st_mode)==0o1777)
"""
    if not offline:
        code = (
            common
            + """    try:
        remote.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("remote_directory_collision")
    result=snapshot()
    print(json.dumps({"status":"passed","mode":"review_transfer_preflight","baseline":result},sort_keys=True))
"""
        )
    else:
        code = (
            common
            + f"""    before={before!r}
    need(snapshot()==before)
    s=remote.lstat();need(stat.S_ISDIR(s.st_mode) and s.st_uid==s.st_gid==1000 and stat.S_IMODE(s.st_mode)==0o700)
    need({{p.name for p in remote.iterdir()}}==set(files))
    for name,sha in files.items():
        fd=os.open(remote/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as f:
            s=os.fstat(f.fileno());need(stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==1000 and stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==1 and s.st_size<=262144)
            raw=f.read(262145);after=os.fstat(f.fileno())
            need(len(raw)==s.st_size and hashlib.sha256(raw).hexdigest()==sha and (s.st_dev,s.st_ino,s.st_mtime_ns,s.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns))
    p=subprocess.run(("/usr/bin/python3.14","-I","-B","-S",str(remote/"verify_history_audit_offline.py"),"--directory",str(remote)),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=30,check=False)
    need(p.returncode==0 and len(p.stdout)<=4096)
    result=json.loads(p.stdout)
    need(result["status"]=="passed" and result["files_verified"]==4 and result["python"].startswith("3.14.")
         and result["credential_reads"]==result["provider_calls"]==result["production_private_reads"]==result["production_db_writes"]==result["service_commands"]==0
         and result["native_audit_executed"] is False and result["apply_executed"] is False)
    need(snapshot()==before)
    print(json.dumps({{"status":"passed","mode":"review_upload_and_offline_only","files_verified":4,
                      "three_services_and_public_pins_preserved":True,"kernel_core_zero":True,
                      "kernel_dumpable_zero":True,"python":"3.14","provider_calls":0,
                      "credential_reads":0,"native_audit_executed":False,"root_executed":False}},sort_keys=True))
"""
        )
    code += """except BaseException:
    print('{"status":"blocked","code":"remote_review_unverified","automatic_retry":false}')
    raise SystemExit(1) from None
"""
    compile(code, "<fixed-public-remote-review>", "exec")
    return code


def ssh(code, *, run=subprocess.run, timeout=35):
    command = shlex.join(("/usr/bin/python3.14", "-I", "-B", "-S", "-c", code))
    p = run(
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=8",
            "asus-server",
            command,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    need(p.returncode == 0 and len(p.stdout) <= 16384, "remote_review_unverified")
    result = json.loads(p.stdout)
    need(type(result) is dict and result.get("status") == "passed", "remote_review_unverified")
    return result


def exclusive(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write((json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def present(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def check_baseline(value):
    need(
        type(value) is dict and set(value) == {"services", "public_pins", "current"},
        "remote_review_unverified",
    )
    need(
        value["current"]
        == "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770",
        "remote_review_unverified",
    )
    services = value["services"]
    need(
        type(services) is dict
        and set(services) == {"api-quota-broker.service", "orderflow.service", "ssh.service"},
        "remote_review_unverified",
    )
    for row in services.values():
        need(
            type(row) is dict
            and set(row)
            == {
                "ActiveState",
                "SubState",
                "MainPID",
                "ExecMainStartTimestampMonotonic",
                "NRestarts",
            }
            and row["ActiveState"] == "active"
            and row["SubState"] == "running"
            and all(
                type(row[k]) is str and row[k].isascii() and row[k].isdigit() and len(row[k]) <= 24
                for k in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts")
            ),
            "remote_review_unverified",
        )
    pins = value["public_pins"]
    names = {
        "/etc/systemd/system/api-quota-broker.service",
        "/usr/local/lib/api-quota-broker-ops/ops_entry.py",
        "/etc/api-quota-broker-ops/policy.json",
    }
    need(
        type(pins) is dict
        and set(pins) == names
        and all(
            type(v) is str and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
            for v in pins.values()
        ),
        "remote_review_unverified",
    )


def execute(*, run=subprocess.run):
    local_packet(REVIEW)
    need(not present(CLAIM) and not present(RECEIPT), "transfer_claim_exists")
    meta = CLAIM.parent.lstat()
    need(
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == os.getuid()
        and stat.S_IMODE(meta.st_mode) == 0o700,
        "local_metadata_untrusted",
    )
    preflight = ssh(remote_code(), run=run)
    need(set(preflight) == {"status", "mode", "baseline"}, "remote_review_unverified")
    need(preflight["mode"] == "review_transfer_preflight", "remote_review_unverified")
    baseline = preflight["baseline"]
    check_baseline(baseline)
    # An intent claim is durable before any mkdir/put. Partial or uncertain
    # transfer never permits automatic resend, reuse, cleanup, or a new path.
    exclusive(
        CLAIM,
        {
            "mode": "review_upload_offline_r1",
            "remote": REMOTE,
            "files": FILES,
            "automatic_retry": False,
            "native_audit_authorized": False,
        },
    )
    outcome = {"status": "blocked", "code": "transfer_unknown", "automatic_retry": False}
    try:
        p = run(
            (
                "sftp",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=8",
                "-b",
                "-",
                "asus-server",
            ),
            input=batch(REVIEW),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        need(p.returncode == 0, "transfer_unknown")
        outcome = ssh(remote_code(offline=True, before=baseline), run=run, timeout=95)
        need(
            set(outcome)
            == {
                "status",
                "mode",
                "files_verified",
                "three_services_and_public_pins_preserved",
                "kernel_core_zero",
                "kernel_dumpable_zero",
                "python",
                "provider_calls",
                "credential_reads",
                "native_audit_executed",
                "root_executed",
            },
            "remote_review_unverified",
        )
        need(
            outcome["native_audit_executed"] is False and outcome["root_executed"] is False,
            "remote_review_unverified",
        )
        need(
            outcome["mode"] == "review_upload_and_offline_only"
            and outcome["files_verified"] == 4
            and outcome["python"] == "3.14"
            and all(
                outcome[k] is True
                for k in (
                    "three_services_and_public_pins_preserved",
                    "kernel_core_zero",
                    "kernel_dumpable_zero",
                )
            )
            and outcome["provider_calls"] == outcome["credential_reads"] == 0,
            "remote_review_unverified",
        )
    except BaseException:  # noqa: BLE001 - no raw transport/private exception output.
        outcome = {
            "status": "blocked",
            "code": "transfer_or_offline_unknown",
            "automatic_retry": False,
        }
    exclusive(RECEIPT, outcome)
    return outcome


def main():
    if sys.argv[1:] == []:
        print(
            '{"mode":"history_review_transfer_proposal","network_calls":0,"uploaded":false,"native_audit_executed":false}'
        )
        return 0
    try:
        need(sys.argv[1:] == ["--execute-approved-upload-offline"], "arguments_denied")
        result = execute()
    except BaseException:  # noqa: BLE001 - fixed codes, no error strings or paths.
        result = {
            "status": "blocked",
            "code": "review_transfer_unverified",
            "automatic_retry": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
