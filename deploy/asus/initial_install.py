"""One reviewed root TTY session for the initial ASUS broker installation.

The default is a pure plan. Secrets enter only the separate interactive importer.
Failure preserves state and receipts, stops this broker if start was attempted,
and never retries, restores a ledger, or alters another service.
"""

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import re
import resource
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HOST = "asus-ubuntu2604-server"
VERIFIER_SHA = "f7f66dc6cb7ed7f1298474890a69e16050e2ee937d05ea256ab906a976456858"
SERVICE = "api-quota-broker.service"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
PHASES = (
    "preflight",
    "source_verify",
    "provision",
    "source_stage",
    "native_runtime",
    "credential_import",
    "activation",
    "unit_verify",
    "start",
    "initial_acceptance",
    "stop_cleanup_backup",
    "restart",
    "restart_acceptance",
    "enable",
    "complete",
)


class OperatorError(ValueError):
    pass


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise OperatorError
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def private_file(path: Path, *, owner: int = 0) -> bytes:
    for component in (*reversed(path.parents), path):
        if stat.S_ISLNK(component.lstat().st_mode):
            raise OperatorError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 128 * 1024 * 1024
        ):
            raise OperatorError
        raw = file.read(info.st_size + 1)
        if len(raw) != info.st_size:
            raise OperatorError
        return raw


def operator_gate() -> None:
    if os.geteuid() != 0 or socket.gethostname() != HOST or not sys.stdin.isatty():
        raise OperatorError
    if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
        raise OperatorError
    raw = Path("/proc/self/cgroup").read_bytes()
    if raw not in (
        b"0::/system.slice/api-quota-broker-install.service",
        b"0::/system.slice/api-quota-broker-install.service\n",
    ):
        raise OperatorError
    path = Path("/sys/fs/cgroup/system.slice/api-quota-broker-install.service/memory.swap.max")
    if path.read_bytes() not in (b"0", b"0\n"):
        raise OperatorError


def command(argv: list[str], *, timeout: float = 20, capture: bool = False) -> bytes:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        env=ENV,
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv)
    if capture and len(result.stdout) > 4096:
        raise OperatorError
    return result.stdout if capture else b""


def orderflow() -> dict[str, Any]:
    raw = command(
        [
            "/usr/bin/systemctl",
            "show",
            "orderflow.service",
            "--property=ActiveState",
            "--property=NRestarts",
        ],
        capture=True,
    )
    values = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
    if values.get("ActiveState") != "active" or not re.fullmatch(
        r"[0-9]{1,9}", values.get("NRestarts", "")
    ):
        raise OperatorError
    connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=5)
    try:
        connection.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
        if connection.getresponse().status != 200:
            raise OperatorError
    finally:
        connection.close()
    return {"active": True, "restarts": int(values["NRestarts"]), "http_status": 200}


def ready() -> None:
    # Only unauthenticated loopback readiness is polled; no task is submitted.
    deadline = time.monotonic() + 25
    while (remaining := deadline - time.monotonic()) > 0:
        connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=min(1, remaining))
        try:
            connection.request("GET", "/v1/diagnostics")
            if connection.getresponse().status == 401:
                return
        except OSError:
            pass
        finally:
            connection.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    raise OperatorError


def accepted(report: dict[str, Any]) -> None:
    if (
        report.get("status") != "passed"
        or report.get("mode") != "initial"
        or not isinstance(report.get("database"), dict)
        or report["database"].get("quick_check") != "ok"
    ):
        raise OperatorError


