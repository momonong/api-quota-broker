"""Read-only canonical browser verifier; independent of Gateway credentials."""

import grp
import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

SCRYPT_MAXMEM = 64 * 1024 * 1024
VERIFIER = re.compile(r"scrypt:32768:8:1\$([A-Za-z0-9]{8,128})\$([0-9a-f]{128})")


class BrowserAuthError(ValueError):
    def __init__(self) -> None:
        super().__init__("browser authentication unavailable")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BrowserAuthError()
        value[key] = item
    return value


@dataclass(frozen=True)
class BrowserCredential:
    revision: str
    verifier: str = field(repr=False)

    def verify(self, password: Any) -> bool:
        if (
            not isinstance(password, str)
            or not 1 <= len(password) <= 1024
            or any(char in password for char in "\r\n\0")
        ):
            return False
        try:
            encoded = password.encode("utf-8")
            match = VERIFIER.fullmatch(self.verifier)
            if match is None:
                return False
            digest = hashlib.scrypt(
                encoded,
                salt=match[1].encode("ascii"),
                n=32768,
                r=8,
                p=1,
                dklen=64,
                maxmem=SCRYPT_MAXMEM,
            )
            return hmac.compare_digest(digest.hex(), match[2])
        except (ValueError, UnicodeError):
            return False


class BrowserPasswordFile:
    """Production owner is root:api-quota-dashboard, mode 0640, with trusted parents.

    fixture_owner is an explicit local test boundary; the CLI never exposes it.
    No verifier or revision is cached across requests.
    """

    def __init__(self, path: str | Path, *, fixture_owner: tuple[int, int] | None = None) -> None:
        self.path = Path(path).absolute()
        self.fixture_owner = fixture_owner

    def read(self) -> BrowserCredential:
        try:
            uid, gid = self.fixture_owner or (0, grp.getgrnam("api-quota-dashboard").gr_gid)
            for parent in self.path.parents:
                info = parent.lstat()
                if (
                    self.fixture_owner is not None
                    and parent == Path("/tmp")
                    and stat.S_ISDIR(info.st_mode)
                    and info.st_uid == 0
                    and info.st_mode & stat.S_ISVTX
                ):
                    continue
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in {0, uid}
                    or info.st_mode & 0o022
                ):
                    raise BrowserAuthError()
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode))
                    != (uid, gid, 0o640)
                ):
                    raise BrowserAuthError()
                raw = stream.read(8193)
                after = os.fstat(stream.fileno())
            current = self.path.lstat()
            identity = lambda i: (i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns)
            if (
                len(raw) > 8192
                or identity(before) != identity(after)
                or identity(after) != identity(current)
            ):
                raise BrowserAuthError()
            obj = json.loads(raw, object_pairs_hook=_unique)
            if (
                not isinstance(obj, dict)
                or set(obj) != {"schema_version", "kind", "verifier", "revision"}
                or type(obj["schema_version"]) is not int
                or obj["schema_version"] != 1
                or obj["kind"] != "scrypt"
                or not isinstance(obj["verifier"], str)
                or VERIFIER.fullmatch(obj["verifier"]) is None
                or not isinstance(obj["revision"], str)
                or str(UUID(obj["revision"])) != obj["revision"]
            ):
                raise BrowserAuthError()
            return BrowserCredential(obj["revision"], obj["verifier"])
        except (OSError, KeyError, ValueError, TypeError, UnicodeError):
            raise BrowserAuthError() from None
