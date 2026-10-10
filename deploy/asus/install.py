"""Reviewed ASUS deployment preparation and stopped-service backups.

Default commands inspect metadata and the independently pinned release artifact.
They never create credentials, build dependencies, start services or replay work.
Mutation requires an explicit --apply from a root TTY on the reviewed ASUS host.
"""

import argparse
import fcntl
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

HOST = "asus-ubuntu2604-server"
SERVICE = "api-quota-broker.service"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
CREDENTIALS = ("digest_key", "client_token", "admin_token", "queue_key", "doppler_service_token")
ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ACCOUNT_TOOLS = {"groupadd": "/usr/sbin/groupadd", "useradd": "/usr/sbin/useradd"}
NATIVE_OPERATIONS = {"uv_lock_normalize", "runtime_inventory"}
ERROR_REASONS = {
    "gate_failed",
    "deployment_gate_failed",
    "unsafe_path",
    "unsafe_metadata",
    "service_group_not_dedicated",
    "service_account_unsafe",
    "service_owner_required",
    "service_account_provision_failed",
    "account_tool_metadata",
    "account_tool_unavailable",
    "account_tool_exit",
    "account_tool_timeout",
    "os_error",
    "subprocess_exit",
    "subprocess_timeout",
    "runtime_lock_unsafe",
    "runtime_writable_metadata",
}


