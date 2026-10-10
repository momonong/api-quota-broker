"""Resume the exact r5 prepared runtime at credential import, once.

Default is a pure plan. Frozen source/runtime/receipts are never rewritten.
Only approved hidden-TTY initialization and activation/acceptance continue.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

NATIVE_HELPER_SHA = "51e496258bf00fca4b261cd298124fb3c754f0336b4065c90de959792923f1ea"
IMPORTER_SHA = "28f125ae55b95516cd174212da172e5964b91e7458b94329e8864013820b2de1"
FAILED_BOOTSTRAP = Path("/var/tmp/api-quota-broker-credentials-recovery.hojSuG5F")
FAILED_DRIVER_SHA = "5416b24c7eb9a85dd07582367f920bd3e9829eb223f253804de619c8330262e8"
FAILED_IMPORTER_SHA = "4c94b79330e6e999885c1112414445b0af503faa963202b21287cd716558a216"
STAGE_NAME = ".credential-init.ad6503560a1842c5b4e437689755fc1f"
LEGACY_EVIDENCE = {
    "schema_version": 1,
    "status": "failed; operator review required; no retry",
    "credential_policy": "host",
    "encrypted": [],
}
LEGACY_BYTES = json.dumps(LEGACY_EVIDENCE, sort_keys=True, separators=(",", ":")).encode()
PREPARED_SHA = "03dac14f04c1f63a5fbfcc74fb2325744f9abebf21d284fa85eba40c5b3a2938"
FAILED_JOURNAL = Path(
    "/var/backups/api-quota-broker/deployment-credentials-66a6e406ec7146f3a74e1ab2faa5518b.json"
)
HOST = "asus-ubuntu2604-server"
SERVICE = "api-quota-broker.service"


class RecoveryError(ValueError):
    pass


def native_helper(private: Path) -> Any:
    # Before executing any sibling: root trueTTY, no symlink parents, private
    # root ownership, single-link regular file and independent frozen digest.
    if os.geteuid() != 0 or socket.gethostname() != HOST or not sys.stdin.isatty():
        raise RecoveryError
    for component in (*reversed(private.parents), private):
        if stat.S_ISLNK(component.lstat().st_mode):
            raise RecoveryError
    info = private.lstat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RecoveryError
    path = private / "resume_native.py"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 128 * 1024
        ):
            raise RecoveryError
        raw = stream.read(info.st_size + 1)
        if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != NATIVE_HELPER_SHA:
            raise RecoveryError
    spec = importlib.util.spec_from_file_location("credential_resume_native_gates", path)
    if spec is None or spec.loader is None:
        raise RecoveryError
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.operator_gate()  # Exact cgroup / 384MiB / swap0 / CPU50% / core0.
    return module


def validate_failed_receipt(raw: bytes, tool: Any, shared: Any) -> dict:
    receipt = tool.strict_json(raw)
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or receipt.get("mode") != "resume_credentials_r5"
        or receipt.get("status") != "failed"
        or receipt.get("phase") != "credential_import"
        or receipt.get("completed_phases") != ["preflight"]
        or receipt.get("preserved_phases")
        != ["source_verify", "provision", "source_stage", "native_runtime"]
        or receipt.get("source_payload_manifest_sha256") != shared.SOURCE_SHA
        or receipt.get("runtime_manifest_sha256") != shared.BUNDLE_SHA
        or receipt.get("prepared_runtime_sha256") != PREPARED_SHA
        or receipt.get("prepared_runtime_preserved") is not True
        or type(receipt.get("native_runtime_mutations")) is not int
        or receipt["native_runtime_mutations"] != 0
        or receipt.get("reason") != "credential_import_failed"
        or receipt.get("automatic_retry") is not False
        or receipt.get("previous_resume_completed_phases") != ["preflight", "native_runtime"]
        or receipt.get("previous_failure_sha256")
        != "b8fd0c88b74b483f96cf1bfc3455dd131761b955171bd4346880a763424c8a2b"
        or receipt.get("resumed_from")
        != "/var/backups/api-quota-broker/deployment-resume-42e4774c1ec04e01bc50d5b1ede164d7.json"
        or receipt.get("start_attempted") is not False
        or type(receipt.get("provider_calls")) is not int
        or receipt["provider_calls"] != 0
        or receipt.get("state_preserved") is not True
    ):
        raise RecoveryError
    return receipt


def names(directory: Path) -> set[str]:
    values: set[str] = set()
    for path in directory.iterdir():
        if len(values) == 32:
            raise RecoveryError
        values.add(path.name)
    return values


def evidence_only_stage(
    layout: Any, tool: Any, shared: Any, credentials: Any, owner: tuple[int, int]
) -> dict:
    """Admit only the observed stage and the exact known nonsecret legacy evidence."""
    for path, uid, gid, mode in (
        (layout.config, 0, owner[1], 0o750),
        (layout.config / "credentials", 0, 0, 0o700),
        (layout.state, owner[0], owner[1], 0o700),
        (layout.backups, 0, 0, 0o700),
        (layout.config / STAGE_NAME, 0, 0, 0o700),
    ):
        if not tool.metadata(path, uid, mode, directory=True, gid=gid):
            raise RecoveryError
    if (
        names(layout.config) != {"credentials", STAGE_NAME}
        or any((layout.config / "credentials").iterdir())
        or any(layout.state.iterdir())
        or not credentials.check_host_key(
            layout.path("/var/lib/systemd/credential.secret"), installer=tool
        )
        or not tool.metadata(Path("/usr/bin/systemd-creds"), 0, 0o755)
    ):
        raise RecoveryError
    stage = layout.config / STAGE_NAME
    info = stage.lstat()
    if info.st_nlink != 2 or stage.stat().st_dev != layout.backups.stat().st_dev:
        raise RecoveryError
    if names(stage) != {"import-evidence.json"}:
        raise RecoveryError
    evidence = stage / "import-evidence.json"
    if not tool.metadata(evidence, 0, 0o600, gid=0):
        raise RecoveryError
    file_info = evidence.lstat()
    if file_info.st_nlink != 1 or file_info.st_size != len(LEGACY_BYTES):
        raise RecoveryError
    if shared.private_bytes(evidence, bound=len(LEGACY_BYTES)) != LEGACY_BYTES:
        raise RecoveryError
    return {
        "source": str(stage),
        "stage_identity": [info.st_dev, info.st_ino],
        "evidence_identity": [file_info.st_dev, file_info.st_ino],
        "evidence_sha256": hashlib.sha256(LEGACY_BYTES).hexdigest(),
    }


def archive_stage(
    layout: Any,
    tool: Any,
    shared: Any,
    credentials: Any,
    owner: tuple[int, int],
    admitted: dict,
    destination: Path,
) -> dict:
    # Revalidate before any write. Destination is a fresh root-only directory;
    # preserve the complete old stage with rename, never delete/copy its contents.
    if evidence_only_stage(layout, tool, shared, credentials, owner) != admitted:
        raise RecoveryError
    if (
        destination.parent != layout.backups
        or re.fullmatch(r"credential-failure-archive-[0-9a-f]{32}", destination.name) is None
    ):
        raise RecoveryError
    destination.mkdir(mode=0o700)
    if not tool.metadata(destination, 0, 0o700, directory=True, gid=0):
        raise RecoveryError
    archived = destination / "stage"
    os.rename(layout.config / STAGE_NAME, archived)
    stage_info, file_info = archived.lstat(), (archived / "import-evidence.json").lstat()
    if (
        [stage_info.st_dev, stage_info.st_ino] != admitted["stage_identity"]
        or [file_info.st_dev, file_info.st_ino] != admitted["evidence_identity"]
        or names(archived) != {"import-evidence.json"}
        or shared.private_bytes(archived / "import-evidence.json", bound=len(LEGACY_BYTES))
        != LEGACY_BYTES
    ):
        raise RecoveryError
    for directory in (layout.config, destination, layout.backups):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    credentials.inspect_initial_state(layout, installer=tool)
    return {**admitted, "destination": str(archived), "preserved": True}


def recovery() -> dict:
    phase, started, journal, tool, credentials = "preflight", False, None, None, None
    record: dict[str, Any] = {
        "schema_version": 1,
        "mode": "resume_credentials_r5",
        "status": "running",
        "completed_phases": [],
        "provider_calls": 0,
        "resumed_from": str(FAILED_JOURNAL),
        "state_preserved": True,
        "native_runtime_mutations": 0,
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
        private = Path(__file__).absolute().parent
        shared = native_helper(private)
        shared.private_bytes(
            Path(__file__).absolute()
        )  # SHA is independently gated by the TTY block.
        archive = shared.BOOTSTRAP / "source.tar"
        shared.pinned_private(archive, shared.ARCHIVE_SHA)
        verifier_path = shared.BOOTSTRAP / "scripts/build_asus_release.py"
        shared.pinned_private(verifier_path, shared.VERIFIER_SHA)
        verifier = shared.load_module(verifier_path, "credential_resume_verifier")
        artifact = verifier.verify_archive(shared.private_bytes(archive), shared.SOURCE_SHA)
        expected = {row["path"]: row["sha256"] for row in artifact["release"]["files"]}
        old_path = shared.BOOTSTRAP / "deploy/asus/install.py"
        shared.pinned_private(old_path, shared.OLD_INSTALLER_SHA)
        bootstrap_tool = shared.load_module(old_path, "credential_resume_bootstrap")
        layout = bootstrap_tool.Layout()
        bootstrap_tool.stopped_service()
        release, _ = bootstrap_tool.validate_staged_source(layout, archive, shared.SOURCE_SHA)
        tool = shared.load_module(release / "deploy/asus/install.py", "credential_resume_original")
        failed_bytes = shared.private_bytes(FAILED_JOURNAL, bound=2 * 1024 * 1024)
        failed = validate_failed_receipt(failed_bytes, tool, shared)
        shared.pinned_private(FAILED_BOOTSTRAP / "resume_credentials.py", FAILED_DRIVER_SHA)
        shared.pinned_private(FAILED_BOOTSTRAP / "import_credentials.py", FAILED_IMPORTER_SHA)
        importer_path = private / "import_credentials.py"
        shared.pinned_private(importer_path, IMPORTER_SHA)
        credentials = shared.load_module(importer_path, "credential_resume_importer")
        owner = tool.service_owner()
        if owner is None:
            raise RecoveryError
        admitted = evidence_only_stage(layout, tool, shared, credentials, owner)
        shared.validate_precredential_layout(
            tool.inspect_layout(layout, service_uid=owner[0], service_gid=owner[1]),
            tool.CREDENTIALS,
        )
        if layout.unit.exists() or (layout.config / "gateway.json").exists():
            raise RecoveryError
        if not tool.metadata(layout.backups, 0, 0o700, directory=True):
            raise RecoveryError
        bundle_path = shared.BOOTSTRAP / "runtime"
        bundle = tool.verify_runtime_bundle(
            release, bundle_path, shared.BUNDLE_SHA, shared.SOURCE_SHA
        )
        tool.trusted_bundle(bundle_path, bundle)
        prepared_sha = PREPARED_SHA
        tool.validate_native_runtime(release, bundle, prepared_sha)
        operator = shared.load_module(
            release / "deploy/asus/initial_install.py", "credential_resume_operator"
        )
        before_orderflow = operator.orderflow()
        journal = layout.backups / ("deployment-credentials-" + uuid.uuid4().hex + ".json")
        tool.write_receipt(journal, record)
        checkpoint(
            "preflight",
            prepared_runtime_sha256=prepared_sha,
            preserved_phases=failed["preserved_phases"],
            previous_resume_completed_phases=failed["completed_phases"],
            previous_failure_sha256=hashlib.sha256(failed_bytes).hexdigest(),
            source_payload_manifest_sha256=shared.SOURCE_SHA,
            runtime_manifest_sha256=shared.BUNDLE_SHA,
            prepared_runtime_preserved=True,
        )
        phase = "stage_archive"
        destination = layout.backups / ("credential-failure-archive-" + uuid.uuid4().hex)
        record["stage_archive"] = {
            **admitted,
            "destination": str(destination / "stage"),
            "preserved": False,
        }
        save()  # Persist the exact destination before rename, including on failure.
        archived = archive_stage(layout, tool, shared, credentials, owner, admitted, destination)
        checkpoint(phase, stage_archive=archived)
        phase = "credential_import"
        initialized = credentials.interactive_initialize(installer=tool, allow_host_key_setup=False)
        if initialized.get("phase") != "credentials-initialized":
            raise RecoveryError
        checkpoint(phase, credential_metadata=initialized["metadata"])
        phase = "activation"
        activated = tool.activation(
            layout,
            archive,
            shared.SOURCE_SHA,
            bundle_path,
            shared.BUNDLE_SHA,
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
        acceptance = shared.load_module(
            release / "deploy/asus/acceptance.py", "credential_resume_acceptance"
        )
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
            if credentials is not None and isinstance(exc, credentials.CredentialError):
                record.update(credentials.failure_details(exc))
        if journal is not None:
            try:
                save()
                record["journal"] = str(journal)
            except (OSError, ValueError):
                pass
    return record


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RecoveryError


def main() -> int:
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args()
        result = (
            recovery()
            if args.apply
            else {
                "mode": "resume_credentials_r5_plan",
                "resume_from": "credential_import",
                "preserve": [
                    "source",
                    "prepared runtime",
                    "host key",
                    "old receipts",
                    "exact evidence-only stage via archive",
                ],
                "required_stage": STAGE_NAME,
                "host_key_setup": False,
                "provider_calls": 0,
                "credential_reads": 0,
                "service_changes": 0,
            }
        )
    except RecoveryError:
        result = {"status": "failed", "phase": "options", "reason": "invalid_options"}
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
