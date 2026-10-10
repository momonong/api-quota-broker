import hashlib
import importlib.util
import json
import os
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def operator():
    path = Path(__file__).resolve().parents[1] / "deploy/asus/initial_install.py"
    spec = importlib.util.spec_from_file_location("tested_asus_operator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected operation")


def test_default_plan_reads_no_host_files_or_credentials(operator, monkeypatch, capsys):
    monkeypatch.setattr(operator, "deployment", forbidden)
    assert operator.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry_plan"
    assert result["credential_reads"] == result["provider_calls"] == result["service_changes"] == 0


@pytest.mark.parametrize("argv", [["--token", "PRIVATE"], ["--apply"]])
def test_invalid_options_do_not_reflect_arguments(operator, capsys, argv):
    assert operator.main(argv) == 1
    raw = capsys.readouterr().out
    assert "PRIVATE" not in raw
    assert json.loads(raw)["phase"] == "options"


def test_host_gate_refuses_before_files_or_credentials(operator, monkeypatch):
    monkeypatch.setattr(operator.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(operator.socket, "gethostname", forbidden)
    with pytest.raises(operator.OperatorError):
        operator.operator_gate()


@pytest.mark.parametrize("ready_after", [3.0, None])
def test_readiness_waits_for_slow_start_and_stops_at_deadline(operator, monkeypatch, ready_after):
    clock = [0.0]
    connections = []
    monkeypatch.setattr(operator.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        operator.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 18084)
            assert 0 < timeout <= min(1, 25 - clock[0])
            self.closed = False
            connections.append(self)

        def request(self, method, path):
            assert (method, path) == ("GET", "/v1/diagnostics")
            if ready_after is None or clock[0] < ready_after:
                raise ConnectionRefusedError

        def getresponse(self):
            return SimpleNamespace(status=401)

        def close(self):
            self.closed = True

    monkeypatch.setattr(operator.http.client, "HTTPConnection", Connection)
    if ready_after is None:
        with pytest.raises(operator.OperatorError):
            operator.ready()
        assert clock[0] == 25
    else:
        operator.ready()
        assert clock[0] == ready_after
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "wrong_owner"])
def test_bootstrap_metadata_rejects_untrusted_input(operator, tmp_path, kind):
    file = tmp_path / "helper.py"
    file.write_bytes(b"fixture")
    file.chmod(0o600)
    owner = os.getuid()
    if kind == "symlink":
        alias = tmp_path / "link"
        alias.symlink_to(file)
        file = alias
    elif kind == "hardlink":
        os.link(file, tmp_path / "alias")
    elif kind == "public":
        file.chmod(0o644)
    else:
        owner += 10000
    with pytest.raises(operator.OperatorError):
        operator.private_file(file, owner=owner)


@pytest.fixture
def workflow(operator, tmp_path, monkeypatch):
    events = []
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    layout = SimpleNamespace(
        state=tmp_path / "state",
        unit=tmp_path / "broker.service",
        config=tmp_path / "config",
        backups=backup_dir,
    )
    layout.state.mkdir()
    layout.config.mkdir()
    blob = b"reviewed fixture"
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "__file__", str(bootstrap / "deploy/asus/initial_install.py"))
    safe_installer = operator.load_module(
        Path(__file__).resolve().parents[1] / "deploy/asus/install.py", "tested_safe_installer"
    )
    digest = hashlib.sha256(blob).hexdigest()
    expected = {
        p: digest
        for p in [
            "deploy/asus/initial_install.py",
            "deploy/asus/install.py",
            "deploy/asus/gateway.disabled.json",
        ]
    }
    artifact = {"release": {"files": [{"path": p, "sha256": h} for p, h in expected.items()]}}
    verifier = SimpleNamespace(verify_archive=lambda *args: artifact)

    def event(name, result=None):
        def call(*args, **kwargs):
            events.append(name)
            return result

        return call

    def write_receipt(path, body):
        safe_installer.write_receipt(path, body)

    installer = SimpleNamespace(
        Layout=lambda: layout,
        stopped_service=event("stopped"),
        current_release=lambda _: None,
        provision=event("provision"),
        stage_release=event("stage"),
        validate_staged_source=lambda *args: (release, {}),
        verify_runtime_bundle=event("bundle", {}),
        prepare_native_runtime=event("native", {"prepared_runtime_sha256": "a" * 64}),
        service_owner=lambda: (400, 400),
        activation=event("activation", {"before_activation_backup": str(backup_dir / "before")}),
        backup=event("backup", backup_dir / "after"),
        write_receipt=write_receipt,
        safe_error=safe_installer.safe_error,
    )
    credentials = SimpleNamespace(
        interactive_initialize=event(
            "hidden_import",
            {"phase": "credentials-initialized", "metadata": {"expires_at": "fixture UTC"}},
        )
    )
    acceptance = SimpleNamespace(
        run_checks=event(
            "acceptance",
            {"status": "passed", "mode": "initial", "database": {"quick_check": "ok", "rows": 0}},
        )
    )

    def load(path, name):
        return {
            "operator_release_verifier": verifier,
            "operator_installer": installer,
            "operator_credentials": credentials,
            "operator_acceptance": acceptance,
        }[name]

    def command(argv, **kwargs):
        events.append(tuple(argv))
        if "is-enabled" in argv:
            return b"enabled\n"
        return b""

    real_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: False if str(self) == "/run/api-quota-broker" else real_exists(self),
    )
    monkeypatch.setattr(operator, "operator_gate", event("gate"))
    monkeypatch.setattr(operator, "VERIFIER_SHA", digest)
    monkeypatch.setattr(operator, "private_file", lambda *args: blob)
    monkeypatch.setattr(operator, "load_module", load)
    monkeypatch.setattr(
        operator, "orderflow", lambda: {"active": True, "restarts": 0, "http_status": 200}
    )
    monkeypatch.setattr(operator, "command", command)
    monkeypatch.setattr(operator, "ready", event("ready"))
    args = Namespace(
        archive=tmp_path / "source.tar",
        source_manifest_sha256="b" * 64,
        runtime_bundle=tmp_path / "runtime",
        runtime_manifest_sha256="c" * 64,
    )
    return SimpleNamespace(
        args=args,
        events=events,
        installer=installer,
        acceptance=acceptance,
        layout=layout,
        command=command,
    )


