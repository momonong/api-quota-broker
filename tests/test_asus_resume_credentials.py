"""Prepared-runtime recovery, bounded dates and safe failure sequencing."""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resume = load("tested_credentials_resume", "resume_credentials.py")
native = load("tested_credentials_native", "resume_native.py")
installer = load("tested_credentials_resume_installer", "install.py")
importer = load("tested_credentials_resume_importer", "import_credentials.py")


def failed_receipt():
    return {
        "schema_version": 1,
        "mode": "resume_credentials_r5",
        "status": "failed",
        "phase": "credential_import",
        "completed_phases": ["preflight"],
        "preserved_phases": ["source_verify", "provision", "source_stage", "native_runtime"],
        "source_payload_manifest_sha256": native.SOURCE_SHA,
        "runtime_manifest_sha256": native.BUNDLE_SHA,
        "prepared_runtime_sha256": resume.PREPARED_SHA,
        "prepared_runtime_preserved": True,
        "native_runtime_mutations": 0,
        "reason": "credential_import_failed",
        "automatic_retry": False,
        "previous_resume_completed_phases": ["preflight", "native_runtime"],
        "previous_failure_sha256": "b8fd0c88b74b483f96cf1bfc3455dd131761b955171bd4346880a763424c8a2b",
        "resumed_from": "/var/backups/api-quota-broker/deployment-resume-42e4774c1ec04e01bc50d5b1ede164d7.json",
        "start_attempted": False,
        "provider_calls": 0,
        "state_preserved": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "activation"),
        ("prepared_runtime_sha256", "0" * 64),
        ("completed_phases", ["preflight", "native_runtime"]),
        ("provider_calls", True),
        ("start_attempted", True),
        ("prepared_runtime_preserved", False),
        ("source_payload_manifest_sha256", "0" * 64),
    ],
)
def test_exact_previous_interruption_and_prepared_pin_required(field, value):
    raw = failed_receipt()
    assert resume.validate_failed_receipt(json.dumps(raw).encode(), installer, native) == raw
    with pytest.raises(resume.RecoveryError):
        resume.validate_failed_receipt(
            json.dumps({**raw, field: value}).encode(), installer, native
        )