def deployment(args: argparse.Namespace) -> dict[str, Any]:
    phase = "preflight"
    started = False
    journal = None
    installer = None
    record: dict[str, Any] = {
        "schema_version": 1,
        "mode": "initial_install",
        "status": "running",
        "completed_phases": [],
        "provider_calls": 0,
    }

    def checkpoint(name: str, **values: Any) -> None:
        record.update(values)
        record["completed_phases"].append(name)
        if journal is not None:
            temporary = journal.with_name("." + journal.name + "." + uuid.uuid4().hex)
            installer.write_receipt(temporary, record)
            os.replace(temporary, journal)
        print(json.dumps({"phase": name, "status": "passed"}, sort_keys=True), flush=True)

    try:
        operator_gate()
        for value in (args.source_manifest_sha256, args.runtime_manifest_sha256):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise OperatorError
        bootstrap = Path(__file__).resolve().parents[2]
        verifier_path = bootstrap / "scripts/build_asus_release.py"
        if hashlib.sha256(private_file(verifier_path)).hexdigest() != VERIFIER_SHA:
            raise OperatorError
        before_orderflow = orderflow()
        phase = "source_verify"
        verifier = load_module(verifier_path, "operator_release_verifier")
        artifact = verifier.verify_archive(private_file(args.archive), args.source_manifest_sha256)
        expected = {row["path"]: row["sha256"] for row in artifact["release"]["files"]}
        for name in ("deploy/asus/initial_install.py", "deploy/asus/install.py"):
            if hashlib.sha256(private_file(bootstrap / name)).hexdigest() != expected[name]:
                raise OperatorError
        installer = load_module(bootstrap / "deploy/asus/install.py", "operator_installer")
        layout = installer.Layout()
        installer.stopped_service()
        if installer.current_release(layout) is not None:
            raise OperatorError
        for suffix in ("", "-wal", "-shm", "-journal"):
            if (layout.state / ("ledger.sqlite3" + suffix)).exists():
                raise OperatorError
        if layout.unit.exists() or (layout.config / "gateway.json").exists():
            raise OperatorError
        # The source and bootstrap helpers are now verified. Record a failure
        # even when provisioning never creates the official backups directory.
        info = bootstrap.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OperatorError
        journal = bootstrap / ("deployment-" + uuid.uuid4().hex + ".json")
        record["bootstrap_journal"] = str(journal)
        checkpoint(phase)
        phase = "provision"
        installer.provision(layout, apply=True)
        journal = layout.backups / ("deployment-" + uuid.uuid4().hex + ".json")
        checkpoint(
            phase,
            source_payload_manifest_sha256=args.source_manifest_sha256,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
        )
        phase = "source_stage"
        installer.stage_release(layout, args.archive, args.source_manifest_sha256)
        release, _ = installer.validate_staged_source(
            layout, args.archive, args.source_manifest_sha256
        )
        checkpoint(phase)
        # Metadata validation loads its sibling importer. Use the fully verified
        # release after staging, instead of an incomplete bootstrap directory.
        installer = load_module(release / "deploy/asus/install.py", "operator_installer")
        phase = "native_runtime"
        bundle = installer.verify_runtime_bundle(
            release, args.runtime_bundle, args.runtime_manifest_sha256, args.source_manifest_sha256
        )
        prepared = installer.prepare_native_runtime(
            release, args.runtime_bundle, bundle, apply=True
        )
        checkpoint(phase, prepared_runtime_sha256=prepared["prepared_runtime_sha256"])
        phase = "credential_import"
        credentials = load_module(
            release / "deploy/asus/import_credentials.py", "operator_credentials"
        )
        initialized = credentials.interactive_initialize()
        if initialized.get("phase") != "credentials-initialized":
            raise OperatorError
        checkpoint(phase, credential_metadata=initialized["metadata"])
        phase = "activation"
        owner = installer.service_owner()
        if owner is None:
            raise OperatorError
        activated = installer.activation(
            layout,
            args.archive,
            args.source_manifest_sha256,
            args.runtime_bundle,
            args.runtime_manifest_sha256,
            prepared["prepared_runtime_sha256"],
            expected["deploy/asus/gateway.disabled.json"],
            service_uid=owner[0],
            service_gid=owner[1],
            apply=True,
        )
        checkpoint(phase, before_activation_backup=activated["before_activation_backup"])
        phase = "unit_verify"
        command(["/usr/bin/systemd-analyze", "verify", str(layout.unit)], timeout=30)
        checkpoint(phase)
        phase = "start"
        started = True
        command(["/usr/bin/systemctl", "start", SERVICE], timeout=45)
        ready()
        checkpoint(phase)
        acceptance = load_module(release / "deploy/asus/acceptance.py", "operator_acceptance")
        phase = "initial_acceptance"
        first = acceptance.run_checks(initial=True)
        record["initial_acceptance"] = first
        accepted(first)
        checkpoint(phase, initial_acceptance=first)
        phase = "stop_cleanup_backup"
        command(["/usr/bin/systemctl", "stop", SERVICE], timeout=45)
        if Path("/run/api-quota-broker").exists():
            raise OperatorError
        snapshot = installer.backup(layout)
        checkpoint(phase, stopped_backup=str(snapshot), runtime_queue_key_cleared=True)
        phase = "restart"
        command(["/usr/bin/systemctl", "start", SERVICE], timeout=45)
        ready()
        checkpoint(phase)
        phase = "restart_acceptance"
        second = acceptance.run_checks(initial=True)
        record["restart_acceptance"] = second
        accepted(second)
        if first["database"] != second["database"]:
            raise OperatorError
        if orderflow() != before_orderflow:
            raise OperatorError
        checkpoint(phase, restart_acceptance=second, orderflow_unchanged=True)
        phase = "enable"
        command(["/usr/bin/systemctl", "enable", SERVICE], timeout=20)
        if (
            command(["/usr/bin/systemctl", "is-enabled", SERVICE], capture=True).strip()
            != b"enabled"
        ):
            raise OperatorError
        checkpoint(phase)
        phase = "complete"
        record.update(
            status="passed",
            phase=phase,
            enabled=True,
            targets_enabled=False,
            representative_provider_verified=False,
            journal=str(journal),
        )
        checkpoint(phase)
        return record
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - secret-safe operator boundary
        # Never expose third-party stderr, exception messages, inputs or credentials.
        if started:
            try:
                command(["/usr/bin/systemctl", "stop", SERVICE], timeout=45)
            except (OSError, OperatorError, subprocess.SubprocessError):
                pass
        record.update(
            status="failed",
            phase=phase,
            reason="cancelled" if isinstance(exc, KeyboardInterrupt) else "gate_failed",
            automatic_retry=False,
            state_preserved=True,
            start_attempted=started,
        )
        if installer is not None and not isinstance(exc, KeyboardInterrupt):
            record.update(installer.safe_error(exc))
        if journal is not None and installer is not None:
            try:
                temporary = journal.with_name("." + journal.name + "." + uuid.uuid4().hex)
                installer.write_receipt(temporary, record)
                os.replace(temporary, journal)
                record["journal"] = str(journal)
            except (OSError, ValueError):
                pass
        return record


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise OperatorError


def main(argv: list[str] | None = None) -> int:
    parser = Parser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--runtime-bundle", type=Path)
    parser.add_argument("--runtime-manifest-sha256")
    try:
        args = parser.parse_args(argv)
        if not args.apply:
            result = {
                "schema_version": 1,
                "mode": "dry_plan",
                "phases": list(PHASES),
                "required": [
                    "ASUS root TTY",
                    "pinned root bootstrap",
                    "swap-disabled installer cgroup",
                    "dashboard read-only token and UTC expiration",
                ],
                "credential_reads": 0,
                "provider_calls": 0,
                "service_changes": 0,
            }
        elif any(
            getattr(args, name) is None
            for name in (
                "archive",
                "source_manifest_sha256",
                "runtime_bundle",
                "runtime_manifest_sha256",
            )
        ):
            raise OperatorError
        else:
            result = deployment(args)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result.get("status") != "failed" else 1
    except (OperatorError, OSError, ValueError):
        print('{"status":"failed","phase":"options","reason":"invalid_options"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
