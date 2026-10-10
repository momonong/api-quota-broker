"""Local review of future transfer; every transport is fake, no SSH or network."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "audit_transfer", ROOT / "deploy/asus/transfer_history_audit_review.py"
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def baseline():
    return {
        "current": "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770",
        "services": {
            name: {
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": "100",
                "ExecMainStartTimestampMonotonic": "123456",
                "NRestarts": "0",
            }
            for name in ("api-quota-broker.service", "orderflow.service", "ssh.service")
        },
        "public_pins": {
            name: "a" * 64
            for name in (
                "/etc/systemd/system/api-quota-broker.service",
                "/usr/local/lib/api-quota-broker-ops/ops_entry.py",
                "/etc/api-quota-broker-ops/policy.json",
            )
        },
    }


def fixture(tmp_path, monkeypatch):
    review = tmp_path / "review"
    spec = importlib.util.spec_from_file_location(
        "transfer_public_builder", ROOT / "deploy/asus/build_history_audit_review.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    receipt = builder.build(review)
    # Preserve the transport's production pin check; source drift must fail here.
    assert receipt["files"] == t.FILES
    monkeypatch.setattr(t, "REVIEW", review)
    monkeypatch.setattr(t, "CLAIM", tmp_path / "claim.json")
    monkeypatch.setattr(t, "RECEIPT", tmp_path / "receipt.json")
    return review


def test_default_and_invalid_arguments_never_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(t, "execute", lambda: pytest.fail("transport must not start"))
    monkeypatch.setattr(sys, "argv", ["PUBLIC"])
    assert t.main() == 0 and json.loads(capsys.readouterr().out)["network_calls"] == 0
    monkeypatch.setattr(sys, "argv", ["PUBLIC", "--audit-only"])
    assert (
        t.main() == 1
        and json.loads(capsys.readouterr().out)["code"] == "review_transfer_unverified"
    )


def test_local_tamper_rejected_before_transport(tmp_path, monkeypatch):
    review = fixture(tmp_path, monkeypatch)
    (review / "history-audit-payload.tar").write_bytes(b"PUBLIC_TAMPER")
    with pytest.raises(t.Blocked, match="local_files_unverified"):
        t.execute(run=lambda *_a, **_k: pytest.fail("SSH before local verification"))
    assert not t.CLAIM.exists()


def test_batch_quotes_fixed_paths_and_rejects_control_characters(tmp_path):
    value = t.batch(tmp_path / "public space").decode()
    assert value.count("put ") == 4 and value.startswith('mkdir "' + t.REMOTE + '"\n')
    assert "sudo" not in value and "rm " not in value and "rename " not in value
    with pytest.raises(t.Blocked, match="local_path_untrusted"):
        t.batch(Path('/tmp/PUBLIC"\nBAD'))


def test_remote_collision_fails_before_snapshot_or_any_process(tmp_path, monkeypatch, capsys):
    code = t.remote_code().replace(
        "remote=Path(" + repr(t.REMOTE) + ")", "remote=Path(" + repr(str(tmp_path)) + ")"
    )
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server"))
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)
    monkeypatch.setattr(
        t.subprocess, "run", lambda *_a, **_k: pytest.fail("collision must precede commands")
    )
    with pytest.raises(SystemExit):
        exec(compile(code, "<public-collision-fixture>", "exec"), {})  # noqa: S102 - public fixture only.
    assert json.loads(capsys.readouterr().out)["code"] == "remote_review_unverified"


def test_preflight_failure_creates_no_intent_and_does_not_upload(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    calls = []

    def fail(argv, **_):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=b"")

    with pytest.raises(t.Blocked, match="remote_review_unverified"):
        t.execute(run=fail)
    assert len(calls) == 1 and calls[0][0] == "ssh" and not t.CLAIM.exists()


@pytest.mark.parametrize("sftp_status", [0, 1])
def test_fake_transfer_has_one_sftp_and_permanent_unknown_claim(tmp_path, monkeypatch, sftp_status):
    fixture(tmp_path, monkeypatch)
    calls = []
    success = {
        "status": "passed",
        "mode": "review_upload_and_offline_only",
        "files_verified": 4,
        "three_services_and_public_pins_preserved": True,
        "kernel_core_zero": True,
        "kernel_dumpable_zero": True,
        "python": "3.14",
        "provider_calls": 0,
        "credential_reads": 0,
        "native_audit_executed": False,
        "root_executed": False,
    }

    def fake(argv, **kwargs):
        calls.append(argv)
        assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
        if argv[0] == "sftp":
            assert t.CLAIM.exists() and kwargs["input"] == t.batch(t.REVIEW)
            return SimpleNamespace(returncode=sftp_status, stdout=b"")
        compile(
            t.remote_code(offline=len(calls) > 1, before=baseline()),
            "<remote-syntax-fixture>",
            "exec",
        )
        result = (
            {"status": "passed", "mode": "review_transfer_preflight", "baseline": baseline()}
            if len(calls) == 1
            else success
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())

    result = t.execute(run=fake)
    assert result["status"] == ("passed" if sftp_status == 0 else "blocked")
    assert [argv[0] for argv in calls] == (
        ["ssh", "sftp", "ssh"] if sftp_status == 0 else ["ssh", "sftp"]
    )
    assert t.CLAIM.exists() and t.RECEIPT.exists()
    with pytest.raises(t.Blocked, match="transfer_claim_exists"):
        t.execute(run=lambda *_a, **_k: pytest.fail("automatic resend"))


def test_invalid_remote_baseline_never_serializes_private_values():
    value = baseline()
    value["services"]["ssh.service"]["MainPID"] = "PUBLIC_PRIVATE_VALUE"
    with pytest.raises(t.Blocked, match="remote_review_unverified"):
        t.check_baseline(value)
