"""One fixed prerequisite update for the authorized October 12 PDF deployment.

No uploaded root code, account changes, service restart or provider request.
Only the installed verifier allowlist, reviewed unit template and package hashes
change; the existing maintenance transaction still owns app backup/deployment.
"""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
BACKUP = Path("/var/backups/api-quota-broker/pdf-integration-20261012-r1")
CURRENT = Path("/opt/api-quota-broker/current")
EXPECTED_RELEASE = (
    "releases/release-510b514833ce06ee491128a8c12d5ab2acb2666d0f9839ee79cdc82e0ec4dda8"
)
PINS = {
    "manifest.json": "e93ace572e9c56d3c39a990f489bb04969dc6aeceaf4cce744f6604d04d29f95",
    "broker-v1.service": "55e36fc717a1b143dd65bfedc629aa647a08043ab151fb6986ab884fed3261b3",
    "release_verifier.py": "fc99a704d01722244a7d445acdeaec3697c8acac04935da54ef5263efd1115d8",
}


def need(value):
    if not value:
        raise ValueError("pdf_prerequisite_gate")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def read(path, uid=0, mode=0o644):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1)
        need(stat.S_IMODE(info.st_mode) == mode and info.st_size <= 2097152)
        raw = stream.read(2097153)
        need(len(raw) == info.st_size)
        return raw


def exclusive(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync(path.parent)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_file(path, raw):
    temporary = path.with_name(".pdf-r1-" + path.name)
    exclusive(temporary, raw, 0o644)
    os.replace(temporary, path)
    sync(path.parent)


def replacements(old):
    unit = old["broker-v1.service"]
    edits = (
        (
            b"    --max-media-input-bytes 1048576",
            b"    --google-pdf-count-tokens --serialize-work \\\n    --max-media-input-bytes 8388608",
        ),
        (b"--max-request-bytes 2097152", b"--max-request-bytes 16777216"),
        (b"MemoryHigh=256M", b"MemoryHigh=384M"),
        (b"MemoryMax=384M", b"MemoryMax=512M"),
    )
    for before, after in edits:
        need(unit.count(before) == 1)
        unit = unit.replace(before, after)
    verifier = old["release_verifier.py"]
    anchor = b'    "src/quota_broker/server.py",\n'
    need(verifier.count(anchor) == 1)
    verifier = verifier.replace(anchor, anchor + b'    "src/quota_broker/token_preflight.py",\n')
    manifest = json.loads(old["manifest.json"])
    need(manifest["schema"] == 2)
    need(manifest["files"]["broker-v1.service"] == PINS["broker-v1.service"])
    need(manifest["files"]["release_verifier.py"] == PINS["release_verifier.py"])
    manifest["files"]["broker-v1.service"] = digest(unit)
    manifest["files"]["release_verifier.py"] = digest(verifier)
    return {
        "broker-v1.service": unit,
        "release_verifier.py": verifier,
        "manifest.json": wire(manifest),
    }


def services():
    raw = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            "api-quota-broker.service",
            "orderflow.service",
            "ssh.service",
            "--property=Id,ActiveState,MainPID,ExecMainStartTimestampMonotonic,NRestarts",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    need(raw.count(b"ActiveState=active") == 3)
    return raw


def prepare(*, owner=0, system=Path("/etc/systemd/system")):
    def checked(path):
        return read(path, uid=owner)

    for directory in (BASE, STATE, BACKUP.parent):
        info = directory.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022)
    need(os.readlink(CURRENT) == EXPECTED_RELEASE)
    lock = os.open(STATE / "operation.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1)
        need(stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = {name: checked(BASE / name) for name in PINS}
        need(all(digest(raw) == PINS[name] for name, raw in old.items()))
        # Verify every existing package member before changing its hash inventory.
        manifest = json.loads(old["manifest.json"])
        for name, expected in manifest["files"].items():
            need(Path(name).name == name and digest(checked(BASE / name)) == expected)
        for name, expected in manifest["units"].items():
            need(Path(name).name == name and digest(checked(system / name)) == expected)
        new = replacements(old)
        before = services()
        BACKUP.mkdir(mode=0o700)
        sync(BACKUP.parent)
        for name, raw in old.items():
            exclusive(BACKUP / name, raw)
        exclusive(
            BACKUP / "intent.json", wire({"operation": "pdf-prerequisite-r1", "state": "intent"})
        )
        sync(BACKUP)
        try:
            for name, raw in new.items():
                replace_file(BASE / name, raw)
            need(all(checked(BASE / name) == raw for name, raw in new.items()))
            need(services() == before and os.readlink(CURRENT) == EXPECTED_RELEASE)
            result = {
                "state": "passed",
                "service_restarts": 0,
                "provider_posts": 0,
                "database_writes": 0,
                "files": {n: digest(v) for n, v in new.items()},
                "backup": str(BACKUP),
                "automatic_retry": False,
            }
        except Exception:  # noqa: BLE001 - no secret/raw command output
            restore_files(old, new, checked)
            result = {"state": "restored", "automatic_retry": False}
        exclusive(BACKUP / "result.json", wire(result))
        return result
    finally:
        os.close(lock)


def restore_files(old, new, checked):
    # Rename can succeed before directory fsync raises. Inspect every member,
    # never infer changed bytes solely from a completed function call.
    for name in new:
        need(checked(BASE / name) in (old[name], new[name]))
    for name in new:
        if checked(BASE / name) == new[name] and new[name] != old[name]:
            temporary = (BASE / name).with_name(".pdf-r1-" + name)
            if os.path.lexists(temporary):
                need(checked(temporary) in (old[name], new[name]))
                temporary.unlink()
            replace_file(BASE / name, old[name])
    need(all(checked(BASE / name) == raw for name, raw in old.items()))


def restore(*, owner=0):
    """Independent hash-fenced recovery before app deploy, never automatic replay."""
    need(os.readlink(CURRENT) == EXPECTED_RELEASE)
    for directory in (BASE, STATE, BACKUP):
        info = directory.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022)
    lock = os.open(STATE / "operation.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1)
        need(stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = {name: read(BACKUP / name, uid=owner, mode=0o600) for name in PINS}
        need(all(digest(raw) == PINS[name] for name, raw in old.items()))
        need(
            json.loads(read(BACKUP / "intent.json", uid=owner, mode=0o600))
            == {"operation": "pdf-prerequisite-r1", "state": "intent"}
        )
        new = replacements(old)
        before = services()
        exclusive(BACKUP / "restore-intent.json", wire({"automatic_retry": False}))
        restore_files(old, new, lambda path: read(path, uid=owner))
        need(services() == before)
        result = {"state": "restored", "automatic_retry": False}
        exclusive(BACKUP / "restore-result.json", wire(result))
        return result
    finally:
        os.close(lock)


if __name__ == "__main__":
    need(os.getuid() == os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server")
    need(sys.argv[1:] in ([], ["--restore"]))
    print(json.dumps(restore() if sys.argv[1:] else prepare(), sort_keys=True))
