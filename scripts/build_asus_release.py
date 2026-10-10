"""Build and verify an exact ASUS release allowlist; default build is dry-run.

The archive contains working-tree bytes, never .git, .venv, state, catalog data,
credentials or tests. A HEAD records provenance; file hashes identify dirty
payload bytes. No deployment, credential lookup, dependency installation or
network operation is performed. Verification/extraction needs only Python 3.10+
stdlib; building uses the project's Python 3.12+ (tomllib).
"""

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

SOURCE_FILES = (
    "src/quota_broker/__init__.py",
    "src/quota_broker/bounded_curl.py",
    "src/quota_broker/catalog.py",
    "src/quota_broker/cli.py",
    "src/quota_broker/client.py",
    "src/quota_broker/client_credentials.py",
    "src/quota_broker/config.py",
    "src/quota_broker/core.py",
    "src/quota_broker/discovery.py",
    "src/quota_broker/discovery_parsers.py",
    "src/quota_broker/discovery_sources.py",
    "src/quota_broker/families.py",
    "src/quota_broker/family_adapters.py",
    "src/quota_broker/family_transport.py",
    "src/quota_broker/gateway.py",
    "src/quota_broker/gateway_providers.py",
    "src/quota_broker/gateway_server.py",
    "src/quota_broker/key_admin.py",
    "src/quota_broker/nvidia.py",
    "src/quota_broker/nvidia_server.py",
    "src/quota_broker/provider_policy.py",
    "src/quota_broker/queue.py",
    "src/quota_broker/registry.py",
    "src/quota_broker/retry.py",
    "src/quota_broker/routing.py",
    "src/quota_broker/server.py",
)
DEPLOY_FILES = (
    "deploy/asus/acceptance.py",
    "deploy/asus/api-quota-broker.service",
    "deploy/asus/api-quota-broker-v1.service",
    "deploy/asus/api-quota-broker-client.service",
    "deploy/asus/aqb",
    "deploy/asus/export_client_credential.py",
    "deploy/asus/backup.sh",
    "deploy/asus/gateway.disabled.json",
    "deploy/asus/import_credentials.py",
    "deploy/asus/install.sh",
    "deploy/asus/install.py",
    "deploy/asus/initial_install.py",
    "deploy/asus/rollback.sh",
    "deploy/asus/swap_guard.py",
)
BASE_FILES = ("pyproject.toml", "uv.lock")
VERIFIER_FILE = "scripts/build_asus_release.py"
RUNTIME_BUILDER_FILE = "scripts/prepare_asus_runtime.py"
MANIFEST_MEMBER = "release-manifest.json"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
DESCRIPTION = (
    "Captured allowlisted working-tree bytes; HEAD is provenance, file hashes identify content."
)
HASH = re.compile(r"[a-f0-9]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]{0,48}")


class ReleaseError(ValueError):
    def __init__(self, reason: str):
        super().__init__("release validation failed")
        self.reason = reason


class ReleaseArtifact(NamedTuple):
    archive: bytes
    manifest: dict[str, Any]
    archive_name: str
    manifest_name: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def _paths(source_only_fixture: bool = False) -> tuple[str, ...]:
    # Only fixture callers can explicitly omit deployment files. CLI never can.
    return tuple(
        sorted(
            BASE_FILES
            + SOURCE_FILES
            + (VERIFIER_FILE, RUNTIME_BUILDER_FILE)
            + (() if source_only_fixture else DEPLOY_FILES)
        )
    )


def _mode(path: str) -> int:
    return 0o755 if path in DEPLOY_FILES and path.endswith(".sh") else 0o644


def _directory(path: Path) -> int:
    pieces = path.absolute().parts
    if ".." in pieces:
        raise ReleaseError("source_layout")
    descriptor = os.open(pieces[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for piece in pieces[1:]:
            child = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _read_regular(root: Path, relative: str, bound: int = MAX_FILE_BYTES) -> bytes:
    """Open each component through no-follow directory descriptors; FIFO cannot block."""
    descriptors = []
    try:
        descriptors.append(_directory(root))
        pieces = relative.split("/")
        for piece in pieces[:-1]:
            descriptors.append(
                os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1])
            )
        descriptors.append(
            os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        )
        before = os.fstat(descriptors[-1])
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReleaseError("source_type")
        if before.st_size > bound:
            raise ReleaseError("source_size")
        parts = bytearray()
        while True:
            chunk = os.read(descriptors[-1], min(65536, bound + 1 - len(parts)))
            if not chunk:
                break
            parts.extend(chunk)
            if len(parts) > bound:
                raise ReleaseError("source_size")
        after = os.fstat(descriptors[-1])
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReleaseError("source_changed")
        return bytes(parts)
    except OSError:
        raise ReleaseError("source_type") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _inventory(root: Path) -> None:
    """Unknown formal .py files must trigger an explicit allowlist update."""
    source = root / "src" / "quota_broker"
    # Parent descriptors and all payload files are separately checked no-follow.
    if source.is_symlink() or not source.is_dir():
        raise ReleaseError("source_layout")
    observed = set()
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.is_symlink():
                raise ReleaseError("source_type")
            if entry.is_dir(follow_symlinks=False):
                if entry.name != "__pycache__":
                    raise ReleaseError("source_layout")
            elif not entry.is_file(follow_symlinks=False):
                raise ReleaseError("source_type")
            elif entry.name.endswith(".py"):
                observed.add("src/quota_broker/" + entry.name)
    if observed != set(SOURCE_FILES):
        raise ReleaseError("source_layout")


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode or len(completed.stdout) > 2 * 1024 * 1024:
        raise ReleaseError("git_metadata")
    return completed.stdout


