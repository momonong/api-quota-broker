"""One-shot recovery of the specific r5 ASUS empty-uv-lock failure.

Default is a pure plan. Frozen r5 source, partial runtime, accounts and old
receipts are retained. No provision, source staging, reinstall or replay occurs.
The root-private externally pinned helper seals one empty lock, then continues
the previously approved credential/activation/initial/restart acceptance flow.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import socket
import stat
import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

HOST = "asus-ubuntu2604-server"
BOOTSTRAP = Path("/var/tmp/api-quota-broker-bootstrap.6UJytmEW")
FAILED_JOURNAL = Path(
    "/var/backups/api-quota-broker/deployment-0f3a1ef38400492c819f12ea9b90bdb9.json"
)
SOURCE_SHA = "86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
ARCHIVE_SHA = "a759096bb0cecfbec6a49d35be77e3930de08e2dcd971dfb95ad5f03568856b3"
BUNDLE_SHA = "abc9343db75505e579b0f74a30fbfe273e697d6202a71888d14f9fe1a3c8db8b"
VERIFIER_SHA = "f7f66dc6cb7ed7f1298474890a69e16050e2ee937d05ea256ab906a976456858"
OLD_INSTALLER_SHA = "af621bba02a50cc3d2795d5130eb970a54a4ee78e92777a1b78548561f6e23c6"
PATCHED_INSTALLER_SHA = "01149729eda673a3eef27c22a38bf4e19f462373b37f84df958221ab231bf2e3"
REFERENCE_SHA = "4da182123f2aa01f9cbd72481f4e94c6126c5edc5a802080aafb9aa517a0767e"
SERVICE = "api-quota-broker.service"


class RecoveryError(ValueError):
    pass


def private_bytes(path: Path, *, owner: int = 0, bound: int = 128 * 1024 * 1024) -> bytes:
    for component in (*reversed(path.parents), path):
        if stat.S_ISLNK(component.lstat().st_mode):
            raise RecoveryError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > bound
        ):
            raise RecoveryError
        raw = stream.read(info.st_size + 1)
        if len(raw) != info.st_size:
            raise RecoveryError
        return raw


def pinned_private(path: Path, digest: str) -> bytes:
    raw = private_bytes(path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RecoveryError
    return raw


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RecoveryError
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def operator_gate() -> None:
    if os.geteuid() != 0 or socket.gethostname() != HOST or not sys.stdin.isatty():
        raise RecoveryError
    if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
        raise RecoveryError
    cgroup = Path("/proc/self/cgroup").read_bytes()
    if cgroup.rstrip(b"\n") != b"0::/system.slice/api-quota-broker-install.service":
        raise RecoveryError
    directory = Path("/sys/fs/cgroup/system.slice/api-quota-broker-install.service")
    if (directory / "memory.swap.max").read_bytes().strip() != b"0" or (
        directory / "memory.max"
    ).read_bytes().strip() != b"402653184":
        raise RecoveryError
    cpu = (directory / "cpu.max").read_bytes().split()
    if len(cpu) != 2 or int(cpu[0]) * 2 != int(cpu[1]):
        raise RecoveryError


def validate_failed_receipt(raw: bytes, tool: Any) -> dict:
    receipt = tool.strict_json(raw)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("mode") != "initial_install"
        or receipt.get("status") != "failed"
        or receipt.get("phase") != "native_runtime"
        or receipt.get("completed_phases") != ["source_verify", "provision", "source_stage"]
        or receipt.get("source_payload_manifest_sha256") != SOURCE_SHA
        or receipt.get("runtime_manifest_sha256") != BUNDLE_SHA
        or receipt.get("start_attempted") is not False
        or receipt.get("provider_calls") != 0
        or receipt.get("state_preserved") is not True
        or receipt.get("reason") != "deployment_gate_failed"
    ):
        raise RecoveryError
    return receipt


def validate_precredential_layout(report: dict, credential_names: tuple[str, ...]) -> None:
    expected_missing = {"gateway_config", "credential_policy_metadata"} | {
        "encrypted_credential:" + name for name in credential_names
    }
    if (
        report.get("current_release") is not None
        or set(report.get("missing", [])) != expected_missing
    ):
        raise RecoveryError


def verify_installed_runtime(
    runtime: Path, bundle: Path, receipt: dict, reference: dict, tool: Any, *, root_uid: int = 0
) -> dict:
    """Compare all 288 files to the pre-repair snapshot and wheel payload bytes.

    The independent snapshot covers uv-generated activation/entrypoint/.pth
    files, which wheels do not contain. It was taken from root-owned immutable
    files; the only writable file was the known empty .lock, excluded here.
    """
    if not isinstance(reference, dict) or len(reference) != 288:
        raise RecoveryError
    tool.normalize_uv_lock(runtime, root_uid=root_uid)  # Metadata-only, no content read/write.
    observed = {}
    for path in (runtime, *runtime.rglob("*")):
        info = path.lstat()
        name = "." if path == runtime else path.relative_to(runtime).as_posix()
        if info.st_uid != root_uid:
            raise RecoveryError
        if name == ".lock":
            continue
        if stat.S_ISLNK(info.st_mode):
            aliases = {
                "bin/python": "/usr/bin/python3.14",
                "bin/python3": "python",
                "bin/python3.14": "python",
                "lib64": "lib",
            }
            if aliases.get(name) != os.readlink(path):
                raise RecoveryError
        elif stat.S_IMODE(info.st_mode) & 0o022:
            raise RecoveryError
        elif stat.S_ISREG(info.st_mode):
            observed[name] = tool.file_hash(path)
        elif not stat.S_ISDIR(info.st_mode):
            raise RecoveryError
    if observed != reference:
        raise RecoveryError
    verified = 0
    wheels = [row for row in receipt["files"] if row["path"].endswith(".whl")]
    if len(wheels) != 5:
        raise RecoveryError
    for row in wheels:
        wheel = bundle / row["path"]
        if tool.file_hash(wheel) != row["sha256"]:
            raise RecoveryError
        with zipfile.ZipFile(wheel) as archive:
            seen = set()
            for member in archive.infolist():
                name = member.filename
                pieces = PurePosixPath(name).parts
                if (
                    name in seen
                    or not pieces
                    or ".." in pieces
                    or name.startswith("/")
                    or "\\" in name
                ):
                    raise RecoveryError
                seen.add(name)
                if member.is_dir():
                    continue
                if ".data" in pieces or member.file_size > 256 * 1024 * 1024:
                    raise RecoveryError
                if name.endswith(".dist-info/RECORD"):
                    continue  # uv rewrites RECORD; the independent snapshot pins its bytes.
                expected_path = "lib/python3.14/site-packages/" + name
                if reference.get(expected_path) != hashlib.sha256(archive.read(member)).hexdigest():
                    raise RecoveryError
                verified += 1
    return {"snapshot_files": len(observed), "wheel_payload_files": verified, "wheels": 5}


def recovery() -> dict:
    phase, started, journal, tool = "preflight", False, None, None
    record: dict[str, Any] = {
        "schema_version": 1,
        "mode": "resume_native_r5",
        "status": "running",
        "completed_phases": [],
        "provider_calls": 0,
        "resumed_from": str(FAILED_JOURNAL),
        "state_preserved": True,
    }

    def save() -> None:
        temporary = journal.with_name("." + journal.name + "." + uuid.uuid4().hex)
        tool.write_receipt(temporary, record)
        os.replace(temporary, journal)

    def checkpoint(name: str, **values: Any) -> None:
        record.update(values)
        record["completed_phases"].append(name)
        save()
        print(json.dumps({"phase": name, "status": "passed"}), flush=True)

    try:
        operator_gate()
        private = Path(__file__).resolve().parent
        if not stat.S_IMODE(private.lstat().st_mode) == 0o700 or private.lstat().st_uid != 0:
            raise RecoveryError
        private_bytes(
            Path(__file__).resolve()
        )  # Execution digest is pinned by the TTY entry block.
        patched = private / "install.py"
        pinned_private(patched, PATCHED_INSTALLER_SHA)
        fixed = load_module(patched, "recovery_fixed_installer")
        archive = BOOTSTRAP / "source.tar"
        pinned_private(archive, ARCHIVE_SHA)
        verifier_path = BOOTSTRAP / "scripts/build_asus_release.py"
        pinned_private(verifier_path, VERIFIER_SHA)
        verifier = load_module(verifier_path, "recovery_verifier")
        artifact = verifier.verify_archive(private_bytes(archive), SOURCE_SHA)
        expected = {row["path"]: row["sha256"] for row in artifact["release"]["files"]}
        old_path = BOOTSTRAP / "deploy/asus/install.py"
        pinned_private(old_path, OLD_INSTALLER_SHA)
        bootstrap_tool = load_module(old_path, "recovery_bootstrap_installer")
        layout = bootstrap_tool.Layout()
        bootstrap_tool.stopped_service()
        release, _ = bootstrap_tool.validate_staged_source(layout, archive, SOURCE_SHA)
        tool = load_module(release / "deploy/asus/install.py", "recovery_original_installer")
        failed_bytes = private_bytes(FAILED_JOURNAL, bound=2 * 1024 * 1024)
        failed = validate_failed_receipt(failed_bytes, tool)
        operator = load_module(
            release / "deploy/asus/initial_install.py", "recovery_original_operator"
        )
        credentials = load_module(
            release / "deploy/asus/import_credentials.py", "recovery_credentials"
        )
        credentials.inspect_initial_state(layout)
        if (
            tool.current_release(layout) is not None
            or layout.unit.exists()
            or (layout.config / "gateway.json").exists()
        ):
            raise RecoveryError
        if (release / "runtime-prepared.json").exists():
            raise RecoveryError
        owner = tool.service_owner()
        if owner is None:
            raise RecoveryError
        validate_precredential_layout(
            tool.inspect_layout(layout, service_uid=owner[0], service_gid=owner[1]),
            tool.CREDENTIALS,
        )
        before_orderflow = operator.orderflow()
        reference = tool.strict_json(
            pinned_private(private / "runtime-reference.json", REFERENCE_SHA)
        )
        bundle_path = BOOTSTRAP / "runtime"
        bundle = tool.verify_runtime_bundle(release, bundle_path, BUNDLE_SHA, SOURCE_SHA)
        tool.trusted_bundle(bundle_path, bundle)
        integrity = verify_installed_runtime(
            release / "runtime", bundle_path, bundle, reference, fixed
        )
        journal = layout.backups / ("deployment-resume-" + uuid.uuid4().hex + ".json")
        tool.write_receipt(journal, record)
        checkpoint(
            "preflight",
            preserved_phases=failed["completed_phases"],
            original_failure_sha256=hashlib.sha256(failed_bytes).hexdigest(),
            source_payload_manifest_sha256=SOURCE_SHA,
            runtime_manifest_sha256=BUNDLE_SHA,
            runtime_integrity=integrity,
        )
        phase = "native_runtime"
        lock = fixed.normalize_uv_lock(release / "runtime", apply=True)
        files = tool.runtime_inventory(release / "runtime")
        smoke = tool.native_smoke(release, bundle["target"])
        # Adoption is allowed only after exact frozen-wheel and full-tree checks.
        if {
            row["path"]: row["sha256"]
            for row in files
            if row["kind"] == "file" and row["path"] != ".lock"
        } != reference:
            raise RecoveryError
        prepared = {
            "schema_version": 1,
            "policy": "asus-native-runtime-v1",
            "status": "prepared",
            "bundle": bundle,
            "native_smoke": smoke,
            "files": files,
        }
        prepared_sha = tool.write_receipt(release / "runtime-prepared.json", prepared)
        tool.validate_native_runtime(release, bundle, prepared_sha)
        checkpoint(
            phase,
            lock_repair=lock,
            prepared_runtime_sha256=prepared_sha,
            runtime_reinstalled=False,
            runtime_contents_preserved=True,
        )
        phase = "credential_import"
        initialized = credentials.interactive_initialize()
        if initialized.get("phase") != "credentials-initialized":
            raise RecoveryError
        checkpoint(phase, credential_metadata=initialized["metadata"])
        phase = "activation"
        activated = tool.activation(
            layout,
            archive,
            SOURCE_SHA,
            bundle_path,
            BUNDLE_SHA,
            prepared_sha,
            expected["deploy/asus/gateway.disabled.json"],
            service_uid=owner[0],
            service_gid=owner[1],
            apply=True,
        )
        checkpoint(phase, before_activation_backup=activated["before_activation_backup"])
        phase = "unit_verify"
        operator.command(["/usr/bin/systemd-analyze", "verify", str(layout.unit)], timeout=30)
        checkpoint(phase)
        phase = "start"
        started = True
        operator.command(["/usr/bin/systemctl", "start", SERVICE], timeout=45)
        operator.ready()
        checkpoint(phase)
        acceptance = load_module(release / "deploy/asus/acceptance.py", "recovery_acceptance")
        phase = "initial_acceptance"
        first = acceptance.run_checks(initial=True)
        operator.accepted(first)
        checkpoint(phase, initial_acceptance=first)
        phase = "stop_cleanup_backup"
        operator.command(["/usr/bin/systemctl", "stop", SERVICE], timeout=45)
        if Path("/run/api-quota-broker").exists():
            raise RecoveryError
        checkpoint(phase, stopped_backup=str(tool.backup(layout)), runtime_queue_key_cleared=True)
        phase = "restart"
        operator.command(["/usr/bin/systemctl", "start", SERVICE], timeout=45)
        operator.ready()
        checkpoint(phase)
        phase = "restart_acceptance"
        second = acceptance.run_checks(initial=True)
        operator.accepted(second)
        if first["database"] != second["database"] or operator.orderflow() != before_orderflow:
            raise RecoveryError
        checkpoint(phase, restart_acceptance=second, orderflow_unchanged=True)
        phase = "enable"
        operator.command(["/usr/bin/systemctl", "enable", SERVICE], timeout=20)
        if (
            operator.command(["/usr/bin/systemctl", "is-enabled", SERVICE], capture=True).strip()
            != b"enabled"
        ):
            raise RecoveryError
        checkpoint(phase)
        phase = "complete"
        checkpoint(
            phase,
            status="passed",
            phase=phase,
            enabled=True,
            targets_enabled=False,
            representative_provider_verified=False,
            journal=str(journal),
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - no free-text errors or secrets
        if started:
            try:
                operator.command(["/usr/bin/systemctl", "stop", SERVICE], timeout=45)
            except Exception:  # noqa: BLE001 - no exception messages at the secret boundary
                record["failure_stop_completed"] = False
        record.update(
            status="failed",
            phase=phase,
            reason="gate_failed",
            automatic_retry=False,
            start_attempted=started,
        )
        if tool is not None:
            record.update(tool.safe_error(exc))
        if journal is not None:
            try:
                save()
                record["journal"] = str(journal)
            except (OSError, ValueError):
                pass
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = (
        recovery()
        if args.apply
        else {
            "mode": "resume_native_r5_plan",
            "preserve": ["r5 source", "runtime", "accounts", "old receipts"],
            "resume_from": "native_runtime",
            "credential_reads": 0,
            "provider_calls": 0,
            "service_changes": 0,
        }
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
