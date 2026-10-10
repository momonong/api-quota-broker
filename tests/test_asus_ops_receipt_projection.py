"""Public fixtures only; never access ASUS/root/private production receipts."""

import importlib.util
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "deploy/asus/project_ops_receipts.py"
spec = importlib.util.spec_from_file_location("receipt_projector", SOURCE)
projector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projector)
FAKE = "PUBLIC_FIXTURE_NOT_A_SECRET_00000000"


def test_safe_projection_drops_raw_secrets_paths_unknown_enums_and_type_confusion():
    data = {
        "status": "blocked",
        "stage": "preflight",
        "check": "parent_metadata",
        "code": "ssh_fragment_exists",
        "rollback_verified": True,
        "mode": "asus_ops_socket_initialization",
        "provider_calls": False,
        "password": FAKE,
        "token": FAKE,
        "raw_stdout": FAKE,
        "path": FAKE,
        "preflight_checks_passed": ["identity", FAKE],
        "native_restart_executed": FAKE,
    }
    result = projector.safe_fields(data)
    assert result == {
        "status": "blocked",
        "stage": "preflight",
        "check": "parent_metadata",
        "code": "ssh_fragment_exists",
        "rollback_verified": True,
        "mode": "asus_ops_socket_initialization",
        "preflight_checks_passed": ["identity"],
    }
    assert FAKE not in json.dumps(result)
    assert projector.safe_fields({"status": FAKE, "code": FAKE, "checkpoint": FAKE}) == {}


def test_old_prepared_and_future_progress_are_distinct():
    old = projector.safe_fields(
        {
            "status": "prepared",
            "mode": "asus_ops_socket_bootstrap",
            "preflight_checks_passed": ["bootstrap_guard"],
            "provider_calls": 0,
        }
    )
    assert "checkpoint" not in old
    new = projector.safe_fields(
        {
            "mode": "asus_ops_socket_bootstrap_progress",
            "checkpoint": "ssh_reload_verified",
            "raw_response": FAKE,
        }
    )
    assert new == {
        "mode": "asus_ops_socket_bootstrap_progress",
        "checkpoint": "ssh_reload_verified",
    }


