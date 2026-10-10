"""Fresh local packet QA; no root, network, production state, or service commands."""

import ast
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "audit_builder", ROOT / "deploy/asus/build_history_audit_review.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def build(path):
    receipt = b.build(path)
    for name, sha in receipt["files"].items():
        file = path / name
        assert hashlib.sha256(file.read_bytes()).hexdigest() == sha
        assert file.stat().st_mode & 0o777 == 0o600 and file.stat().st_nlink == 1
    assert path.stat().st_mode & 0o777 == 0o700
    return receipt


def verify(path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(path / "verify_history_audit_offline.py"),
            "--directory",
            str(path),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.stderr == b""
    return result.returncode, json.loads(result.stdout)


def test_actual_build_verify_and_default_wrapper_are_zero_effect(tmp_path):
    packet = tmp_path / "review"
    build(packet)
    status, result = verify(packet)
    assert status == 0 and result["status"] == "passed"
    assert result["checks"]["four_classes"] and result["kernel_core_zero"]
    assert result["production_private_reads"] == result["credential_reads"] == 0
    assert result["provider_calls"] == result["service_commands"] == 0
    wrapper = packet / "history-audit-once.sh"
    syntax = subprocess.run(
        ["/bin/bash", "-n", str(wrapper)], capture_output=True, timeout=5, check=False
    )
    assert syntax.returncode == 0 and syntax.stdout == syntax.stderr == b""
    default = subprocess.run(
        ["/bin/bash", str(wrapper)], capture_output=True, timeout=5, check=False
    )
    assert default.returncode == 0 and json.loads(default.stdout)["native_executed"] is False
    reject = subprocess.run(
        ["/bin/bash", str(wrapper), "--apply"], capture_output=True, timeout=5, check=False
    )
    assert reject.returncode == 1 and reject.stdout == reject.stderr == b""


@pytest.mark.parametrize(
    "name", ["history-audit-payload.tar", "seal.json", "history-audit-once.sh"]
)
def test_tamper_or_pin_mismatch_is_rejected_without_raw_errors(tmp_path, name):
    packet = tmp_path / "review"
    build(packet)
    file = packet / name
    file.write_bytes(
        file.read_bytes().replace(b"private_history_readonly_review", b"UNTRUSTED_ROW")
        if name == "seal.json"
        else file.read_bytes() + b"INVALID_PUBLIC_BYTES"
    )
    # Wrapper corruption must change the embedded chain to exercise the
    # verifier's internal check; its whole-file SHA is verified externally.
    if name == "history-audit-once.sh":
        file.write_bytes(file.read_bytes().replace(b"expected=", b"unexpected="))
    status, result = verify(packet)
    assert status == 1 and result == {
        "status": "blocked",
        "code": "offline_validation_unverified",
        "automatic_retry": False,
    }


def test_symlink_rejected_and_existing_destination_preserved(tmp_path):
    packet = tmp_path / "review"
    receipt = build(packet)
    with pytest.raises(FileExistsError):
        b.build(packet)
    assert {
        name: hashlib.sha256((packet / name).read_bytes()).hexdigest() for name in receipt["files"]
    } == receipt["files"]
    source = packet / "history-audit-payload.tar"
    other = tmp_path / "payload"
    source.rename(other)
    source.symlink_to(other)
    assert verify(packet)[0] == 1


def test_native_argument_is_fully_compiled_and_has_no_credentials_or_apply_phase(tmp_path):
    packet = tmp_path / "review"
    build(packet)
    shell = (packet / "history-audit-once.sh").read_text()
    words = shlex.split(shell[shell.index("exec /usr/bin/sudo -n") :])
    assert words[:8] == [
        "exec",
        "/usr/bin/sudo",
        "-n",
        "--",
        "/usr/bin/python3.14",
        "-I",
        "-B",
        "-S",
    ]
    code = words[words.index("-c") + 1]
    compile(code, "<complete-native-argument>", "exec")
    tree = ast.parse(code)
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "--audit-only" in constants and "--apply" not in constants
    assert "--property=ProtectSystem=strict" in constants
    assert "--property=PrivateNetwork=yes" in constants
    assert not any("LoadCredential" in value for value in constants)
    assert "/usr/bin/sudo -v" in shell and "bash -s" not in shell
