"""Fixed systemd client-token export to volatile /run; no network or provider IO."""

import ctypes
import json
import os
import re
import resource
import stat
import sys
from pathlib import Path

DIRECTORY = Path("/run/api-quota-broker-client")
CREDENTIAL = Path("/run/credentials/api-quota-broker-client.service/client_token")
UID = GID = 1000


def require(value):
    if not value:
        raise ValueError("client_runtime_gate")


def run_is_tmpfs(mountinfo):
    mounts = [line.split() for line in mountinfo.splitlines()]
    return any(
        len(fields) > 6
        and "-" in fields
        and fields[4] == "/run"
        and fields.index("-") + 1 < len(fields)
        and fields[fields.index("-") + 1] == "tmpfs"
        for fields in mounts
    )


def regular(path, *, uid, gid, mode, maximum=4096):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == uid
            and info.st_gid == gid
            and stat.S_IMODE(info.st_mode) == mode
            and info.st_nlink == 1
            and 0 < info.st_size <= maximum
        )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        require(
            len(raw) == info.st_size
            and (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
        )
        return raw, info


def directory():
    info = DIRECTORY.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == 0o711
    )
    require({p.name for p in DIRECTORY.iterdir()} <= {"client_token", "metadata.json"})


def cleanup():
    directory()
    meta = DIRECTORY / "metadata.json"
    token = DIRECTORY / "client_token"
    if not meta.exists():
        require(not token.exists() and not token.is_symlink() and not meta.is_symlink())
        return
    raw, _ = regular(meta, uid=0, gid=0, mode=0o644)
    value = json.loads(raw)
    require(value.get("schema") == 1 and value.get("state") == "ready")
    # Never need the plaintext for removal; reject foreign path/inode/link.
    info = token.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == UID
        and info.st_gid == GID
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
        and (info.st_dev, info.st_ino) == (value.get("device"), value.get("inode"))
    )
    token.unlink()
    meta.unlink()


def publish():
    directory()
    cleanup()
    raw, _ = regular(CREDENTIAL, uid=0, gid=0, mode=0o400, maximum=256)
    require(re.fullmatch(rb"[A-Za-z0-9_~-]{32,256}", raw.strip()) is not None)
    token, meta = DIRECTORY / "client_token", DIRECTORY / "metadata.json"
    owned = []
    try:
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        owned.append((token, os.fstat(fd).st_ino))
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw.strip())
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), UID, GID)
            info = os.fstat(stream.fileno())
        # Existing persistent Broker client key has no independent expiry.
        # This is not a new token, nor a renewal of Doppler/account admission.
        value = {
            "schema": 1,
            "state": "ready",
            "inode": info.st_ino,
            "device": info.st_dev,
            "expires_at": None,
            "expiry_basis": "existing nonexpiring local client key; volatile unit lifecycle",
        }
        fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        owned.append((meta, os.fstat(fd).st_ino))
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(json.dumps(value).encode())
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        for path, inode in reversed(owned):
            if path.lstat().st_ino == inode:
                path.unlink()
        raise


def main():
    try:
        require(sys.argv[1:] in (["publish"], ["cleanup"]))
        require(os.geteuid() == 0)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        require(ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0)
        require(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 0)
        require(os.environ.get("CREDENTIALS_DIRECTORY") == str(CREDENTIAL.parent))
        require(run_is_tmpfs(Path("/proc/self/mountinfo").read_text()))
        require(os.stat(DIRECTORY).st_dev == os.stat("/run").st_dev)
        require(
            Path("/proc/self/cgroup").read_bytes().strip()
            == b"0::/system.slice/api-quota-broker-client.service"
        )
        require(ctypes.CDLL(None).prctl(39, 0, 0, 0, 0) == 1)
        if sys.argv[1] == "publish":
            publish()
        else:
            cleanup()
        return 0
    except BaseException:  # noqa: BLE001 - never emit credential or raw exception text
        os.write(2, b"client_credential_unit_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
