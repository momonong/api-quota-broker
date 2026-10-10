"""Sealed artifact QA; never root, network, native installation, or audit."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "history_packet", ROOT / "deploy/asus/build_history_ops_review.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def verify(receipt):
    directory = Path(receipt["review_directory"])
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(directory / "verify_history_ops_offline.py"),
            "--directory",
            str(directory),
            "--wrapper-sha256",
            receipt["files"]["history-ops-upgrade-once.sh"],
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.stderr == b""
    return result.returncode, json.loads(result.stdout)


def test_outer_archive_inner_seal_wrapper_and_actual_offline_verifier(tmp_path):
    receipt = b.build(tmp_path / "review")
    need = receipt["files"]
    assert (
        len(need) == 12
        and hashlib.sha256(Path(receipt["bundle_path"]).read_bytes()).hexdigest()
        == receipt["bundle_sha256"]
    )
    with tarfile.open(receipt["bundle_path"], "r:") as archive:
        rows = archive.getmembers()
        assert len(rows) == 12 and {row.name for row in rows} == set(need)
        for row in rows:
            assert (
                row.isfile()
                and row.uid == row.gid == row.mtime == 0
                and row.mode == 0o600
                and not row.pax_headers
            )
            assert hashlib.sha256(archive.extractfile(row).read()).hexdigest() == need[row.name]
    status, value = verify(receipt)
    assert status == 0 and value["source_files_verified"] == 10 and value["default_plan_io"] == 0
    assert value["usage_76_25_projected"] and value["readonly_sqlite_fixture"]
    assert value["native_systemd_sandbox_verified"] is False and value["Doppler_GET"] == 0
    wrapper = Path(receipt["review_directory"]) / "history-ops-upgrade-once.sh"
    for arguments in (["-n", str(wrapper)], [str(wrapper)]):
        result = subprocess.run(
            ["/bin/bash", *arguments], capture_output=True, timeout=5, check=False
        )
        assert result.returncode == 0 and result.stderr == b""
    with pytest.raises(FileExistsError):
        b.build(tmp_path / "review")


@pytest.mark.parametrize(
    "name", ["history_ops_entry.py", "history-ops-upgrade-once.sh", "seal.json"]
)
def test_source_and_root_shell_tamper_rejected_before_any_host_action(tmp_path, name):
    receipt = b.build(tmp_path / "review")
    file = Path(receipt["review_directory"]) / name
    file.write_bytes(file.read_bytes() + b"PUBLIC_INVALID_TAMPER")
    status, result = verify(receipt)
    assert status == 1 and result == {
        "status": "blocked",
        "code": "history_ops_offline_unverified",
        "automatic_retry": False,
    }
