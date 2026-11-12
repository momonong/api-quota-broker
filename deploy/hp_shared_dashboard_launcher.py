"""Fixed non-root launcher; immutable release metadata becomes a private snapshot.

Compatibility bridge for owner-only readers in both the baseline and new app.
Copies only the scoped read/plan Broker credential into a private owner snapshot.
The public Dashboard remains advice-only; no execution credential is accepted.
"""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT_OWNER = 0
BASE = Path("/opt/momonong-quota")
BROKER_CREDENTIAL = Path("/run/credentials/api-quota-dashboard.service/broker-read")
METADATA = (
    "accounts.json",
    "gateway.json",
    "provider-observations.json",
    "registry.json",
    "reviewed.json",
)
ARGS = [
    "dashboard-serve",
    "--reviewed-config",
    str(BASE / "current/metadata/reviewed.json"),
    "--accounts-file",
    str(BASE / "current/metadata/accounts.json"),
    "--provider-metadata-file",
    str(BASE / "current/metadata/provider-observations.json"),
    "--dashboard-password-file",
    "/etc/api-quota-dashboard/browser-auth.json",
    "--public-base-url",
    "https://momonong.me/quota/",
    "--port",
    "18586",
]


def require(ok, code):
    if not ok:
        raise ValueError(code)


def read(path, bound):
    for parent in path.parents:
        require(stat.S_ISDIR(parent.lstat().st_mode), "metadata_parent")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == ROOT_OWNER
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o640
            and before.st_gid == os.getegid()
            and before.st_size <= bound,
            "metadata_owner",
        )
        raw = f.read(bound + 1)
        after = os.fstat(f.fileno())
        require(
            len(raw) <= bound
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "metadata_drift",
        )
    return raw


def private_metadata(release, destination):
    for path in (release, release / "metadata"):
        info = path.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == ROOT_OWNER
            and info.st_gid == os.getegid()
            and stat.S_IMODE(info.st_mode) == 0o750,
            "metadata_directory",
        )
    rows = json.loads(read(release / "manifest.json", 128 * 1024))
    require(isinstance(rows, list) and 0 < len(rows) <= 128, "manifest")
    expected = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "size", "sha256"}, "manifest")
        require(isinstance(row["path"], str) and row["path"] not in expected, "manifest_duplicate")
        expected[row["path"]] = row
    contents = {}
    for name in METADATA:
        row = expected.get("metadata/" + name)
        require(row is not None and type(row["size"]) is int, "metadata_manifest")
        raw = read(release / "metadata" / name, 262144)
        require(
            len(raw) == row["size"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
            "metadata_digest",
        )
        contents[name] = raw
    info = destination.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "private_destination",
    )
    for name, raw in contents.items():
        fd = os.open(
            destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(raw)


def private_broker_token(destination):
    """systemd root-owned ACL credentials are copied before owner-only readers."""
    for directory in (BROKER_CREDENTIAL.parent.parent, BROKER_CREDENTIAL.parent):
        info = directory.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_OWNER and not info.st_mode & 0o022,
            "credential_directory",
        )
    fd = os.open(BROKER_CREDENTIAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid in {ROOT_OWNER, os.geteuid()}
            and not info.st_mode & 0o027
            and 32 <= info.st_size <= 1024,
            "credential_permissions",
        )
        raw = stream.read(1025)
        after = os.fstat(stream.fileno())
        require(
            (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and len(raw) == info.st_size
            and all(33 <= value <= 126 for value in raw),
            "credential_format",
        )
    target = destination / "broker-read"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
    return target


def application_args(private, token):
    return [
        "dashboard-serve",
        "--upstream",
        "http://127.0.0.1:18587",
        "--upstream-token-file",
        str(token),
        "--accounts-file",
        str(private / "accounts.json"),
        "--provider-metadata-file",
        str(private / "provider-observations.json"),
        "--dashboard-password-file",
        "/etc/api-quota-dashboard/browser-auth.json",
        "--public-base-url",
        "https://momonong.me/quota/",
        "--port",
        "18586",
    ]


def main():
    if os.geteuid() == 0:
        raise SystemExit("service_identity_required")
    require(sys.argv[1:] == ARGS, "fixed_arguments_required")
    for path in (BASE, BASE / "releases"):
        info = path.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_OWNER and not info.st_mode & 0o022,
            "release_root",
        )
    link = BASE / "current"
    info = link.lstat()
    require(stat.S_ISLNK(info.st_mode) and info.st_uid == ROOT_OWNER, "current_owner")
    target = os.readlink(link)
    require(re.fullmatch(r"releases/[a-z0-9][a-z0-9_-]{0,95}", target), "current_target")
    release = BASE / target
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="quota-metadata-") as directory:
        private = Path(directory)
        private_metadata(release, private)
        # Bind code and metadata to the same release, even during later switches.
        sys.path[:0] = [str(release / "app"), "/opt/momonong-quota-readonly/site"]
        token = private_broker_token(private)
        # Preserve the existing kernel argv required by the restricted maintenance
        # helper. Only Python's application argv uses the live read/plan upstream.
        sys.argv[1:] = application_args(private, token)
        from quota_broker.cli import main as application

        return application()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("metadata_snapshot_unavailable") from None