def read_provenance(root: Path, paths: tuple[str, ...]) -> dict[str, Any]:
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise ReleaseError("git_metadata")
    dirty = bool(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    records = _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths
    ).split(b"\0")
    changes = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status_code = record[:2]
        name = record[3:].decode("utf-8")
        if name in paths:
            changes[name] = "untracked" if status_code == b"??" else "modified"
        if b"R" in status_code or b"C" in status_code:
            index += 1  # Original rename path is metadata only, never payload.
    return {
        "head": head,
        "working_tree_dirty": dirty,
        "payload_dirty": bool(changes),
        "included_changes": [{"path": path, "kind": changes[path]} for path in sorted(changes)],
        "description": DESCRIPTION,
    }


def _tar(payload: dict[str, bytes], manifest: bytes) -> bytes:
    output = io.BytesIO()
    members = {**payload, MANIFEST_MEMBER: manifest}
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(members):
            header = tarfile.TarInfo(path)
            header.size = len(members[path])
            header.mode = _mode(path)
            header.uid = header.gid = header.mtime = 0
            header.uname = header.gname = ""
            archive.addfile(header, io.BytesIO(members[path]))
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ReleaseError("archive_size")
    return raw


def build_release(root: Path, *, source_only_fixture: bool = False) -> ReleaseArtifact:
    """Capture the full fixed payload, including untracked/dirty capability code."""
    import tomllib

    paths = _paths(source_only_fixture)
    _inventory(root)
    provenance = read_provenance(root, paths)
    payload = {}
    payload_bytes = 0
    for path in paths:
        payload[path] = _read_regular(root, path)
        payload_bytes += len(payload[path])
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise ReleaseError("source_size")
    project = tomllib.loads(payload["pyproject.toml"].decode("utf-8")).get("project", {})
    version = project.get("version")
    if (
        project.get("name") != "api-quota-broker"
        or not isinstance(version, str)
        or not VERSION.fullmatch(version)
    ):
        raise ReleaseError("version")
    _inventory(root)
    if read_provenance(root, paths) != provenance or any(
        _read_regular(root, path) != payload[path] for path in paths
    ):
        raise ReleaseError("source_changed")
    release = {
        "schema_version": 1,
        "policy": "source-only-fixture-v1" if source_only_fixture else "asus-release-v1",
        "project": "api-quota-broker",
        "package_version": version,
        "source": provenance,
        "files": [
            {
                "path": path,
                "size": len(payload[path]),
                "mode": _mode(path),
                "sha256": _digest(payload[path]),
            }
            for path in paths
        ],
    }
    manifest = _canonical(release)
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise ReleaseError("manifest_size")
    archive = _tar(payload, manifest)
    external: dict[str, Any] = {
        "schema_version": 1,
        "archive_format": "tar-ustar",
        "archive_bytes": len(archive),
        "archive_sha256": _digest(archive),
        "payload_manifest_sha256": _digest(manifest),
        "artifact_id": "release-" + _digest(manifest),
        "release": release,
    }
    # The same strict parser/verifier is the final build gate.
    verified = verify_archive(
        archive, external["payload_manifest_sha256"], source_only_fixture=source_only_fixture
    )
    if verified != external:
        raise ReleaseError("manifest")
    stem = (
        "api-quota-broker-"
        + version
        + "-"
        + provenance["head"][:12]
        + "-"
        + external["archive_sha256"][:12]
    )
    return ReleaseArtifact(archive, external, stem + ".tar", stem + ".manifest.json")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("manifest")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ReleaseError("manifest")


