"""Read the root-managed volatile client credential; never resolve provider keys."""

import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

RUNTIME_DIRECTORY = Path("/run/api-quota-broker-client")
RUNTIME_TOKEN = RUNTIME_DIRECTORY / "client_token"
CLIENT_UID = CLIENT_GID = 1000


class ClientCredentialError(ValueError):
    pass


def unit_ready() -> bool:
    """Unprivileged fixed public state query; no credential/env/command arguments."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                "api-quota-broker-client.service",
                "--property=ActiveState",
                "--property=SubState",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            check=False,
        )
        fields = dict(line.split(b"=", 1) for line in result.stdout.splitlines() if b"=" in line)
        return result.returncode == 0 and fields == {
            b"ActiveState": b"active",
            b"SubState": b"exited",
        }
    except (OSError, subprocess.TimeoutExpired):
        return False


def _read(directory_fd: int, name: str, uid: int, gid: int, mode: int, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not (
            stat.S_ISREG(before.st_mode)
            and before.st_uid == uid
            and before.st_gid == gid
            and stat.S_IMODE(before.st_mode) == mode
            and before.st_nlink == 1
            and 0 < before.st_size <= maximum
        ):
            raise ClientCredentialError("client_credential_unsafe")
        value = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_size,
        ) or len(value) != before.st_size:
            raise ClientCredentialError("client_credential_changed")
        return value


def read_runtime_client() -> str:
    """No anonymous, environment, sudo, home-file, or provider fallback."""
    try:
        if os.geteuid() != CLIENT_UID:
            raise ClientCredentialError("client_credential_wrong_user")
        if not unit_ready():
            raise ClientCredentialError("client_credential_unit_inactive")
        # Parent is root0711: traverse with O_PATH, never require directory read.
        fd = os.open(RUNTIME_DIRECTORY, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o711:
                raise ClientCredentialError("client_credential_unsafe_directory")
            metadata = json.loads(_read(fd, "metadata.json", 0, 0, 0o644, 4096))
            if metadata.get("schema") != 1 or metadata.get("state") != "ready":
                raise ClientCredentialError("client_credential_unit_not_ready")
            expiry = metadata.get("expires_at")
            if expiry is not None:
                end = datetime.fromisoformat(expiry)
                if end.tzinfo is None or datetime.now(UTC) >= end:
                    raise ClientCredentialError("client_credential_expired")
            value = _read(fd, "client_token", CLIENT_UID, CLIENT_GID, 0o600, 256)
            info = os.stat("client_token", dir_fd=fd, follow_symlinks=False)
            if (info.st_ino, info.st_dev) != (metadata.get("inode"), metadata.get("device")):
                raise ClientCredentialError("client_credential_changed")
        finally:
            os.close(fd)
        token = value.decode("ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", token):
            raise ClientCredentialError("client_credential_invalid")
        if not unit_ready():
            raise ClientCredentialError("client_credential_unit_inactive")
        return token
    except ClientCredentialError:
        raise
    except FileNotFoundError:
        raise ClientCredentialError("client_credential_missing_or_unit_inactive") from None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ClientCredentialError("client_credential_unreadable_or_invalid") from None