def test_complete_initial_workflow_enables_only_after_restart_acceptance(operator, workflow):
    result = operator.deployment(workflow.args)
    assert result["status"] == "passed" and result["enabled"]
    assert not result["representative_provider_verified"]
    events = workflow.events
    assert events.index("native") < events.index("hidden_import") < events.index("activation")
    assert events.count("acceptance") == 2
    enable = ("/usr/bin/systemctl", "enable", operator.SERVICE)
    assert events.index(enable) > max(i for i, e in enumerate(events) if e == "acceptance")
    assert events.count(("/usr/bin/systemctl", "start", operator.SERVICE)) == 2
    assert result["runtime_queue_key_cleared"]


def test_existing_ledger_refuses_secret_initialization(operator, workflow):
    (workflow.layout.state / "ledger.sqlite3").write_bytes(b"historical fixture")
    result = operator.deployment(workflow.args)
    assert result["status"] == "failed" and result["phase"] == "source_verify"
    assert "hidden_import" not in workflow.events and "provision" not in workflow.events
    assert (workflow.layout.state / "ledger.sqlite3").read_bytes() == b"historical fixture"


def test_native_failure_never_prompts_for_secret(operator, workflow):
    workflow.installer.prepare_native_runtime = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("PRIVATE backend error")
    )
    result = operator.deployment(workflow.args)
    assert result["phase"] == "native_runtime" and result["status"] == "failed"
    assert "PRIVATE" not in json.dumps(result)
    assert "hidden_import" not in workflow.events
    assert not result["start_attempted"]


def test_initial_acceptance_failure_stops_broker_and_never_enables(operator, workflow):
    workflow.acceptance.run_checks = lambda **kwargs: (_ for _ in ()).throw(
        ValueError("PRIVATE token")
    )
    result = operator.deployment(workflow.args)
    assert result["phase"] == "initial_acceptance" and result["state_preserved"]
    assert ("/usr/bin/systemctl", "stop", operator.SERVICE) in workflow.events
    assert ("/usr/bin/systemctl", "enable", operator.SERVICE) not in workflow.events
    assert "PRIVATE" not in json.dumps(result)


def test_failed_acceptance_receipt_cannot_enable_service(operator, workflow):
    workflow.acceptance.run_checks = lambda **kwargs: {
        "status": "failed",
        "phase": "http",
        "reason": "authorization_or_error",
    }
    result = operator.deployment(workflow.args)
    assert result["status"] == "failed" and result["phase"] == "initial_acceptance"
    assert result["initial_acceptance"]["reason"] == "authorization_or_error"
    assert ("/usr/bin/systemctl", "enable", operator.SERVICE) not in workflow.events


def test_restart_database_change_stops_without_enable(operator, workflow):
    observations = iter(
        [
            {"status": "passed", "mode": "initial", "database": {"quick_check": "ok", "rows": 0}},
            {"status": "passed", "mode": "initial", "database": {"quick_check": "ok", "rows": 1}},
        ]
    )
    workflow.acceptance.run_checks = lambda **kwargs: next(observations)
    result = operator.deployment(workflow.args)
    assert result["phase"] == "restart_acceptance" and result["status"] == "failed"
    assert ("/usr/bin/systemctl", "enable", operator.SERVICE) not in workflow.events


def test_orderflow_change_stops_without_enable(operator, workflow, monkeypatch):
    observations = iter([{"restarts": 0}, {"restarts": 1}])
    monkeypatch.setattr(operator, "orderflow", lambda: next(observations))
    result = operator.deployment(workflow.args)
    assert result["phase"] == "restart_acceptance" and result["status"] == "failed"
    assert ("/usr/bin/systemctl", "enable", operator.SERVICE) not in workflow.events


def test_provision_failure_leaves_private_bootstrap_journal_and_safe_errno(operator, workflow):
    def fail(*args, **kwargs):
        raise FileNotFoundError(2, "PRIVATE path or credential")

    workflow.installer.provision = fail
    result = operator.deployment(workflow.args)
    assert result["phase"] == "provision" and result["reason"] == "os_error"
    assert result["errno"] == 2 and not result["start_attempted"]
    assert result["completed_phases"] == ["source_verify"]
    assert "hidden_import" not in workflow.events
    journal = Path(result["bootstrap_journal"])
    assert journal == Path(result["journal"]) and journal.exists()
    assert journal.stat().st_mode & 0o777 == 0o600
    saved = json.loads(journal.read_text())
    assert saved["phase"] == "provision" and saved["errno"] == 2
    assert "PRIVATE" not in json.dumps(saved)
