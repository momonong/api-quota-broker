"""Initial host-key credential import on the reviewed ASUS root TTY only.

Default invocation is a pure plan. Scope/access/expiry are human dashboard
attestations, never remote verification. No plaintext is written to disk and
failed private ciphertext stages are retained for operator review, not retried.
"""

import argparse
import getpass
import importlib.util
import json
import os
import re
import resource
import secrets
import socket
import stat
import subprocess
import sys
import uuid
import warnings
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HOST = "asus-ubuntu2604-server"
NAMES = ("digest_key", "client_token", "admin_token", "queue_key", "doppler_service_token")
METADATA = "doppler-metadata.json"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
TOKEN = re.compile(r"dp\.st\.dev\.[A-Za-z0-9_-]{32,256}\Z")
FAILURE = "credential initialization failed; retained ciphertext requires operator review"
EXPECTED_GROUP = b"0::/system.slice/api-quota-broker-install.service"
SWAP = "/sys/fs/cgroup/system.slice/api-quota-broker-install.service/memory.swap.max"
EXPIRY_REASONS = {"expiry_invalid", "expiry_past", "expiry_over30days"}
FAILURE_REASONS = EXPIRY_REASONS | {
    "credential_import_failed",
    "credential_os_error",
    "credential_tool_exit",
    "credential_tool_timeout",
    "credential_cancelled",
    "credential_eof",
    "tty_unavailable",
    "tty_noecho_failed",
}
OPERATIONS = {
    "operator_gate",
    "service_state",
    "expiry_validation",
    "initial_state",
    "stage_create",
    "stage_evidence",
    "systemd_creds_metadata",
    "host_key_check",
    "host_key_setup",
    "host_key_verify",
    "hidden_token",
    "token_validate",
    "key_generate",
    "credential_publish",
    "metadata_write",
} | {"encrypt_" + name for name in NAMES}