def test_successful_collection_reads_only_receipt_pattern_and_emits_safe_rows(
    monkeypatch, tmp_path, capsys
):
    folder = tmp_path / ("ops-bootstrap-" + "a" * 32)
    folder.mkdir()
    receipt = folder / ("receipt-" + "b" * 16 + ".json")
    receipt.write_text(
        json.dumps(
            {
                "status": "prepared",
                "mode": "asus_ops_socket_bootstrap",
                "token": FAKE,
                "provider_calls": 0,
            }
        )
    )
    (folder / "ops_doppler.cred").write_text(FAKE)
    (tmp_path / "unrelated-secret-backup").write_text(FAKE)
    monkeypatch.setattr(projector, "BASE", tmp_path)
    monkeypatch.setattr(projector.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        projector.os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server")
    )
    monkeypatch.setattr(projector, "trusted_dir", lambda *a, **kw: None)
    reads = []

    def read(path):
        reads.append(path.name)
        assert path == receipt
        return path.read_bytes(), 123

    monkeypatch.setattr(projector, "receipt_bytes", read)
    assert projector.main(["--read-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "projected" and len(result["receipts"]) == 1
    assert result["receipts"][0]["mtime_ns"] == "123"
    assert reads == [receipt.name] and FAKE not in json.dumps(result)


@pytest.mark.parametrize("failure", [OSError(13, FAKE), ValueError(FAKE), KeyboardInterrupt(FAKE)])
def test_failure_stdout_is_fixed_and_never_raw(monkeypatch, capsys, failure):
    def fail():
        raise failure

    monkeypatch.setattr(projector, "collect", fail)
    assert projector.main(["--read-only"]) == 1
    result = capsys.readouterr()
    assert json.loads(result.out) == {
        "status": "blocked",
        "code": "ops_receipt_projection_unverified",
        "raw_output_shown": False,
    }
    assert FAKE not in result.out + result.err


def test_no_root_data_read_by_default_or_invalid_arguments(monkeypatch, capsys):
    monkeypatch.setattr(projector, "collect", lambda: pytest.fail("read root data"))
    assert projector.main([]) == 0
    assert json.loads(capsys.readouterr().out)["reads"] == 0
    assert projector.main(["--read-only", "--base", str(ROOT)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "ops_receipt_projection_unverified"


def test_normal_sudo_command_is_self_contained_and_compiled():
    command = projector.normal_sudo_command(SOURCE.read_text())
    args = shlex.split(command)
    assert args[:7] == ["sudo", "--", "/usr/bin/python3.14", "-I", "-B", "-S", "-c"]
    assert args[-1] == "--read-only" and args[7] == SOURCE.read_text()
    compile(args[7], "<fixture-inline>", "exec")


def root_stat(original, **changes):
    values = {
        k: getattr(original, k)
        for k in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    }
    values.update(st_uid=0, st_gid=0)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "case", ["safe", "foreign_owner", "wrong_mode", "oversize", "hardlink", "symlink"]
)
def test_receipt_file_boundary_uses_nofollow_root_single_link_and_bounds(
    monkeypatch, tmp_path, case
):
    target = tmp_path / "receipt.json"
    target.write_bytes(b'{"status":"prepared"}')
    target.chmod(0o600)
    if case == "symlink":
        link = tmp_path / "link"
        link.symlink_to(target)
        target = link
    if case == "hardlink":
        os.link(target, tmp_path / "second")
    real = os.fstat
    changes = {
        "foreign_owner": {"st_uid": 1000},
        "wrong_mode": {"st_mode": 0o100644},
        "oversize": {"st_size": projector.MAX_BYTES + 1},
    }.get(case, {})
    monkeypatch.setattr(projector.os, "fstat", lambda fd: root_stat(real(fd), **changes))
    if case == "safe":
        assert projector.receipt_bytes(target)[0] == b'{"status":"prepared"}'
    else:
        with pytest.raises((ValueError, OSError)):
            projector.receipt_bytes(target)


def test_trusted_folder_rejects_dangling_symlink(monkeypatch, tmp_path):
    link = tmp_path / "ops-bootstrap-test"
    link.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError):
        projector.trusted_dir(link, private=True)


@pytest.mark.parametrize("raw", [b'{"status":"prepared","status":"passed"}', b'{"n":NaN}', b"[]"])
def test_receipt_json_rejects_ambiguous_or_nonobject(raw):
    with pytest.raises(ValueError):
        projector.strict_json(raw)


def test_nanosecond_evidence_survives_exact_decimal_string():
    evidence = json.loads(
        (ROOT / "tests/fixtures/asus-history/r8-prepared-projection.json").read_text()
    )
    value = evidence["projection"]["receipts"][0]["mtime_ns"]
    assert type(value) is str and int(value) == 1791260682969549998
    assert str(int(value)) == value
    raw = (ROOT / "tests/fixtures/asus-history/r8-prepared-projection.numeric.json").read_text()
    assert '"mtime_ns": 1791260682969549998' in raw


def test_nested_recovery_predicate_projection_drops_unknown_values():
    result = projector.safe_fields(
        {
            "recovery_source_failure": {
                "predicate": "helper_effective_path",
                "field": "sshdsessionpath",
                "user": "broker-deploy",
                "actual": "missing",
                "path": FAKE,
                "token": FAKE,
                "unexpected": FAKE,
            }
        }
    )
    assert result == {
        "recovery_source_failure": {
            "predicate": "helper_effective_path",
            "field": "sshdsessionpath",
            "user": "broker-deploy",
            "actual": "missing",
        }
    }
    assert FAKE not in json.dumps(result)