def test_nonroot_stops_before_helper_or_private_files(monkeypatch):
    monkeypatch.setattr(resume.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(Path, "lstat", lambda *a: pytest.fail("root file read"))
    result = resume.recovery()
    assert result["status"] == "failed" and result["phase"] == "preflight"


@pytest.mark.parametrize("fault", ["digest", "mode", "hardlink", "symlink"])
def test_untrusted_shared_helper_never_executes(tmp_path, monkeypatch, fault):
    private = tmp_path / "review"
    private.mkdir(mode=0o700)
    path = private / "resume_native.py"
    path.write_bytes(b"untrusted fixture")
    path.chmod(0o600)
    if fault == "mode":
        path.chmod(0o644)
    elif fault == "hardlink":
        os.link(path, private / "alias")
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(private / "foreign")
    monkeypatch.setattr(resume.os, "geteuid", lambda: 0)
    monkeypatch.setattr(resume.socket, "gethostname", lambda: resume.HOST)
    monkeypatch.setattr(resume.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    real_lstat, real_fstat = Path.lstat, os.fstat
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda p: (
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
            if p == private
            else real_lstat(p)
        ),
    )

    def root_info(fd):
        info = real_fstat(fd)
        return SimpleNamespace(
            st_uid=0, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size
        )

    monkeypatch.setattr(os, "fstat", root_info)
    monkeypatch.setattr(
        resume.importlib.util,
        "spec_from_file_location",
        lambda *a: pytest.fail("untrusted helper executed"),
    )
    with pytest.raises((resume.RecoveryError, OSError)):
        resume.native_helper(private)


@pytest.mark.parametrize("args", [[], ["--token", "PRIVATE_FIXTURE"]])
def test_options_have_no_secret_reflection_or_host_effect(monkeypatch, capsys, args):
    monkeypatch.setattr(resume.sys, "argv", ["resume_credentials.py", *args])
    monkeypatch.setattr(resume, "recovery", lambda: pytest.fail("unexpected operation"))
    assert resume.main() == bool(args)
    output = capsys.readouterr().out
    assert "PRIVATE_FIXTURE" not in output
    if not args:
        report = json.loads(output)
        assert (
            report["provider_calls"] == report["credential_reads"] == report["service_changes"] == 0
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "early_receipt",
        "partial_credentials",
        "prepared",
        "archive",
        "expiry",
        "initial",
        "restart",
    ],
)
def test_only_credentials_continue_after_prepared_gate_and_failures_preserve_state(
    tmp_path, monkeypatch, capsys, fault
):
    events = []
    private = tmp_path / "review"
    private.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    prepared_file = release / "runtime-prepared.json"
    prepared_file.write_bytes(b"frozen prepared fixture")
    dirs = {name: tmp_path / name for name in ("config", "state", "backups")}
    for path in dirs.values():
        path.mkdir()
    layout = SimpleNamespace(unit=tmp_path / "broker.service", **dirs)
    old = tmp_path / "previous.json"
    failed = failed_receipt()
    if fault == "early_receipt":
        failed["phase"] = "activation"
    original_bytes = json.dumps(failed).encode()
    old.write_bytes(original_bytes)
    monkeypatch.setattr(resume, "FAILED_JOURNAL", old)
    monkeypatch.setattr(resume, "__file__", str(private / "resume_credentials.py"))

    def forbidden(*a, **k):
        pytest.fail("native mutation / reinstall / provision / staging forbidden")

    def native_validation(release, bundle, pin):
        events.append("prepared_validation")
        assert pin == resume.PREPARED_SHA
        if fault == "prepared":
            raise resume.RecoveryError

    names = installer.CREDENTIALS
    missing = ["gateway_config", "credential_policy_metadata"] + [
        "encrypted_credential:" + n for n in names
    ]
    tool = SimpleNamespace(
        Layout=lambda: layout,
        stopped_service=lambda: None,
        validate_staged_source=lambda *a: (release, {}),
        strict_json=installer.strict_json,
        service_owner=lambda: (995, 982),
        CREDENTIALS=names,
        inspect_layout=lambda *a, **k: {"current_release": None, "missing": missing},
        metadata=lambda *a, **k: True,
        verify_runtime_bundle=lambda *a: {},
        trusted_bundle=lambda *a: None,
        validate_native_runtime=native_validation,
        write_receipt=installer.write_receipt,
        activation=lambda *a, **k: {"before_activation_backup": "snapshot"},
        backup=lambda *a: "snapshot",
        safe_error=installer.safe_error,
        normalize_uv_lock=forbidden,
        provision=forbidden,
        stage_release=forbidden,
        prepare_native_runtime=forbidden,
    )

    def initial_state(*args, **kwargs):
        events.append("empty_credentials")
        assert kwargs["installer"] is tool
        if fault == "partial_credentials":
            raise importer.CredentialError

    def initialize(*, installer, allow_host_key_setup):
        assert installer is tool and allow_host_key_setup is False
        events.append("token_import")
        if fault == "expiry":
            raise importer.CredentialError("expiry_past")
        return {"phase": "credentials-initialized", "metadata": {}}

    credentials = SimpleNamespace(
        inspect_initial_state=initial_state,
        interactive_initialize=initialize,
        CredentialError=importer.CredentialError,
        failure_details=importer.failure_details,
        metadata_record=importer.metadata_record,
    )

    def command(argv, **kwargs):
        events.append(tuple(argv))
        return b"enabled" if "is-enabled" in argv else b""

    def accepted(report):
        if report["status"] != "passed":
            raise resume.RecoveryError

    operator = SimpleNamespace(
        command=command,
        ready=lambda: events.append("ready"),
        accepted=accepted,
        orderflow=lambda: {"active": True},
    )
    attempts = []

    def acceptance(**kwargs):
        attempts.append(1)
        return {
            "status": "failed" if fault == "initial" and len(attempts) == 1 else "passed",
            "database": {"rows": int(fault == "restart" and len(attempts) == 2)},
        }

    verifier = SimpleNamespace(
        verify_archive=lambda *a: {
            "release": {
                "files": [{"path": "deploy/asus/gateway.disabled.json", "sha256": "a" * 64}]
            }
        }
    )
    modules = {
        "credential_resume_verifier": verifier,
        "credential_resume_bootstrap": tool,
        "credential_resume_original": tool,
        "credential_resume_importer": credentials,
        "credential_resume_operator": operator,
        "credential_resume_acceptance": SimpleNamespace(run_checks=acceptance),
    }
    shared = SimpleNamespace(
        BOOTSTRAP=tmp_path,
        SOURCE_SHA=native.SOURCE_SHA,
        BUNDLE_SHA=native.BUNDLE_SHA,
        ARCHIVE_SHA=native.ARCHIVE_SHA,
        VERIFIER_SHA=native.VERIFIER_SHA,
        OLD_INSTALLER_SHA=native.OLD_INSTALLER_SHA,
        private_bytes=lambda path, **kwargs: original_bytes if path == old else b"fixture",
        pinned_private=lambda *a: b"fixture",
        load_module=lambda path, name: modules[name],
        validate_precredential_layout=native.validate_precredential_layout,
    )
    monkeypatch.setattr(resume, "native_helper", lambda *a: shared)

    def admit(*a):
        initial_state(layout, installer=tool)
        return {
            "source": "fixture",
            "stage_identity": [1, 2],
            "evidence_identity": [1, 3],
            "evidence_sha256": "a" * 64,
        }

    def archive(*a):
        events.append("stage_archive")
        if fault == "archive":
            raise OSError(13, "PRIVATE_FIXTURE_NOT_LOGGED")
        return {**admit(), "preserved": True, "destination": "fixture_archive"}

    monkeypatch.setattr(resume, "evidence_only_stage", admit)
    monkeypatch.setattr(resume, "archive_stage", archive)
    result = resume.recovery()
    assert (
        old.read_bytes() == original_bytes
        and prepared_file.read_bytes() == b"frozen prepared fixture"
    )
    assert result["native_runtime_mutations"] == result["provider_calls"] == 0
    assert "Traceback" not in capsys.readouterr().out
    if fault:
        assert result["status"] == "failed"
        assert ("/usr/bin/systemctl", "enable", resume.SERVICE) not in events
        if fault in ("early_receipt", "partial_credentials", "prepared"):
            assert "token_import" not in events
            assert list(dirs["backups"].iterdir()) == []
        if fault == "archive":
            assert result["phase"] == "stage_archive" and "token_import" not in events
            assert result["stage_archive"]["preserved"] is False
            assert "PRIVATE_FIXTURE_NOT_LOGGED" not in json.dumps(result)
        if fault == "expiry":
            assert result["reason"] == "expiry_past"
        if fault in ("initial", "restart"):
            assert events[-1] == ("/usr/bin/systemctl", "stop", resume.SERVICE)
    else:
        assert result["status"] == "passed" and result["phase"] == "complete"
        assert result["preserved_phases"] == [
            "source_verify",
            "provision",
            "source_stage",
            "native_runtime",
        ]
        assert (
            events.index("prepared_validation")
            < events.index("stage_archive")
            < events.index("token_import")
        )
        assert attempts == [1, 1]
    assert prepared_file.read_bytes() == b"frozen prepared fixture"


