"""Recovery gates and sequencing; fixtures never contact hosts or credentials."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resume = load("tested_asus_resume", ROOT / "deploy/asus/resume_native.py")
installer = load("tested_resume_installer", ROOT / "deploy/asus/install.py")


def failed_receipt():
    return {
        "schema_version": 1,
        "mode": "initial_install",
        "status": "failed",
        "phase": "native_runtime",
        "completed_phases": ["source_verify", "provision", "source_stage"],
        "source_payload_manifest_sha256": resume.SOURCE_SHA,
        "runtime_manifest_sha256": resume.BUNDLE_SHA,
        "start_attempted": False,
        "provider_calls": 0,
        "state_preserved": True,
        "reason": "deployment_gate_failed",
    }


def test_precredential_layout_requires_exact_initial_missing_set():
    names = installer.CREDENTIALS
    missing = ["gateway_config", "credential_policy_metadata"] + [
        "encrypted_credential:" + n for n in names
    ]
    report = {"missing": missing, "current_release": None}
    resume.validate_precredential_layout(report, names)
    for changed in (missing + ["state_directory"], missing[:-1], []):
        with pytest.raises(resume.RecoveryError):
            resume.validate_precredential_layout({**report, "missing": changed}, names)
    with pytest.raises(resume.RecoveryError):
        resume.validate_precredential_layout({**report, "current_release": "old"}, names)


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "credential_import"),
        ("start_attempted", True),
        ("provider_calls", 1),
        ("source_payload_manifest_sha256", "0" * 64),
        ("completed_phases", ["source_verify"]),
        ("state_preserved", False),
    ],
)
def test_resume_requires_matching_original_failure(field, value):
    receipt = failed_receipt()
    resume.validate_failed_receipt(json.dumps(receipt).encode(), installer)
    with pytest.raises(resume.RecoveryError):
        resume.validate_failed_receipt(json.dumps({**receipt, field: value}).encode(), installer)


@pytest.mark.parametrize("fault", [None, "missing", "content", "write", "wheel", "unknown"])
def test_snapshot_and_independent_wheel_bytes_must_match_before_mutation(tmp_path, fault):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)  # Fixture contract must not depend on preceding root-guard umask.
    (runtime / ".lock").touch()
    (runtime / ".lock").chmod(0o777)
    package = runtime / "lib/python3.14/site-packages"
    package.mkdir(parents=True)
    for directory in (runtime / "lib", runtime / "lib/python3.14", package):
        directory.chmod(0o755)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    reference, wheels = {}, []
    for index in range(288):
        path = package / f"module{index}.py"
        path.write_bytes(b"public fixture")
        path.chmod(0o644)
        reference[path.relative_to(runtime).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    for index in range(5):
        wheel = bundle / f"package{index}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"module{index}.py",
                b"different" if fault == "wheel" and index == 0 else b"public fixture",
            )
        wheels.append(
            {"path": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        )
    path = package / "module0.py"
    if fault == "missing":
        path.unlink()
    elif fault == "content":
        path.write_bytes(b"changed")
    elif fault == "write":
        path.chmod(0o666)
    elif fault == "unknown":
        (package / "unexpected.pth").write_bytes(b"unsafe")
    if fault:
        with pytest.raises(resume.RecoveryError):
            resume.verify_installed_runtime(
                runtime, bundle, {"files": wheels}, reference, installer, root_uid=os.getuid()
            )
    else:
        assert resume.verify_installed_runtime(
            runtime, bundle, {"files": wheels}, reference, installer, root_uid=os.getuid()
        ) == {"snapshot_files": 288, "wheel_payload_files": 5, "wheels": 5}
    assert stat.S_IMODE((runtime / ".lock").stat().st_mode) == 0o777


def test_root_gate_refuses_without_reading_or_loading_helpers(monkeypatch):
    monkeypatch.setattr(resume.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(resume, "load_module", lambda *a: pytest.fail("loaded before root gate"))
    result = resume.recovery()
    assert result["status"] == "failed" and result["phase"] == "preflight"


def test_default_plan_has_no_files_secrets_or_operations(monkeypatch, capsys):
    monkeypatch.setattr(resume.sys, "argv", ["resume_native.py"])
    monkeypatch.setattr(resume, "recovery", lambda: pytest.fail("executed a plan"))
    assert resume.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["credential_reads"] == result["provider_calls"] == result["service_changes"] == 0


@pytest.mark.parametrize("fault", [None, "core", "cgroup", "swap", "memory", "cpu"])
def test_formal_recovery_requires_reviewed_resource_limits(monkeypatch, fault):
    monkeypatch.setattr(resume.os, "geteuid", lambda: 0)
    monkeypatch.setattr(resume.socket, "gethostname", lambda: resume.HOST)
    monkeypatch.setattr(resume.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        resume.resource, "getrlimit", lambda _: (1, 1) if fault == "core" else (0, 0)
    )
    values = {
        "cgroup": b"0::/system.slice/api-quota-broker-install.service\n",
        "memory.swap.max": b"0\n",
        "memory.max": b"402653184\n",
        "cpu.max": b"50000 100000\n",
    }
    changed = {
        "cgroup": b"0::/wrong\n",
        "swap": b"max\n",
        "memory": b"max\n",
        "cpu": b"100000 100000\n",
    }
    if fault in changed:
        key = {"swap": "memory.swap.max", "memory": "memory.max", "cpu": "cpu.max"}.get(
            fault, fault
        )
        values[key] = changed[fault]
    monkeypatch.setattr(Path, "read_bytes", lambda path: values[path.name])
    if fault:
        with pytest.raises(resume.RecoveryError):
            resume.operator_gate()
    else:
        resume.operator_gate()


@pytest.mark.parametrize(
    "fault", [None, "integrity", "native", "credentials", "initial", "restart"]
)
def test_recovery_sequence_preserves_old_state_and_stops_failures(tmp_path, monkeypatch, fault):
    events = []
    private = tmp_path / "review"
    private.mkdir(mode=0o700)
    release = tmp_path / "release"
    release.mkdir()
    state, config, backups = (tmp_path / name for name in ("state", "config", "backups"))
    for directory in (state, config, backups):
        directory.mkdir()
    layout = SimpleNamespace(
        unit=tmp_path / "broker.service", state=state, config=config, backups=backups
    )
    old = tmp_path / "old.json"
    raw_failed = json.dumps(failed_receipt()).encode()
    old.write_bytes(raw_failed)
    monkeypatch.setattr(resume, "FAILED_JOURNAL", old)
    monkeypatch.setattr(resume, "__file__", str(private / "resume_native.py"))
    real_lstat = Path.lstat

    def lstat(path):
        info = real_lstat(path)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0) if path == private else info

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(resume, "operator_gate", lambda: events.append("operator_gate"))
    monkeypatch.setattr(
        resume, "private_bytes", lambda path, **kw: raw_failed if path == old else b"fixture"
    )
    monkeypatch.setattr(resume, "pinned_private", lambda path, digest: b"{}")
    names = installer.CREDENTIALS
    report = {
        "current_release": None,
        "missing": ["gateway_config", "credential_policy_metadata"]
        + ["encrypted_credential:" + n for n in names],
    }

    def native(*args, **kwargs):
        events.append("native_smoke")
        if fault == "native":
            raise resume.RecoveryError
        return {"status": "passed"}

    def forbidden(*args, **kwargs):
        pytest.fail("provision/staging/reinstall is forbidden during recovery")

    tool = SimpleNamespace(
        Layout=lambda: layout,
        stopped_service=lambda: None,
        validate_staged_source=lambda *a: (release, {}),
        strict_json=installer.strict_json,
        current_release=lambda *a: None,
        service_owner=lambda: (995, 982),
        CREDENTIALS=names,
        inspect_layout=lambda *a, **k: report,
        verify_runtime_bundle=lambda *a: {"target": {}},
        trusted_bundle=lambda *a: None,
        write_receipt=installer.write_receipt,
        runtime_inventory=lambda *a: [],
        native_smoke=native,
        validate_native_runtime=lambda *a: events.append("prepared_validate"),
        activation=lambda *a, **k: {"before_activation_backup": "snapshot"},
        backup=lambda *a: "stopped-snapshot",
        safe_error=installer.safe_error,
        provision=forbidden,
        stage_release=forbidden,
        prepare_native_runtime=forbidden,
    )

    def command(argv, **kwargs):
        events.append(tuple(argv))
        return b"enabled" if "is-enabled" in argv else b""

    operator = SimpleNamespace(
        orderflow=lambda: {"active": True},
        command=command,
        ready=lambda: events.append("ready"),
        accepted=lambda r: (
            None if r["status"] == "passed" else (_ for _ in ()).throw(resume.RecoveryError())
        ),
    )

    def initialize():
        events.append("credential_import")
        if fault == "credentials":
            raise resume.RecoveryError
        return {"phase": "credentials-initialized", "metadata": {}}

    credentials = SimpleNamespace(
        inspect_initial_state=lambda *a: events.append("empty_credentials"),
        interactive_initialize=initialize,
    )
    calls = []

    def acceptance(**kwargs):
        calls.append(1)
        return {
            "status": "failed" if fault == "initial" and len(calls) == 1 else "passed",
            "database": {"rows": 1 if fault == "restart" and len(calls) == 2 else 0},
        }

    verifier = SimpleNamespace(
        verify_archive=lambda *a: {
            "release": {
                "files": [{"path": "deploy/asus/gateway.disabled.json", "sha256": "a" * 64}]
            }
        }
    )
    fixed = SimpleNamespace(normalize_uv_lock=lambda *a, **k: events.append("seal_lock") or {})
    modules = {
        "recovery_fixed_installer": fixed,
        "recovery_verifier": verifier,
        "recovery_bootstrap_installer": tool,
        "recovery_original_installer": tool,
        "recovery_original_operator": operator,
        "recovery_credentials": credentials,
        "recovery_acceptance": SimpleNamespace(run_checks=acceptance),
    }
    monkeypatch.setattr(resume, "load_module", lambda path, name: modules[name])

    def integrity(*args):
        events.append("integrity")
        if fault == "integrity":
            raise resume.RecoveryError
        return {"wheels": 5}

    monkeypatch.setattr(resume, "verify_installed_runtime", integrity)
    result = resume.recovery()
    assert old.read_bytes() == raw_failed
    assert result["provider_calls"] == 0 and result["state_preserved"]
    if fault:
        assert result["status"] == "failed"
        assert ("/usr/bin/systemctl", "enable", resume.SERVICE) not in events
        if fault in ("integrity", "native"):
            assert "credential_import" not in events
        if fault in ("initial", "restart"):
            assert events[-1] == ("/usr/bin/systemctl", "stop", resume.SERVICE)
    else:
        assert result["status"] == "passed" and result["phase"] == "complete"
        assert result["runtime_reinstalled"] is False and result["runtime_contents_preserved"]
        assert (
            events.index("integrity")
            < events.index("seal_lock")
            < events.index("credential_import")
        )
        assert calls == [1, 1]