def _object(raw: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ReleaseError("manifest")
    return raw


def _validate_manifest(raw: bytes, source_only_fixture: bool) -> dict[str, Any]:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ReleaseError("manifest_size")
    release = _object(
        json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant),
        {"schema_version", "policy", "project", "package_version", "source", "files"},
    )
    policy = "source-only-fixture-v1" if source_only_fixture else "asus-release-v1"
    if (
        type(release["schema_version"]) is not int
        or release["schema_version"] != 1
        or release["policy"] != policy
        or release["project"] != "api-quota-broker"
        or not isinstance(release["package_version"], str)
        or not VERSION.fullmatch(release["package_version"])
    ):
        raise ReleaseError("manifest")
    source = _object(
        release["source"],
        {"head", "working_tree_dirty", "payload_dirty", "included_changes", "description"},
    )
    if (
        not isinstance(source["head"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", source["head"])
        or type(source["working_tree_dirty"]) is not bool
        or type(source["payload_dirty"]) is not bool
        or source["description"] != DESCRIPTION
    ):
        raise ReleaseError("manifest")
    paths = _paths(source_only_fixture)
    changes = source["included_changes"]
    if not isinstance(changes, list) or len(changes) > len(paths):
        raise ReleaseError("manifest")
    changed_paths = []
    for change in changes:
        change = _object(change, {"path", "kind"})
        if (
            not isinstance(change["path"], str)
            or change["path"] not in paths
            or change["kind"] not in ("modified", "untracked")
        ):
            raise ReleaseError("manifest")
        changed_paths.append(change["path"])
    if (
        changed_paths != sorted(set(changed_paths))
        or source["payload_dirty"] != bool(changes)
        or source["payload_dirty"]
        and not source["working_tree_dirty"]
    ):
        raise ReleaseError("manifest")
    files = release["files"]
    if not isinstance(files, list) or len(files) != len(paths):
        raise ReleaseError("manifest")
    for expected, record in zip(paths, files, strict=True):
        record = _object(record, {"path", "size", "mode", "sha256"})
        if (
            record["path"] != expected
            or type(record["size"]) is not int
            or not 0 <= record["size"] <= MAX_FILE_BYTES
            or type(record["mode"]) is not int
            or record["mode"] != _mode(expected)
            or not isinstance(record["sha256"], str)
            or not HASH.fullmatch(record["sha256"])
        ):
            raise ReleaseError("manifest")
    if _canonical(release) != raw:
        raise ReleaseError("manifest")
    return release


def _verify(
    archive: bytes, expected_manifest_sha256: str, source_only_fixture: bool
) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(expected_manifest_sha256, str) or not HASH.fullmatch(
        expected_manifest_sha256
    ):
        raise ReleaseError("manifest_hash")
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise ReleaseError("archive_size")
    expected = set(_paths(source_only_fixture)) | {MANIFEST_MEMBER}
    payload = {}
    payload_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle:
            if (
                member.name not in expected
                or member.name in payload
                or member.type != tarfile.REGTYPE
                or member.pax_headers
                or member.linkname
            ):
                raise ReleaseError("member")
            if (
                member.mode != _mode(member.name)
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.mtime != 0
                or not 0
                <= member.size
                <= (MAX_MANIFEST_BYTES if member.name == MANIFEST_MEMBER else MAX_FILE_BYTES)
            ):
                raise ReleaseError("member")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ReleaseError("member")
            with stream:
                payload[member.name] = stream.read(member.size + 1)
            if member.name != MANIFEST_MEMBER:
                payload_bytes += len(payload[member.name])
                if payload_bytes > MAX_PAYLOAD_BYTES:
                    raise ReleaseError("archive_size")
            if len(payload[member.name]) != member.size:
                raise ReleaseError("member")
    if set(payload) != expected:
        raise ReleaseError("member")
    raw = payload.pop(MANIFEST_MEMBER)
    if _digest(raw) != expected_manifest_sha256:
        raise ReleaseError("manifest_hash")
    release = _validate_manifest(raw, source_only_fixture)
    for record in release["files"]:
        data = payload[record["path"]]
        if record["size"] != len(data) or record["sha256"] != _digest(data):
            raise ReleaseError("file_hash")
    if _tar(payload, raw) != archive:
        raise ReleaseError("archive_canonical")
    return {
        "schema_version": 1,
        "archive_format": "tar-ustar",
        "archive_bytes": len(archive),
        "archive_sha256": _digest(archive),
        "payload_manifest_sha256": expected_manifest_sha256,
        "artifact_id": "release-" + expected_manifest_sha256,
        "release": release,
    }, payload


def verify_archive(
    archive: bytes, expected_manifest_sha256: str, *, source_only_fixture: bool = False
) -> dict[str, Any]:
    """Verify against a trusted canonical embedded-manifest hash, never extract."""
    return _checked_verify(archive, expected_manifest_sha256, source_only_fixture)[0]


def _checked_verify(
    archive: bytes, expected_manifest_sha256: str, source_only_fixture: bool
) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        return _verify(archive, expected_manifest_sha256, source_only_fixture)
    except (
        tarfile.TarError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        KeyError,
    ):
        raise ReleaseError("manifest") from None


def verify_manifest(
    archive: bytes, manifest_json: bytes, *, source_only_fixture: bool = False
) -> dict[str, Any]:
    """Check the external JSON manifest too; authentication still needs a trusted hash."""
    try:
        if len(manifest_json) > MAX_MANIFEST_BYTES + 4096:
            raise ReleaseError("manifest_size")
        manifest = _object(
            json.loads(manifest_json, object_pairs_hook=_pairs, parse_constant=_constant),
            {
                "schema_version",
                "archive_format",
                "archive_bytes",
                "archive_sha256",
                "payload_manifest_sha256",
                "artifact_id",
                "release",
            },
        )
        if (
            type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or manifest["archive_format"] != "tar-ustar"
            or type(manifest["archive_bytes"]) is not int
            or not 0 < manifest["archive_bytes"] <= MAX_ARCHIVE_BYTES
        ):
            raise ReleaseError("manifest")
        for key in ("archive_sha256", "payload_manifest_sha256"):
            if not isinstance(manifest[key], str) or not HASH.fullmatch(manifest[key]):
                raise ReleaseError("manifest")
        _validate_manifest(_canonical(manifest["release"]), source_only_fixture)
        verified = verify_archive(
            archive, manifest["payload_manifest_sha256"], source_only_fixture=source_only_fixture
        )
        if manifest != verified:
            raise ReleaseError("manifest")
        return verified
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, KeyError):
        raise ReleaseError("manifest") from None


def _new_file(directory: int, name: str, data: bytes, mode: int) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory
    )
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def extract_archive(archive: bytes, expected_manifest_sha256: str, staging: Path) -> dict[str, Any]:
    """Verify fully before writing; destination must be a new directory."""
    external, payload = _checked_verify(archive, expected_manifest_sha256, False)
    parent = _directory(staging.parent)
    try:
        os.mkdir(staging.name, mode=0o700, dir_fd=parent)
        directory = os.open(
            staging.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            # Explicit payload paths have no traversal; newly created directories
            # remain private until installer sets the final ownership/modes.
            for path, data in {**payload, MANIFEST_MEMBER: _canonical(external["release"])}.items():
                descriptors = [os.dup(directory)]
                try:
                    pieces = path.split("/")
                    for piece in pieces[:-1]:
                        try:
                            os.mkdir(piece, mode=0o700, dir_fd=descriptors[-1])
                        except FileExistsError:
                            pass
                        descriptors.append(
                            os.open(
                                piece,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptors[-1],
                            )
                        )
                    _new_file(descriptors[-1], pieces[-1], data, _mode(path))
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
        finally:
            os.close(directory)
    finally:
        os.close(parent)
    return external


def write_artifact(artifact: ReleaseArtifact, destination: Path) -> None:
    parent = _directory(destination.parent)
    try:
        try:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        directory = os.open(
            destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
    finally:
        os.close(parent)
    try:
        state = os.fstat(directory)
        if state.st_uid != os.geteuid() or stat.S_IMODE(state.st_mode) & 0o077:
            raise ReleaseError("output_parent")
        for name in (artifact.archive_name, artifact.manifest_name):
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ReleaseError("output_exists")
        _new_file(directory, artifact.archive_name, artifact.archive, 0o600)
        _new_file(directory, artifact.manifest_name, _canonical(artifact.manifest) + b"\n", 0o600)
    finally:
        os.close(directory)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(json.dumps({"error": "release_invalid", "reason": "options"}))
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    build = commands.add_parser("build")
    build.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--output-dir", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    verify.add_argument("manifest_sha256")
    verify.add_argument("--extract", type=Path)
    args = parser.parse_args(["build"] if argv == [] else argv)
    try:
        if args.command in (None, "build"):
            root = getattr(args, "repo", Path(__file__).resolve().parents[1])
            artifact = build_release(root)
            output = getattr(args, "output_dir", None)
            if output is not None:
                write_artifact(artifact, output)
            result = {
                "mode": "built" if output is not None else "dry_run",
                "archive_name": artifact.archive_name,
                "manifest_name": artifact.manifest_name,
                "allowlist": list(_paths()),
                "manifest": artifact.manifest,
            }
        else:
            archive = _read_regular(args.archive.parent, args.archive.name, MAX_ARCHIVE_BYTES)
            manifest = (
                extract_archive(archive, args.manifest_sha256, args.extract)
                if args.extract is not None
                else verify_archive(archive, args.manifest_sha256)
            )
            result = {
                "mode": "extracted" if args.extract is not None else "verified",
                "manifest": manifest,
            }
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "error": "release_invalid",
                    "reason": exc.reason if isinstance(exc, ReleaseError) else "io",
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