@pytest.fixture
def stage_case(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    layout = installer.Layout(tmp_path)
    for path, mode in (
        (layout.config, 0o750),
        (layout.config / "credentials", 0o700),
        (layout.state, 0o700),
        (layout.backups, 0o700),
        (layout.path("/var/lib/systemd"), 0o700),
    ):
        path.mkdir(parents=True, mode=mode, exist_ok=True)
        path.chmod(mode)
    host = layout.path("/var/lib/systemd/credential.secret")
    host.write_bytes(b"DO_NOT_READ_HOST_KEY_FIXTURE")
    host.chmod(0o400)
    stage = layout.config / resume.STAGE_NAME
    stage.mkdir(mode=0o700)
    evidence = stage / "import-evidence.json"
    evidence.write_bytes(resume.LEGACY_BYTES)
    evidence.chmod(0o600)
    real_metadata = installer.metadata

    def metadata(path, uid, mode, *, directory=False, gid=None):
        if path == Path("/usr/bin/systemd-creds"):
            return True
        return real_metadata(
            path,
            os.getuid(),
            mode,
            directory=directory,
            gid=os.getgid() if gid is not None else None,
        )

    monkeypatch.setattr(installer, "metadata", metadata)
    monkeypatch.setattr(installer, "service_owner", lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr(installer, "stopped_service", lambda: None)
    monkeypatch.setattr(importer, "operator_gate", lambda: None)
    shared = SimpleNamespace(
        private_bytes=lambda path, bound: native.private_bytes(path, owner=os.getuid(), bound=bound)
    )
    check = lambda path, installer: importer.check_host_key(
        path, root_uid=os.getuid(), installer=installer
    )
    credentials = SimpleNamespace(
        check_host_key=check,
        inspect_initial_state=lambda layout, installer: importer.inspect_initial_state(
            layout, root_uid=os.getuid(), installer=installer
        ),
    )
    return SimpleNamespace(
        layout=layout,
        stage=stage,
        evidence=evidence,
        host=host,
        shared=shared,
        credentials=credentials,
        owner=(os.getuid(), os.getgid()),
        now=datetime(2026, 10, 4, tzinfo=UTC),
    )


def test_real_evidence_stage_archive_preserves_inode_bytes_then_one_token_import(
    stage_case, monkeypatch
):
    from datetime import UTC, datetime

    case = stage_case
    before = case.evidence.read_bytes()
    stage_inode, file_inode = case.stage.stat().st_ino, case.evidence.stat().st_ino
    host_inode = case.host.stat().st_ino
    admitted = resume.evidence_only_stage(
        case.layout, installer, case.shared, case.credentials, case.owner
    )
    destination = case.layout.backups / ("credential-failure-archive-" + "0" * 32)
    result = resume.archive_stage(
        case.layout, installer, case.shared, case.credentials, case.owner, admitted, destination
    )
    archived = destination / "stage"
    assert result["preserved"] and not case.stage.exists()
    assert archived.stat().st_ino == stage_inode
    assert (archived / "import-evidence.json").stat().st_ino == file_inode
    assert (archived / "import-evidence.json").read_bytes() == before
    assert not list((case.layout.config / "credentials").iterdir())
    calls, reads = [], []

    def encrypt(argv, **kwargs):
        assert "setup" not in argv
        calls.append(argv)
        target = Path(argv[-1])
        target.write_bytes(b"CIPHER_FIXTURE")
        target.chmod(0o600)

    monkeypatch.setattr(importer.subprocess, "run", encrypt)
    initialized = importer.initialize(
        datetime(2026, 11, 2, 3, 36, 17, tzinfo=UTC),
        human_attested=True,
        token_reader=lambda: reads.append(1) or ("dp.st.dev." + "a" * 40),
        layout=case.layout,
        now=case.now,
        root_uid=os.getuid(),
        installer=installer,
        allow_host_key_setup=False,
    )
    assert initialized["phase"] == "credentials-initialized" and reads == [1] and len(calls) == 5
    assert case.host.stat().st_ino == host_inode
    assert (archived / "import-evidence.json").read_bytes() == before
    assert {p.name for p in (case.layout.config / "credentials").iterdir()} == {
        n + ".cred" for n in importer.NAMES
    } | {"import-evidence.json", "doppler-metadata.json"}


@pytest.mark.parametrize(
    "fault",
    [
        "cipher",
        "unknown",
        "second_stage",
        "different_stage",
        "evidence_bytes",
        "evidence_status",
        "evidence_mode",
        "evidence_link",
        "evidence_symlink",
        "stage_mode",
        "credentials",
        "state",
        "host_missing",
        "host_mode",
        "changed_after_admission",
        "destination_exists",
    ],
)
def test_unsafe_partial_stage_never_moved_or_imported(stage_case, fault):
    case = stage_case
    admitted = resume.evidence_only_stage(
        case.layout, installer, case.shared, case.credentials, case.owner
    )
    destination = case.layout.backups / ("credential-failure-archive-" + "0" * 32)
    if fault == "cipher":
        (case.stage / "queue_key.cred").write_bytes(b"CIPHER_FIXTURE")
    elif fault == "unknown":
        (case.stage / "UNTRUSTED_FIXTURE_NAME").touch()
    elif fault == "second_stage":
        (case.layout.config / (".credential-init." + "0" * 32)).mkdir(mode=0o700)
    elif fault == "different_stage":
        case.stage.rename(case.layout.config / (".credential-init." + "0" * 32))
    elif fault in ("evidence_bytes", "changed_after_admission"):
        case.evidence.write_bytes(resume.LEGACY_BYTES.replace(b"failed", b"FAILED"))
    elif fault == "evidence_status":
        case.evidence.write_text(json.dumps({**resume.LEGACY_EVIDENCE, "status": "initializing"}))
    elif fault == "evidence_mode":
        case.evidence.chmod(0o644)
    elif fault == "evidence_link":
        os.link(case.evidence, case.layout.backups / "alias")
    elif fault == "evidence_symlink":
        case.evidence.rename(case.layout.backups / "evidence")
        case.evidence.symlink_to(case.layout.backups / "evidence")
    elif fault == "stage_mode":
        case.stage.chmod(0o755)
    elif fault == "credentials":
        (case.layout.config / "credentials" / "queue_key.cred").touch()
    elif fault == "state":
        (case.layout.state / "ledger.sqlite3").touch()
    elif fault == "host_missing":
        case.host.unlink()
    elif fault == "host_mode":
        case.host.chmod(0o644)
    elif fault == "destination_exists":
        destination.mkdir(mode=0o700)
        (destination / "sentinel").write_bytes(b"PRESERVE_FIXTURE")
    before = {
        str(p.relative_to(case.layout.root)): (p.lstat().st_ino, p.lstat().st_mode)
        for p in case.layout.root.rglob("*")
    }
    with pytest.raises(
        (
            resume.RecoveryError,
            installer.DeploymentError,
            native.RecoveryError,
            importer.CredentialError,
            FileNotFoundError,
            FileExistsError,
        )
    ):
        resume.archive_stage(
            case.layout, installer, case.shared, case.credentials, case.owner, admitted, destination
        )
    after = {
        str(p.relative_to(case.layout.root)): (p.lstat().st_ino, p.lstat().st_mode)
        for p in case.layout.root.rglob("*")
    }
    assert before == after


def test_failure_after_atomic_move_preserves_archived_evidence_without_retry(
    stage_case, monkeypatch
):
    case = stage_case
    admitted = resume.evidence_only_stage(
        case.layout, installer, case.shared, case.credentials, case.owner
    )
    destination = case.layout.backups / ("credential-failure-archive-" + "0" * 32)
    original = case.evidence.read_bytes()

    def interrupted(fd):
        raise OSError(5, "PRIVATE_FIXTURE_NOT_LOGGED")

    monkeypatch.setattr(resume.os, "fsync", interrupted)
    with pytest.raises(OSError):
        resume.archive_stage(
            case.layout, installer, case.shared, case.credentials, case.owner, admitted, destination
        )
    assert not case.stage.exists()
    archived = destination / "stage" / "import-evidence.json"
    assert archived.read_bytes() == original
    assert archived.stat().st_ino == admitted["evidence_identity"][1]
    assert not list((case.layout.config / "credentials").iterdir())
    assert not list(case.layout.state.iterdir())
    with pytest.raises(resume.RecoveryError):
        resume.archive_stage(
            case.layout, installer, case.shared, case.credentials, case.owner, admitted, destination
        )