class CredentialError(ValueError):
    def __init__(
        self,
        reason: str = "credential_import_failed",
        *,
        operation: str | None = None,
        errno: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(FAILURE)
        self.reason = reason if reason in FAILURE_REASONS else "credential_import_failed"
        self.operation = operation if operation in OPERATIONS else None
        self.errno = errno if type(errno) is int and 1 <= errno <= 4095 else None
        self.exit_code = exit_code if type(exit_code) is int and -128 <= exit_code <= 255 else None


def failure_details(error: BaseException, operation: str | None = None) -> dict:
    """Fixed categories and bounded numeric status; never message, input, argv or stderr."""
    reason, number, exit_code = "credential_import_failed", None, None
    if isinstance(error, CredentialError):
        reason, number, exit_code = error.reason, error.errno, error.exit_code
        operation = operation or error.operation
    elif isinstance(error, OSError):
        reason, number = "credential_os_error", error.errno
    elif isinstance(error, subprocess.TimeoutExpired):
        reason = "credential_tool_timeout"
    elif isinstance(error, subprocess.CalledProcessError):
        reason, exit_code = "credential_tool_exit", error.returncode
    elif isinstance(error, KeyboardInterrupt):
        reason = "credential_cancelled"
    elif isinstance(error, EOFError):
        reason = "credential_eof"
    safe = CredentialError(reason, operation=operation, errno=number, exit_code=exit_code)
    return {
        key: value
        for key, value in {
            "reason": safe.reason,
            "operation": safe.operation,
            "errno": safe.errno,
            "exit_code": safe.exit_code,
        }.items()
        if value is not None
    }


def _tool() -> Any:
    path = Path(__file__).with_name("install.py")
    spec = importlib.util.spec_from_file_location("asus_credential_installer", path)
    if spec is None or spec.loader is None:
        raise CredentialError
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _kernel(path: str, bound: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CredentialError
        data = bytearray()
        while True:
            chunk = os.read(fd, bound + 1 - len(data))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > bound:
                raise CredentialError
    finally:
        os.close(fd)


def operator_gate() -> None:
    if (
        os.geteuid() != 0
        or not sys.stdin.isatty()
        or socket.gethostname() != HOST
        or _kernel("/proc/self/cgroup", 4096) not in (EXPECTED_GROUP, EXPECTED_GROUP + b"\n")
        or _kernel(SWAP, 32) not in (b"0", b"0\n")
        or resource.getrlimit(resource.RLIMIT_CORE) != (0, 0)
    ):
        raise CredentialError


def parse_utc(value: str) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None
    ):
        raise CredentialError("expiry_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise CredentialError("expiry_invalid") from None


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_expiry(expiry: datetime, now: datetime) -> None:
    if (
        not isinstance(now, datetime)
        or not isinstance(expiry, datetime)
        or now.tzinfo is None
        or expiry.tzinfo is None
        or expiry.microsecond != 0
    ):
        raise CredentialError("expiry_invalid")
    if expiry <= now:
        raise CredentialError("expiry_past")
    created = now.astimezone(UTC).replace(microsecond=0)
    if expiry - created > timedelta(days=30):
        raise CredentialError("expiry_over30days")


def metadata_record(expiry: datetime, *, human_attested: bool, now: datetime) -> dict:
    if human_attested is not True:
        raise CredentialError
    validate_expiry(expiry, now)
    created = now.astimezone(UTC).replace(microsecond=0)
    return {
        "schema_version": 1,
        "credential_policy": "host",
        "approval_user_message_id": "01a0ffaa-84ae-7030-b193-a004ca233d8e",
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "token_name": "asus-api-quota-broker",
        "created_at": stamp(created),
        "expires_at": stamp(expiry),
        "created_at_source": "local_import_clock",
        "human_dashboard_attested": True,
        "remote_scope_verified": False,
        "evidence": "human_dashboard_attestation_only",
        "local_keys_expire": False,
    }


def _validate_metadata(path: Path, *, now: datetime | None = None, root_uid: int = 0) -> dict:
    tool = _tool()
    if not tool.metadata(path, root_uid, 0o600) or path.stat().st_size > 8192:
        raise CredentialError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != root_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CredentialError
        data = stream.read(8193)
    if len(data) > 8192:
        raise CredentialError
    record = tool.strict_json(data)
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("created_at"), str)
        or not isinstance(record.get("expires_at"), str)
    ):
        raise CredentialError
    created, expiry = parse_utc(record["created_at"]), parse_utc(record["expires_at"])
    clock = now or datetime.now(UTC)
    expected = metadata_record(expiry, human_attested=True, now=created)
    if (
        record != expected
        or type(record.get("schema_version")) is not int
        or record.get("human_dashboard_attested") is not True
        or record.get("remote_scope_verified") is not False
        or record.get("local_keys_expire") is not False
        or not isinstance(clock, datetime)
        or clock.tzinfo is None
        or created > clock
        or expiry <= clock
    ):
        raise CredentialError
    return record


def validate_metadata(path: Path, *, now: datetime | None = None, root_uid: int = 0) -> dict:
    try:
        return _validate_metadata(path, now=now, root_uid=root_uid)
    except (ValueError, OSError, UnicodeError):
        raise CredentialError from None


def plan() -> dict:
    return {
        "phase": "credential-plan",
        "credential_policy": "host",
        "credentials": list(NAMES),
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "token_name": "asus-api-quota-broker",
        "maximum_token_days": 30,
        "remote_scope_verified": False,
        "local_keys_expire": False,
        "apply_requires": "reviewed ASUS root TTY in api-quota-broker-install.service, swap.max=0, core=0; stopped broker; empty credentials and no ledger",
    }