class DeploymentError(ValueError):
    """Content-free deployment error suitable for operator logs."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "deployment_gate_failed",
        operation: str | None = None,
        errno: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason, self.operation = reason, operation
        self.errno, self.exit_code = errno, exit_code


def safe_error(error: BaseException) -> dict:
    """Only fixed categories and numeric OS status; never message, argv or stderr."""
    result: dict[str, Any] = {"reason": "gate_failed"}
    number, exit_code = None, None
    if isinstance(error, DeploymentError):
        if isinstance(error.reason, str) and error.reason in ERROR_REASONS:
            result["reason"] = error.reason
        if (
            isinstance(error.operation, str)
            and error.operation in ACCOUNT_TOOLS.keys() | NATIVE_OPERATIONS
        ):
            result["operation"] = error.operation
        number, exit_code = error.errno, error.exit_code
    elif isinstance(error, OSError):
        result["reason"], number = "os_error", error.errno
    elif isinstance(error, subprocess.CalledProcessError):
        result["reason"], exit_code = "subprocess_exit", error.returncode
    elif isinstance(error, subprocess.TimeoutExpired):
        result["reason"] = "subprocess_timeout"
    if type(number) is int and 1 <= number <= 4095:
        result["errno"] = number
    if type(exit_code) is int and -128 <= exit_code <= 255:
        result["exit_code"] = exit_code
    return result


def account_tools_gate(*, root_uid: int = 0) -> None:
    # Check both before creating either account object. No PATH lookup or fallback.
    for operation, binary in ACCOUNT_TOOLS.items():
        try:
            if not metadata(Path(binary), root_uid, 0o755):
                raise FileNotFoundError(2, "account tool unavailable")
        except OSError as error:
            raise DeploymentError(
                "account tool unavailable",
                reason="account_tool_unavailable",
                operation=operation,
                errno=error.errno,
            ) from None
        except DeploymentError:
            raise DeploymentError(
                "account tool metadata rejected",
                reason="account_tool_metadata",
                operation=operation,
            ) from None


def account_command(operation: str, arguments: list[str]) -> None:
    try:
        subprocess.run(
            [ACCOUNT_TOOLS[operation], *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
            env=CLEAN_ENV,
        )
    except OSError as error:
        raise DeploymentError(
            "account tool unavailable",
            reason="account_tool_unavailable",
            operation=operation,
            errno=error.errno,
        ) from None
    except subprocess.CalledProcessError as error:
        raise DeploymentError(
            "account tool rejected operation",
            reason="account_tool_exit",
            operation=operation,
            exit_code=error.returncode,
        ) from None
    except subprocess.TimeoutExpired:
        raise DeploymentError(
            "account tool timed out", reason="account_tool_timeout", operation=operation
        ) from None


def credential_metadata(path: Path, *, root_uid: int = 0) -> dict:
    """Validate the nonsecret host-policy receipt without decrypting credentials."""
    spec = importlib.util.spec_from_file_location(
        "asus_deployment_credentials", Path(__file__).with_name("import_credentials.py")
    )
    if spec is None or spec.loader is None:
        raise DeploymentError("credential policy validator unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.validate_metadata(path, root_uid=root_uid)
    except (ValueError, OSError):
        raise DeploymentError("credential policy metadata rejected") from None


def strict_json(raw: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for name, value in items:
            if name in result:
                raise DeploymentError("duplicate JSON member rejected")
            result[name] = value
        return result

    def constant(value: str) -> None:
        raise DeploymentError("nonfinite JSON value rejected")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as exc:
        raise DeploymentError("JSON nesting exceeds bound") from exc


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/")

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def base(self) -> Path:
        return self.path("/opt/api-quota-broker")

    @property
    def config(self) -> Path:
        return self.path("/etc/api-quota-broker")

    @property
    def state(self) -> Path:
        return self.path("/var/lib/api-quota-broker")

    @property
    def backups(self) -> Path:
        return self.path("/var/backups/api-quota-broker")

    @property
    def unit(self) -> Path:
        return self.path("/etc/systemd/system/api-quota-broker.service")


def real_path(path: Path) -> None:
    """Reject symlinks in every existing component, including dangling links."""
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("absolute real path required", reason="unsafe_path")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise DeploymentError("symlink path rejected", reason="unsafe_path")


def metadata(
    path: Path, uid: int, mode: int, *, directory: bool = False, gid: int | None = None
) -> bool:
    real_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != mode
        or (gid is not None and info.st_gid != gid)
    ):
        raise DeploymentError(
            "existing path has unsafe ownership, permissions or type", reason="unsafe_metadata"
        )
    return True


def current_release(layout: Layout, *, root_uid: int = 0) -> str | None:
    current = layout.base / "current"
    real_path(current.parent)
    try:
        info = current.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != root_uid:
        raise DeploymentError("current must be a root-owned release selector")
    target = os.readlink(current)
    # The one deliberate symlink is confined to a direct release child.
    match = re.fullmatch(r"releases/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})", target)
    if match is None:
        raise DeploymentError("current release selector is outside releases")
    release = layout.base / target
    if not metadata(release, root_uid, 0o755, directory=True):
        raise DeploymentError("current release is missing")
    return match[1]


def inspect_layout(
    layout: Layout,
    *,
    root_uid: int = 0,
    service_uid: int | None = None,
    service_gid: int | None = None,
) -> dict:
    """Metadata only: never read a credential, configuration or ledger body."""
    missing = []
    directories = (
        (layout.base, root_uid, 0o755),
        (layout.base / "releases", root_uid, 0o755),
        (layout.config, root_uid, 0o750),
        (layout.config / "credentials", root_uid, 0o700),
    )
    for path, uid, mode in directories:
        if not metadata(path, uid, mode, directory=True):
            missing.append(str(path.relative_to(layout.root)))
    if service_gid is not None:
        metadata(layout.config, root_uid, 0o750, directory=True, gid=service_gid)
    if service_uid is None:
        missing.append("service_account")
    else:
        if not metadata(layout.state, service_uid, 0o700, directory=True):
            missing.append("state_directory")
        db = layout.state / "ledger.sqlite3"
        metadata(db, service_uid, 0o600)
        for suffix in ("-wal", "-shm", "-journal"):
            metadata(Path(str(db) + suffix), service_uid, 0o600)
    if not metadata(layout.config / "gateway.json", root_uid, 0o640, gid=service_gid):
        missing.append("gateway_config")
    for name in CREDENTIALS:
        if not metadata(layout.config / "credentials" / f"{name}.cred", root_uid, 0o600):
            missing.append(f"encrypted_credential:{name}")
    if not metadata(layout.config / "credentials/doppler-metadata.json", root_uid, 0o600):
        missing.append("credential_policy_metadata")
    return {"current_release": current_release(layout, root_uid=root_uid), "missing": missing}


def service_owner() -> tuple[int, int] | None:
    try:
        user = pwd.getpwnam("api-quota-broker")
    except KeyError:
        user = None
    try:
        group = grp.getgrnam("api-quota-broker")
    except KeyError:
        group = None
    if group is not None and (
        group.gr_gid == 0
        or set(group.gr_mem) - {"api-quota-broker"}
        or any(
            person.pw_name != "api-quota-broker" and person.pw_gid == group.gr_gid
            for person in pwd.getpwall()
        )
    ):
        raise DeploymentError(
            "existing service group is not dedicated", reason="service_group_not_dedicated"
        )
    if user is None:
        return None
    if (
        group is None
        or user.pw_uid == 0
        or group.gr_gid == 0
        or user.pw_gid != group.gr_gid
        or user.pw_dir != "/var/lib/api-quota-broker"
        or user.pw_shell != "/usr/sbin/nologin"
        or set(os.getgrouplist("api-quota-broker", group.gr_gid)) != {group.gr_gid}
        or any(
            person.pw_name != "api-quota-broker" and person.pw_uid == user.pw_uid
            for person in pwd.getpwall()
        )
    ):
        raise DeploymentError(
            "existing service account has an unsafe identity", reason="service_account_unsafe"
        )
    return user.pw_uid, group.gr_gid


def provision(layout: Layout, *, apply: bool = False, root_uid: int = 0) -> dict:
    """Create only approved accounts/directories; existing objects are never repaired."""
    owner = service_owner()
    directories = (
        (layout.base, root_uid, 0o755, None),
        (layout.base / "releases", root_uid, 0o755, None),
        (layout.config, root_uid, 0o750, owner[1] if owner else None),
        (layout.config / "credentials", root_uid, 0o700, None),
        (layout.backups, root_uid, 0o700, None),
    )
    missing = []
    for path, uid, mode, gid in directories:
        if not metadata(path, uid, mode, directory=True, gid=gid):
            missing.append(str(path.relative_to(layout.root)))
    real_path(layout.state)
    if layout.state.exists() and owner is None:
        raise DeploymentError(
            "existing state directory requires a verified service owner",
            reason="service_owner_required",
        )
    if owner is not None:
        metadata(layout.state, owner[0], 0o700, directory=True)
    inspect_layout(
        layout,
        root_uid=root_uid,
        service_uid=owner[0] if owner else None,
        service_gid=owner[1] if owner else None,
    )
    if not apply:
        return {
            "phase": "provision-plan",
            "missing_directories": missing,
            "service_account": bool(owner),
        }
    stopped_service()
    if owner is None:
        account_tools_gate(root_uid=root_uid)
        try:
            grp.getgrnam("api-quota-broker")
        except KeyError:
            account_command("groupadd", ["--system", "api-quota-broker"])
        account_command(
            "useradd",
            [
                "--system",
                "--gid",
                "api-quota-broker",
                "--home-dir",
                "/var/lib/api-quota-broker",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "api-quota-broker",
            ],
        )
        owner = service_owner()
        if owner is None:
            raise DeploymentError(
                "service account provisioning failed", reason="service_account_provision_failed"
            )
    for path, uid, mode, gid in (*directories, (layout.state, owner[0], 0o700, owner[1])):
        actual_gid = owner[1] if path == layout.config else gid
        if not metadata(path, uid, mode, directory=True, gid=actual_gid):
            # Parents are fixed OS directories or already validated broker dirs.
            real_path(path.parent)
            path.mkdir(mode=mode)
            os.chown(path, uid, actual_gid if actual_gid is not None else 0)
            os.chmod(path, mode)
    return {"phase": "provisioned", "activation": "disabled; no credentials or config created"}


def validate_staged_source(
    layout: Layout, archive: Path, digest: str, *, root_uid: int = 0
) -> tuple[Path, dict]:
    manifest = verify_artifact(archive, digest)
    release = layout.base / "releases" / manifest["artifact_id"]
    if not metadata(release, root_uid, 0o755, directory=True):
        raise DeploymentError("verified staged release is missing")
    receipt = release / "release-manifest.json"
    if not metadata(receipt, root_uid, 0o644) or file_hash(receipt) != digest:
        raise DeploymentError("staged source manifest hash mismatch")
    for record in manifest["release"]["files"]:
        path = release / record["path"]
        for directory in path.parents:
            if directory == release:
                break
            if not metadata(directory, root_uid, 0o755, directory=True):
                raise DeploymentError("staged source directory is missing")
        if (
            not metadata(path, root_uid, record["mode"])
            or path.stat().st_size != record["size"]
            or file_hash(path) != record["sha256"]
        ):
            raise DeploymentError("staged source payload hash mismatch")
    return release, manifest


def run_json(argv: list[str], timeout: float = 60) -> dict:
    completed = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        env=CLEAN_ENV,
    )
    if completed.returncode or len(completed.stdout) > 512 * 1024:
        raise DeploymentError("offline runtime validation failed")
    result = strict_json(completed.stdout)
    if not isinstance(result, dict):
        raise DeploymentError("offline runtime receipt is invalid")
    return result


def verify_runtime_bundle(release: Path, bundle: Path, digest: str, source_digest: str) -> dict:
    if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise DeploymentError("pinned runtime manifest digest required")
    real_path(bundle)
    receipt = run_json(
        [
            sys.executable,
            "-I",
            "-B",
            str(release / "scripts/prepare_asus_runtime.py"),
            "verify",
            "--bundle",
            str(bundle),
            "--manifest-sha256",
            digest,
            "--source-manifest-sha256",
            source_digest,
        ]
    )
    if (
        set(receipt)
        != {
            "schema_version",
            "status",
            "policy",
            "runtime_manifest_sha256",
            "source_payload_manifest_sha256",
            "project_wheel_sha256",
            "target",
            "files",
            "preparation",
        }
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["status"] != "verified"
        or receipt["policy"] != "asus-runtime-v1"
        or receipt["runtime_manifest_sha256"] != digest
        or receipt["source_payload_manifest_sha256"] != source_digest
    ):
        raise DeploymentError("runtime receipt does not match pinned source and bundle")
    return receipt


def trusted_bundle(bundle: Path, receipt: dict, *, root_uid: int = 0) -> None:
    # Verify all bytes first; require root ownership before executing bundled uv.
    for path in (bundle, *bundle.rglob("*")):
        real_path(path)
        info = path.lstat()
        if info.st_uid != root_uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise DeploymentError("runtime bundle is not root-owned and immutable to service")
    for parent in bundle.parents:
        info = parent.lstat()
        if info.st_uid not in {0, root_uid} or (
            stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX
        ):
            raise DeploymentError("runtime bundle parent is writable by another owner")
    for record in receipt["files"]:
        if not metadata(bundle / record["path"], root_uid, record["mode"]):
            raise DeploymentError("runtime bundle file is missing")
    for name in ("runtime-manifest.json", "SHA256SUMS"):
        if not metadata(bundle / name, root_uid, 0o644):
            raise DeploymentError("runtime bundle receipt file is missing")


def normalize_uv_lock(runtime: Path, *, root_uid: int = 0, apply: bool = False) -> dict:
    """Seal only uv's empty environment lock after installation has finished.

    uv 0.9.5 creates this file with mode 0777 despite umask 022. Never delete it,
    replace its inode, accept content/links, or relax the runtime inventory gate.
    """

    def reject() -> None:
        raise DeploymentError(
            "uv environment lock is unsafe or busy",
            reason="runtime_lock_unsafe",
            operation="uv_lock_normalize",
        )

    real_path(runtime)
    if not metadata(runtime, root_uid, 0o755, directory=True):
        reject()
    lock = runtime / ".lock"
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            mode = stat.S_IMODE(info.st_mode)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != root_uid
                or info.st_nlink != 1
                or info.st_size != 0
                or mode not in (0o777, 0o644)
            ):
                reject()
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if apply:
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            after = os.fstat(stream.fileno())
            current = lock.lstat()
            if (
                (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink, info.st_size)
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_uid,
                    after.st_gid,
                    after.st_nlink,
                    after.st_size,
                )
                or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size != 0
                or stat.S_IMODE(after.st_mode) != (0o644 if apply else mode)
            ):
                reject()
            return {
                "path": ".lock",
                "before_mode": mode,
                "after_mode": stat.S_IMODE(after.st_mode),
                "inode_preserved": True,
                "size": 0,
                "applied": apply,
            }
    except OSError:
        reject()
    raise AssertionError("unreachable")


def runtime_inventory(runtime: Path, *, root_uid: int = 0) -> list[dict]:
    real_path(runtime)
    if not runtime.is_dir():
        raise DeploymentError("native runtime is missing")
    aliases = {
        "bin/python": "/usr/bin/python3.14",
        "bin/python3": "python",
        "bin/python3.14": "python",
        "lib64": "lib",
    }
    records: list[dict] = []
    for path in chain((runtime,), runtime.rglob("*")):
        info = path.lstat()
        name = "." if path == runtime else path.relative_to(runtime).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != root_uid:
            raise DeploymentError("native runtime has a different owner")
        if stat.S_ISLNK(info.st_mode):
            if name not in aliases or os.readlink(path) != aliases[name]:
                raise DeploymentError("native runtime contains an unapproved symlink")
            records.append({"path": name, "kind": "symlink", "target": aliases[name]})
        elif mode & 0o022:
            raise DeploymentError(
                "native runtime is writable by another owner",
                reason="runtime_writable_metadata",
                operation="runtime_inventory",
            )
        elif stat.S_ISDIR(info.st_mode):
            if mode & 0o005 != 0o005:
                raise DeploymentError("native runtime directory is unreadable by service")
            records.append({"path": name, "kind": "directory", "mode": mode})
        elif stat.S_ISREG(info.st_mode) and info.st_size <= 256 * 1024 * 1024:
            if not mode & 0o004 or (name == "bin/python" and not mode & 0o001):
                raise DeploymentError("native runtime file is unreadable by service")
            records.append(
                {
                    "path": name,
                    "kind": "file",
                    "mode": mode,
                    "size": info.st_size,
                    "sha256": file_hash(path),
                }
            )
        else:
            raise DeploymentError("native runtime has an unsupported file type or size")
        if len(records) > 8192:
            raise DeploymentError("native runtime inventory exceeds bound")
    return sorted(records, key=lambda record: record["path"])


def native_smoke(release: Path, expected_target: dict) -> dict:
    system_python_gate()
    receipt = run_json(
        [
            str(release / "runtime/bin/python"),
            "-I",
            "-B",
            str(release / "scripts/prepare_asus_runtime.py"),
            "--target-smoke",
        ]
    )
    if (
        set(receipt)
        != {
            "schema_version",
            "status",
            "mode",
            "target",
            "checks",
            "provider_calls",
            "credential_reads",
        }
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["status"] != "passed"
        or receipt["mode"] != "target_smoke"
        or receipt["target"] != expected_target
        or type(receipt["provider_calls"]) is not int
        or receipt["provider_calls"] != 0
        or type(receipt["credential_reads"]) is not int
        or receipt["credential_reads"] != 0
        or receipt["checks"]
        != [
            "dependency_versions",
            "project_cli_import",
            "cffi",
            "fernet",
            "aesgcm",
            "pdf",
            "ssl_ca",
            "sqlite",
            "curl_present",
        ]
    ):
        raise DeploymentError("native runtime smoke gate failed")
    return receipt


def system_python_gate() -> None:
    python = Path("/usr/bin/python3.14")
    real_path(python)
    info = python.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
        or not info.st_mode & 0o111
    ):
        raise DeploymentError("reviewed system Python is unavailable or unsafe")


def activation(
    layout: Layout,
    archive: Path,
    source_digest: str,
    bundle: Path,
    runtime_digest: str,
    prepared_digest: str,
    config_digest: str,
    *,
    service_uid: int,
    service_gid: int,
    apply: bool = False,
    root_uid: int = 0,
) -> dict:
    """Validate every gate, then select a disabled release without starting it."""
    stopped_service()
    layout_report = inspect_layout(
        layout, root_uid=root_uid, service_uid=service_uid, service_gid=service_gid
    )
    if set(layout_report["missing"]) - {"gateway_config"}:
        raise DeploymentError("activation prerequisites are incomplete")
    credential_metadata(layout.config / "credentials/doppler-metadata.json", root_uid=root_uid)
    if not metadata(layout.backups, root_uid, 0o700, directory=True) or not metadata(
        layout.unit.parent, root_uid, 0o755, directory=True
    ):
        raise DeploymentError("activation requires verified unit and backup directories")
    metadata(layout.unit, root_uid, 0o644)
    release, manifest = validate_staged_source(layout, archive, source_digest, root_uid=root_uid)
    runtime_receipt = verify_runtime_bundle(release, bundle, runtime_digest, source_digest)
    trusted_bundle(bundle, runtime_receipt, root_uid=root_uid)
    validate_native_runtime(release, runtime_receipt, prepared_digest, root_uid=root_uid)
    config = release / "deploy/asus/gateway.disabled.json"
    if (
        re.fullmatch(r"[a-f0-9]{64}", config_digest) is None
        or file_hash(config) != config_digest
        or config.read_bytes() not in (b'{"targets":[]}', b'{"targets":[]}\n')
    ):
        raise DeploymentError("activation requires the pinned canonical disabled configuration")
    result = {
        "phase": "activation-plan",
        "candidate_release": manifest["artifact_id"],
        "source_manifest_sha256": source_digest,
        "runtime_manifest_sha256": runtime_digest,
        "prepared_runtime_sha256": prepared_digest,
        "config_sha256": config_digest,
        "activation": "stopped; start and enable require separate operator actions",
    }
    if not apply:
        return result
    stopped_service()
    snapshot = backup(layout, root_uid=root_uid)
    result["before_activation_backup"] = str(snapshot)
    journal = snapshot / "activation.json"
    state = {**result, "status": "validated", "completed_steps": []}
    write_receipt(journal, state)

    def completed(step: str) -> None:
        state["completed_steps"].append(step)
        temp = snapshot / (".activation." + uuid.uuid4().hex)
        write_receipt(temp, state)
        os.replace(temp, journal)

    try:
        replace_from_snapshot(config, layout.config / "gateway.json", 0o640, root_uid, service_gid)
        completed("config")
        replace_from_snapshot(
            release / "deploy/asus/api-quota-broker.service",
            layout.unit,
            0o644,
            root_uid,
            os.getgid(),
        )
        completed("unit")
        selector = layout.base / (".current.activate." + uuid.uuid4().hex)
        selector.symlink_to("releases/" + manifest["artifact_id"])
        os.replace(selector, layout.base / "current")
        completed("current")
        subprocess.run(
            ["systemctl", "daemon-reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
            env=CLEAN_ENV,
        )
        state["status"] = "activated-stopped"
        completed("daemon-reload")
    except Exception:
        state["status"] = "partial-failure; operator review required"
        completed("failure")
        raise
    result["phase"] = "activated-stopped"
    return result


def write_receipt(path: Path, value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise DeploymentError("prepared runtime receipt exceeds bound")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


def prepare_native_runtime(
    release: Path, bundle: Path, receipt: dict, *, apply: bool = False, root_uid: int = 0
) -> dict:
    runtime = release / "runtime"
    real_path(runtime)
    if runtime.exists() or (release / "runtime-prepared.json").exists():
        raise DeploymentError("runtime already exists; existing paths are preserved")
    uv = bundle / "tools/uv"
    commands = [
        [str(uv), "--version"],
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "venv",
            "--python",
            "/usr/bin/python3.14",
            "--no-python-downloads",
            str(runtime),
        ],
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "pip",
            "install",
            "--python",
            str(runtime / "bin/python"),
            "--no-index",
            "--find-links",
            str(bundle / "wheelhouse"),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "-r",
            str(bundle / "requirements.txt"),
        ],
    ]
    if not apply:
        return {"phase": "runtime-plan", "commands": commands, "activation": "disabled"}
    stopped_service()
    trusted_bundle(bundle, receipt, root_uid=root_uid)
    system_python_gate()
    version = subprocess.run(
        commands[0],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
        env=CLEAN_ENV,
    )
    if version.returncode or version.stdout.split()[:2] != [b"uv", b"0.9.5"]:
        raise DeploymentError("fixed uv native version gate failed")
    for argv in commands[1:]:
        subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=True,
            env=CLEAN_ENV,
            umask=0o022,
        )
    normalize_uv_lock(runtime, root_uid=root_uid, apply=True)
    runtime_inventory(runtime, root_uid=root_uid)
    smoke = native_smoke(release, receipt["target"])
    prepared = {
        "schema_version": 1,
        "policy": "asus-native-runtime-v1",
        "status": "prepared",
        "bundle": receipt,
        "native_smoke": smoke,
        "files": runtime_inventory(runtime, root_uid=root_uid),
    }
    digest = write_receipt(release / "runtime-prepared.json", prepared)
    return {
        "phase": "runtime-prepared",
        "prepared_runtime_sha256": digest,
        "activation": "disabled",
    }


def validate_native_runtime(
    release: Path, bundle_receipt: dict, digest: str, *, root_uid: int = 0
) -> dict:
    if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise DeploymentError("pinned prepared-runtime digest required")
    path = release / "runtime-prepared.json"
    if (
        not metadata(path, root_uid, 0o600)
        or path.stat().st_size > 2 * 1024 * 1024
        or file_hash(path) != digest
    ):
        raise DeploymentError("prepared-runtime receipt hash mismatch")
    receipt = strict_json(path.read_text())
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "policy", "status", "bundle", "native_smoke", "files"}
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["policy"] != "asus-native-runtime-v1"
        or receipt["status"] != "prepared"
        or receipt["bundle"] != bundle_receipt
        or receipt["files"] != runtime_inventory(release / "runtime", root_uid=root_uid)
        or receipt["native_smoke"] != native_smoke(release, bundle_receipt["target"])
    ):
        raise DeploymentError("prepared runtime files or native gate changed")
    return receipt


def verify_artifact(archive: Path, digest: str, *, extract: Path | None = None) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise DeploymentError("pinned manifest digest required")
    real_path(archive)
    if not archive.is_file():
        raise DeploymentError("regular release archive required")
    verifier = Path(__file__).resolve().parents[2] / "scripts" / "build_asus_release.py"
    argv = [sys.executable, "-I", "-B", str(verifier), "verify", str(archive), digest]
    if extract is not None:
        real_path(extract)
        if extract.exists():
            raise DeploymentError("staging destination already exists")
        argv.extend(["--extract", str(extract)])
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
            env=CLEAN_ENV,
        )
        if completed.returncode or len(completed.stdout) > 128 * 1024:
            raise DeploymentError("release verification failed")
        result = strict_json(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise DeploymentError("release verification failed") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"mode", "manifest"}
        or result["mode"] != ("extracted" if extract is not None else "verified")
        or not isinstance(result["manifest"], dict)
        or result["manifest"].get("artifact_id") != "release-" + digest
        or result["manifest"].get("payload_manifest_sha256") != digest
    ):
        raise DeploymentError("release verifier returned an invalid receipt")
    return result["manifest"]


def stopped_service() -> None:
    completed = subprocess.run(
        ["systemctl", "show", SERVICE, "--property=ActiveState", "--value"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        env=CLEAN_ENV,
    )
    if completed.returncode or completed.stdout.strip() not in (b"inactive", b"failed"):
        raise DeploymentError("broker must be stopped before backup or rollback")


def operator_gate() -> None:
    if os.geteuid() != 0 or not sys.stdin.isatty() or socket.gethostname() != HOST:
        raise DeploymentError("apply requires a root TTY on the reviewed ASUS host")


def stage_release(layout: Layout, archive: Path, digest: str, *, root_uid: int = 0) -> dict:
    """Install verified source bytes exclusively; never select or activate them."""
    stopped_service()
    if not metadata(layout.base, root_uid, 0o755, directory=True) or not metadata(
        layout.base / "releases", root_uid, 0o755, directory=True
    ):
        raise DeploymentError("root-owned release directories must already exist")
    manifest = verify_artifact(archive, digest)
    destination = layout.base / "releases" / manifest["artifact_id"]
    real_path(destination)
    if destination.exists():
        raise DeploymentError("release already exists; existing paths are preserved")
    # The verifier creates the destination exclusively with mode0700. A partial
    # failure remains private for operator inspection; it is never selected.
    extracted = verify_artifact(archive, digest, extract=destination)
    if extracted != manifest:
        raise DeploymentError("release verification receipt changed")
    for directory, _, files in os.walk(destination, followlinks=False):
        directory_path = Path(directory)
        for name in files:
            file = directory_path / name
            real_path(file)
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != root_uid:
                raise DeploymentError("extracted release ownership or type is invalid")
        os.chmod(directory_path, 0o755)
    stopped_service()
    return {"staged_release": manifest["artifact_id"], "activation": "disabled; current unchanged"}


def copy_exclusive(source: Path, destination: Path) -> None:
    real_path(source)
    real_path(destination)
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeploymentError("backup source must be regular")
        dst = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(dst, "wb") as target, os.fdopen(os.dup(src), "rb") as original:
            shutil.copyfileobj(original, target)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(src)


def file_hash(path: Path) -> str:
    real_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeploymentError("hash source must be regular")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup(layout: Layout, *, root_uid: int = 0) -> Path:
    """Snapshot the stopped ledger and encrypted keys; never decrypt or replay."""
    stopped_service()
    if not metadata(layout.backups, root_uid, 0o700, directory=True):
        raise DeploymentError("root-owned private backup directory must already exist")
    destination = layout.backups / uuid.uuid4().hex
    destination.mkdir(mode=0o700)
    old_release = current_release(layout, root_uid=root_uid)
    for name in CREDENTIALS:
        source = layout.config / "credentials" / f"{name}.cred"
        if not metadata(source, root_uid, 0o600):
            raise DeploymentError("encrypted credential is missing")
        copy_exclusive(source, destination / f"{name}.cred")
    policy = layout.config / "credentials/doppler-metadata.json"
    if not metadata(policy, root_uid, 0o600):
        raise DeploymentError("credential policy metadata is missing")
    copy_exclusive(policy, destination / "doppler-metadata.json")
    config_present = metadata(layout.config / "gateway.json", root_uid, 0o640)
    unit_present = metadata(layout.unit, root_uid, 0o644)
    if config_present:
        copy_exclusive(layout.config / "gateway.json", destination / "gateway.json")
    if unit_present:
        copy_exclusive(layout.unit, destination / SERVICE)
    db = layout.state / "ledger.sqlite3"
    real_path(db)
    if db.exists():
        backup_db = destination / "ledger.sqlite3"
        descriptor = os.open(backup_db, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        # mode=ro still reads SQLite WAL; no unsafe immutable snapshot or file-copy.
        with (
            closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(backup_db)) as copied,
        ):
            deadline = time.monotonic() + 30

            def bounded_progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() > deadline:
                    raise DeploymentError("backup deadline exceeded")

            original.backup(copied, pages=128, progress=bounded_progress)
            if copied.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise DeploymentError("backup integrity check failed")
    stopped_service()
    manifest = {
        "version": 2,
        "previous_release": old_release,
        "previous_config": config_present,
        "previous_unit": unit_present,
        "files": {path.name: file_hash(path) for path in destination.iterdir()},
    }
    receipt = destination / "backup.json"
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def backup_receipt(layout: Layout, snapshot: Path, *, root_uid: int = 0) -> dict:
    if snapshot.parent != layout.backups or not re.fullmatch(r"[a-f0-9]{32}", snapshot.name):
        raise DeploymentError("backup must be an existing private snapshot")
    if not metadata(snapshot, root_uid, 0o700, directory=True):
        raise DeploymentError("backup is missing")
    receipt = snapshot / "backup.json"
    if not metadata(receipt, root_uid, 0o600) or receipt.stat().st_size > 8192:
        raise DeploymentError("backup receipt is missing or oversized")
    result = strict_json(receipt.read_text())
    required = {f"{name}.cred" for name in CREDENTIALS} | {"doppler-metadata.json"}
    if (
        not isinstance(result, dict)
        or set(result)
        != {"version", "previous_release", "previous_config", "previous_unit", "files"}
        or type(result["version"]) is not int
        or result["version"] != 2
        or type(result["previous_config"]) is not bool
        or type(result["previous_unit"]) is not bool
        or not isinstance(result["files"], dict)
    ):
        raise DeploymentError("backup receipt is invalid")
    if result["previous_config"]:
        required.add("gateway.json")
    if result["previous_unit"]:
        required.add(SERVICE)
    if set(result["files"]) not in (required, required | {"ledger.sqlite3"}):
        raise DeploymentError("backup receipt file set is invalid")
    old = result["previous_release"]
    if old is not None and (not isinstance(old, str) or ARTIFACT_ID.fullmatch(old) is None):
        raise DeploymentError("backup release is invalid")
    for name, digest in result["files"].items():
        path = snapshot / name
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or not metadata(path, root_uid, 0o600)
            or file_hash(path) != digest
        ):
            raise DeploymentError("backup hash verification failed")
    return result


def replace_from_snapshot(source: Path, target: Path, mode: int, uid: int, gid: int) -> None:
    temporary = target.parent / ("." + target.name + ".rollback." + uuid.uuid4().hex)
    copy_exclusive(source, temporary)
    os.chmod(temporary, mode)
    os.chown(temporary, uid, gid)
    os.replace(temporary, target)


def rollback(
    layout: Layout, snapshot: Path, *, root_uid: int = 0, service_gid: int | None = None
) -> dict:
    """Restore code/config/unit, preserving current ledger and credential identity."""
    stopped_service()
    receipt = backup_receipt(layout, snapshot, root_uid=root_uid)
    old = receipt["previous_release"]
    if old is not None and not metadata(
        layout.base / "releases" / old, root_uid, 0o755, directory=True
    ):
        raise DeploymentError("previous release is missing")
    # Key rotation requires a separate reviewed recovery; never replace queue/HMAC keys.
    for name in CREDENTIALS:
        current = layout.config / "credentials" / f"{name}.cred"
        if (
            not metadata(current, root_uid, 0o600)
            or file_hash(current) != receipt["files"][f"{name}.cred"]
        ):
            raise DeploymentError("credential identity changed; rollback requires review")
    policy = layout.config / "credentials/doppler-metadata.json"
    if (
        not metadata(policy, root_uid, 0o600)
        or file_hash(policy) != receipt["files"]["doppler-metadata.json"]
    ):
        raise DeploymentError("credential policy identity changed; rollback requires review")
    selector = layout.base / "current"
    current_release(layout, root_uid=root_uid)
    config = layout.config / "gateway.json"
    config_present = metadata(config, root_uid, 0o640, gid=service_gid)
    unit_present = metadata(layout.unit, root_uid, 0o644)
    group = (
        service_gid
        if service_gid is not None
        else (config.stat().st_gid if config_present else os.getgid())
    )
    saved = backup(layout, root_uid=root_uid)
    if old is None:
        # First-install rollback has no release to activate; keep the broker stopped.
        subprocess.run(
            ["systemctl", "disable", SERVICE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
            env=CLEAN_ENV,
        )
    if receipt["previous_config"]:
        replace_from_snapshot(snapshot / "gateway.json", config, 0o640, root_uid, group)
    elif config_present:
        config.unlink()
    if receipt["previous_unit"]:
        replace_from_snapshot(snapshot / SERVICE, layout.unit, 0o644, root_uid, os.getgid())
    elif unit_present:
        layout.unit.unlink()
    if unit_present or receipt["previous_unit"]:
        subprocess.run(
            ["systemctl", "daemon-reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
            env=CLEAN_ENV,
        )
    if old is None:
        selector.unlink(missing_ok=True)
    else:
        temporary_selector = layout.base / (".current.rollback." + uuid.uuid4().hex)
        temporary_selector.symlink_to("releases/" + old)
        os.replace(temporary_selector, selector)
    return {"rollback_release": old, "before_rollback_backup": str(saved), "activation": "stopped"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    install = sub.add_parser("install")
    install.add_argument("archive", type=Path)
    install.add_argument("manifest_sha256")
    install_mode = install.add_mutually_exclusive_group()
    install_mode.add_argument("--dry-run", action="store_true")
    install_mode.add_argument("--apply", action="store_true")
    backup_parser = sub.add_parser("backup")
    backup_mode = backup_parser.add_mutually_exclusive_group()
    backup_mode.add_argument("--dry-run", action="store_true")
    backup_mode.add_argument("--apply", action="store_true")
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("snapshot", type=Path)
    rollback_mode = rollback_parser.add_mutually_exclusive_group()
    rollback_mode.add_argument("--dry-run", action="store_true")
    rollback_mode.add_argument("--apply", action="store_true")
    provision_parser = sub.add_parser("provision")
    provision_mode = provision_parser.add_mutually_exclusive_group()
    provision_mode.add_argument("--dry-run", action="store_true")
    provision_mode.add_argument("--apply", action="store_true")
    for action in ("prepare-runtime", "activate"):
        command = sub.add_parser(action)
        command.add_argument("archive", type=Path)
        command.add_argument("manifest_sha256")
        command.add_argument("--runtime-bundle", type=Path, required=True)
        command.add_argument("--runtime-manifest-sha256", required=True)
        if action == "activate":
            command.add_argument("--prepared-runtime-sha256", required=True)
            command.add_argument("--config-sha256", required=True)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply:
            operator_gate()
        owner = service_owner()
        layout = Layout()
        report = inspect_layout(
            layout,
            service_uid=owner[0] if owner else None,
            service_gid=owner[1] if owner else None,
        )
        if args.action == "install":
            report["artifact"] = verify_artifact(args.archive, args.manifest_sha256)
            report["activation"] = "disabled; native runtime and credential policy require review"
            if args.apply:
                report.update(stage_release(layout, args.archive, args.manifest_sha256))
        elif args.action == "provision":
            report.update(provision(layout, apply=args.apply))
        elif args.action == "prepare-runtime":
            release, _ = validate_staged_source(layout, args.archive, args.manifest_sha256)
            receipt = verify_runtime_bundle(
                release, args.runtime_bundle, args.runtime_manifest_sha256, args.manifest_sha256
            )
            report.update(
                prepare_native_runtime(release, args.runtime_bundle, receipt, apply=args.apply)
            )
        elif args.action == "activate":
            if owner is None:
                raise DeploymentError("activation requires the verified dedicated service owner")
            report.update(
                activation(
                    layout,
                    args.archive,
                    args.manifest_sha256,
                    args.runtime_bundle,
                    args.runtime_manifest_sha256,
                    args.prepared_runtime_sha256,
                    args.config_sha256,
                    service_uid=owner[0],
                    service_gid=owner[1],
                    apply=args.apply,
                )
            )
        elif args.action == "rollback":
            receipt = backup_receipt(layout, args.snapshot)
            report["rollback_release"] = receipt["previous_release"]
            report["activation"] = "stopped; ledger and credentials are never restored"
            if args.apply:
                if set(report["missing"]) - {"gateway_config"}:
                    raise DeploymentError("rollback prerequisites are incomplete")
                report.update(
                    rollback(layout, args.snapshot, service_gid=owner[1] if owner else None)
                )
        elif args.apply:
            if set(report["missing"]) - {"gateway_config"}:
                raise DeploymentError("backup prerequisites are incomplete")
            report["backup"] = str(backup(layout))
        print(json.dumps(report, sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "phase": args.action,
                    "automatic_retry": False,
                    **safe_error(error),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