def inspect_initial_state(layout: Any = None, *, root_uid: int = 0, installer: Any = None) -> dict:
    tool = installer if installer is not None else _tool()
    layout = layout or tool.Layout()
    owner = tool.service_owner()
    if owner is None:
        raise CredentialError
    if (
        not tool.metadata(layout.config, root_uid, 0o750, directory=True, gid=owner[1])
        or not tool.metadata(layout.config / "credentials", root_uid, 0o700, directory=True)
        or not tool.metadata(layout.state, owner[0], 0o700, directory=True)
        or any((layout.config / "credentials").iterdir())
        or any(layout.config.glob(".credential-init.*"))
        or any(layout.state.iterdir())
    ):
        raise CredentialError
    for name in (
        "ledger.sqlite3",
        "ledger.sqlite3-wal",
        "ledger.sqlite3-shm",
        "ledger.sqlite3-journal",
    ):
        path = layout.state / name
        tool.real_path(path)
        if path.exists():
            raise CredentialError
    return {"phase": "credential-initial-ready", "credential_policy": "host"}


def check_host_key(path: Path, *, root_uid: int = 0, installer: Any = None) -> bool:
    # Metadata only; never open/read/hash credential.secret.
    (installer if installer is not None else _tool()).real_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != root_uid
        or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
        or info.st_nlink != 1
        or info.st_size <= 0
    ):
        raise CredentialError
    return True


def hidden_token() -> str:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        # A text update stream uses BufferedRandom and requires seekability.
        # getpass opens its own terminal input; our stream is prompt output only.
        with os.fdopen(fd, "w", buffering=1) as tty:
            if not os.isatty(tty.fileno()):
                raise CredentialError
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass(
                    "Doppler ASUS read-only Service Token (hidden): ", stream=tty
                )
    except getpass.GetPassWarning:
        raise CredentialError("tty_noecho_failed", operation="hidden_token") from None
    except OSError as error:
        raise CredentialError(
            "tty_unavailable", operation="hidden_token", errno=error.errno
        ) from None


def _write(path: Path, value: dict, *, replace: bool = False) -> None:
    destination = path.with_name(".evidence." + uuid.uuid4().hex) if replace else path
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(destination, path)


def initialize(
    expiry: datetime,
    *,
    human_attested: bool,
    token_reader: Callable[[], str] | None = None,
    layout: Any = None,
    now: datetime | None = None,
    root_uid: int = 0,
    installer: Any = None,
    allow_host_key_setup: bool = True,
) -> dict:
    stage = None
    operation = "operator_gate"
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "initializing",
        "credential_policy": "host",
        "encrypted": [],
    }
    try:
        operator_gate()
        tool = installer if installer is not None else _tool()
        layout = layout or tool.Layout()
        operation = "service_state"
        tool.stopped_service()
        operation = "expiry_validation"
        record = metadata_record(
            expiry, human_attested=human_attested, now=now or datetime.now(UTC)
        )
        operation = "initial_state"
        inspect_initial_state(layout, root_uid=root_uid, installer=tool)
        directory = layout.config / "credentials"
        placeholder = directory.lstat()
        stage = layout.config / (".credential-init." + uuid.uuid4().hex)
        operation = "stage_create"
        stage.mkdir(mode=0o700)
        operation = "stage_evidence"
        _write(stage / "import-evidence.json", evidence)
        binary = Path("/usr/bin/systemd-creds")
        operation = "systemd_creds_metadata"
        if not tool.metadata(binary, 0, 0o755):
            raise CredentialError
        host_key = layout.path("/var/lib/systemd/credential.secret")
        operation = "host_key_check"
        if not check_host_key(host_key, root_uid=root_uid, installer=tool):
            if not allow_host_key_setup:
                raise CredentialError
            operation = "host_key_setup"
            subprocess.run(
                [str(binary), "setup"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
                env=CLEAN_ENV,
                umask=0o077,
            )
        operation = "host_key_verify"
        if not check_host_key(host_key, root_uid=root_uid, installer=tool):
            raise CredentialError
        operation = "hidden_token"
        token = (token_reader or hidden_token)()
        operation = "token_validate"
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            raise CredentialError
        operation = "key_generate"
        values = {
            "digest_key": secrets.token_bytes(32),
            "queue_key": secrets.token_bytes(32),
            "client_token": secrets.token_urlsafe(32).encode(),
            "admin_token": secrets.token_urlsafe(32).encode(),
            "doppler_service_token": token.encode("ascii"),
        }
        if (
            len(values["digest_key"]) != 32
            or len(values["queue_key"]) != 32
            or len(set(values.values())) != len(values)
            or any(len(values[name]) < 32 for name in NAMES[:-1])
        ):
            raise CredentialError
        for name in NAMES:
            operation = "encrypt_" + name
            argv = [str(binary), "--with-key=host", "--name=" + name]
            if name == "doppler_service_token":
                argv.append(
                    "--not-after=" + expiry.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                )
            destination = stage / (name + ".cred")
            argv.extend(["encrypt", "-", str(destination)])
            subprocess.run(
                argv,
                input=values[name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
                env=CLEAN_ENV,
                umask=0o077,
            )
            if (
                not tool.metadata(destination, root_uid, 0o600)
                or not 0 < destination.stat().st_size <= 65536
            ):
                raise CredentialError
            fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != root_uid
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= 65536
                ):
                    raise CredentialError
                os.fsync(fd)
            finally:
                os.close(fd)
            evidence["encrypted"].append(name)
            _write(stage / "import-evidence.json", evidence, replace=True)
        operation = "metadata_write"
        _write(stage / METADATA, record)
        operation = "credential_publish"
        tool.stopped_service()
        # Repeat all initial checks without treating our private stage as a retry.
        if (
            not tool.metadata(directory, root_uid, 0o700, directory=True)
            or any(directory.iterdir())
            or any(layout.state.iterdir())
            or directory.lstat().st_ino != placeholder.st_ino
            or directory.lstat().st_dev != placeholder.st_dev
        ):
            raise CredentialError
        for name in (
            "ledger.sqlite3",
            "ledger.sqlite3-wal",
            "ledger.sqlite3-shm",
            "ledger.sqlite3-journal",
        ):
            path = layout.state / name
            tool.real_path(path)
            if path.exists():
                raise CredentialError
        evidence["status"] = "complete"
        _write(stage / "import-evidence.json", evidence, replace=True)
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(stage, directory)
        fd = os.open(layout.config, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {
            "phase": "credentials-initialized",
            "credential_policy": "host",
            "metadata": record,
            "activation": "stopped; no service changed",
        }
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - only fixed diagnostics cross this boundary
        details = failure_details(error, operation)
        if stage is not None and stage.exists():
            evidence["status"] = "failed; operator review required; no retry"
            evidence["failure"] = details
            try:
                _write(stage / "import-evidence.json", evidence, replace=True)
            except OSError:
                pass
        raise CredentialError(**details) from None


def interactive_initialize(
    *,
    installer: Any = None,
    clock: Callable[[], datetime] | None = None,
    allow_host_key_setup: bool = True,
) -> dict:
    """Prompt one reviewed root TTY session; return only nonsecret metadata."""
    try:
        operator_gate()
        clock = clock or (lambda: datetime.now(UTC))
        for attempt in range(3):
            try:
                expiry = parse_utc(input("Dashboard Expires UTC (YYYY-MM-DDTHH:MM:SSZ): "))
                validate_expiry(expiry, clock())
                break
            except CredentialError as error:
                print(
                    json.dumps(
                        {
                            "phase": "credential_expiry",
                            "status": "rejected",
                            "reason": error.reason,
                            "attempts_remaining": 2 - attempt,
                        }
                    ),
                    flush=True,
                )
                if attempt == 2:
                    raise
        attested = (
            input(
                "Confirm Dashboard api-quota-broker/dev read-only, ASUS dedicated token, <=30 days (type ATTEST): "
            )
            == "ATTEST"
        )
        return initialize(
            expiry,
            human_attested=attested,
            installer=installer,
            allow_host_key_setup=allow_host_key_setup,
        )
    except CredentialError:
        raise
    except Exception:  # noqa: BLE001 - interactive failures must never reflect token-bearing exceptions
        raise CredentialError from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if not args.apply:
            result = plan()
        else:
            result = interactive_initialize()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CredentialError, OSError, EOFError):
        print(FAILURE, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
