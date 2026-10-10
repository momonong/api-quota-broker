"""One-time account/secret boundaries; native account utilities never run."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

FILE = Path(__file__).resolve().parents[1] / "deploy/asus/initialize_broker_deploy.py"
spec = importlib.util.spec_from_file_location("ops_initializer", FILE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
FAKE = b"FIXTURE_PASSWORD_INPUT_ONLY_".ljust(40, b"0") + b"\n"
assert len(FAKE) == 41


class Ops:
    def __init__(self, fail=None, *, rollback=False, existing=False, drift=False):
        self.fail, self.rollback_fail, self.existing, self.drift = fail, rollback, existing, drift
        self.events, self.buffers, self.commands = [], [], []
        self.source_reads, self.rollback_calls, self.service_reads = 0, 0, 0
        self.create_completed = False

    def point(self, name):
        self.events.append(name)
        if self.fail == name:
            raise RuntimeError(FAKE.decode())

    def preflight(self):
        self.point("preflight")
        if self.existing:
            raise p.Blocked("account_or_group_exists")
        return {"broker": "unchanged", "orderflow": "unchanged"}

    def guard(self):
        self.point("guard")

    def password(self):
        self.point("password")
        self.source_reads += 1
        raw = bytearray(FAKE)
        self.buffers.append(raw)
        return raw

    def create(self):
        self.point("create")
        self.commands.append(p.CREATE)

    def set_password(self, data):
        assert type(data) is memoryview and bytes(data) == b"broker-deploy:" + FAKE
        self.buffers.append(data.obj)
        self.commands.append(p.SET_PASSWORD)
        self.point("set_password")

    def lock(self):
        self.point("lock")
        self.commands.append(p.LOCK)

    def verify(self):
        self.point("verify")
        return {
            "uid": 993,
            "gid": 981,
            "shell": "/usr/sbin/nologin",
            "locked": True,
            "expired": True,
            "supplementary_groups": [],
        }

    def services(self):
        self.point("services")
        self.service_reads += 1
        return {"broker": "drift" if self.drift else "unchanged", "orderflow": "unchanged"}

    def rollback(self):
        self.rollback_calls += 1
        if self.rollback_fail:
            raise RuntimeError(FAKE.decode())
        self.commands.append(p.LOCK)
        return True


def check_result(ops):
    result = p.initialize(ops)
    assert FAKE.decode().strip() not in json.dumps(result)
    assert FAKE.decode().strip() not in repr(ops.commands)
    assert all(not any(v) for v in ops.buffers)
    assert result["sudo_access_enabled"] is False and result["source_cleanup_pending"] is True
    assert result["provider_calls"] == result["doppler_calls"] == 0
    return result


def test_success_is_locked_expired_and_never_enables_privileged_ops(capsys):
    ops = Ops()
    result = check_result(ops)
    assert result["status"] == "account_prepared_locked" and ops.source_reads == 1
    assert result["identity"]["locked"] and result["identity"]["expired"]
    assert (
        result["persistent_ops_service"] is False
        and result["doppler_value_match"] == "not_verified"
    )
    assert ops.commands == [p.CREATE, p.SET_PASSWORD, p.LOCK]
    assert "--expiredate" in p.CREATE and p.CREATE[-1] == p.ACCOUNT
    assert p.CREATE[p.CREATE.index("--expiredate") + 1] == "1"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("fail", ["preflight", "guard", "password", "create"])
def test_failure_before_creation_does_not_lock_or_delete_existing_accounts(fail):
    ops = Ops(fail)
    result = check_result(ops)
    assert result["status"] == "blocked" and ops.rollback_calls == 0
    assert p.SET_PASSWORD not in ops.commands and p.LOCK not in ops.commands
    assert ops.source_reads == (1 if fail == "create" else 0)


@pytest.mark.parametrize("fail", ["set_password", "lock", "verify", "services"])
def test_failure_after_creation_rolls_back_to_locked_expired_without_deleting(fail):
    ops = Ops(fail)
    result = check_result(ops)
    assert result["status"] == "blocked" and ops.rollback_calls == 1
    assert result["rollback_lock_verified"] is True and result["automatic_retry"] is False
    assert ops.commands[-1] == p.LOCK


def test_existing_account_stops_before_secret_read_and_is_not_changed():
    ops = Ops(existing=True)
    result = check_result(ops)
    assert result["code"] == "account_or_group_exists"
    assert ops.events == ["preflight"] and ops.commands == [] and ops.source_reads == 0


@pytest.mark.parametrize("safe_identity", [True, False])
def test_partial_create_is_not_reported_as_no_mutation(safe_identity):
    class PartialCreate(Ops):
        def create(self):
            self.commands.append(p.CREATE)
            self.create_completed = True
            raise RuntimeError(FAKE.decode())

        def rollback(self):
            self.rollback_calls += 1
            if safe_identity:
                self.commands.append(p.LOCK)
            return safe_identity

    ops = PartialCreate()
    result = check_result(ops)
    assert result["account_may_exist"] is True and ops.rollback_calls == 1
    assert result["rollback_lock_verified"] is safe_identity
    assert result["automatic_retry"] is False and p.SET_PASSWORD not in ops.commands
    assert p.LOCK in ops.commands if safe_identity else p.LOCK not in ops.commands


def test_native_partial_create_reprobes_exact_identity_and_locks_when_safe():
    native = p.NativeOps()
    native.runtime_uid = 995
    native.runtime_gid = 982
    account = SimpleNamespace(
        pw_uid=993, pw_gid=981, pw_shell="/usr/sbin/nologin", pw_dir="/nonexistent"
    )
    group = SimpleNamespace(gr_gid=981, gr_mem=[])
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if argv == p.STATUS:
            return "broker-deploy L 2026-10-05 0 99999 7 -1\n"
        if argv == p.EXPIRY:
            return "Account expires : 1970-01-02\n"
        return None

    with (
        patch.object(native, "_run", side_effect=run),
        patch.object(
            p.pwd, "getpwnam", side_effect=[KeyError("fixture"), account, account, account]
        ),
        patch.object(p.grp, "getgrnam", return_value=group),
        patch.object(p.os, "getgrouplist", return_value=[981]),
    ):
        with pytest.raises(KeyError):
            native.create()
        assert native.create_completed and native.new_uid is None
        assert native.rollback() is True
    assert commands == [p.CREATE, p.LOCK, p.STATUS, p.EXPIRY]


def test_native_partial_create_refuses_to_lock_unverified_identity():
    native = p.NativeOps()
    native.runtime_uid = 995
    native.runtime_gid = 982
    native.create_completed = True
    account = SimpleNamespace(pw_uid=0, pw_gid=0, pw_shell="/bin/bash", pw_dir="/root")
    with (
        patch.object(native, "_run", side_effect=AssertionError("no mutation")) as runner,
        patch.object(p.pwd, "getpwnam", return_value=account),
        patch.object(p.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=0, gr_mem=[])),
    ):
        assert native.rollback() is False
    runner.assert_not_called()


def test_rollback_exception_is_redacted_and_requires_manual_review():
    ops = Ops("verify", rollback=True)
    result = check_result(ops)
    assert result["status"] == "blocked" and result["rollback_lock_verified"] is False
    assert result["automatic_retry"] is False and result["account_may_exist"] is True


def test_service_drift_does_not_restart_broker_or_orderflow():
    ops = Ops(drift=True)
    result = check_result(ops)
    assert result["code"] == "existing_services_changed" and ops.commands[-1] == p.LOCK


def fixture_source(tmp_path):
    anchor = tmp_path / "home"
    anchor.mkdir(mode=0o700)
    parent = anchor
    for name in p.SOURCE_PARTS[:-1]:
        parent = parent / name
        parent.mkdir(mode=0o700)
    file = parent / p.SOURCE_PARTS[-1]
    file.write_bytes(FAKE)
    file.chmod(0o600)
    return anchor, file


def test_real_fd_read_is_one_time_and_original_file_is_preserved(tmp_path, capsys):
    anchor, file = fixture_source(tmp_path)
    before = file.stat()
    value = p.source_password(anchor, owner=os.getuid(), group=os.getgid())
    assert value == FAKE
    p.wipe(value)
    assert not any(value) and file.read_bytes() == FAKE
    assert file.stat().st_ino == before.st_ino and file.stat().st_mtime_ns == before.st_mtime_ns
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "attack", ["symlink", "hardlink", "writable_parent", "writable_file", "oversize", "wrong_owner"]
)
def test_untrusted_password_source_rejected_before_body_read(tmp_path, attack):
    anchor, file = fixture_source(tmp_path)
    owner = os.getuid()
    if attack == "symlink":
        original = file.with_suffix(".original")
        file.rename(original)
        file.symlink_to(original)
    elif attack == "hardlink":
        os.link(file, file.with_suffix(".link"))
    elif attack == "writable_parent":
        file.parent.chmod(0o777)
    elif attack == "writable_file":
        file.chmod(0o644)
    elif attack == "oversize":
        file.write_bytes(FAKE * 2)
    else:
        owner += 1
    with (
        patch("os.read", side_effect=AssertionError("source body must not be read")) as reader,
        pytest.raises(p.Blocked, match="source_input_unverified"),
    ):
        p.source_password(anchor, owner=owner, group=os.getgid())
    reader.assert_not_called()


@pytest.mark.parametrize("raw", [b"x" * 40 + b"\x00", b"x" * 39 + b" \n", b"x" * 39 + b":\n"])
def test_bad_password_format_is_not_forwarded_to_account_tools(tmp_path, raw):
    anchor, file = fixture_source(tmp_path)
    file.write_bytes(raw)
    with pytest.raises(p.Blocked, match="source_input_unverified"):
        p.source_password(anchor, owner=os.getuid(), group=os.getgid())
    assert file.read_bytes() == raw


def test_native_adapter_passes_password_only_via_anonymous_stdin():
    native = p.NativeOps()
    payload = bytearray(b"broker-deploy:" + FAKE)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert tuple(argv) == p.SET_PASSWORD
        assert FAKE.decode().strip() not in repr(argv) + repr(kwargs["env"])
        assert isinstance(kwargs["input"], memoryview)
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0)

    with patch("subprocess.run", side_effect=fake_run):
        native.set_password(memoryview(payload))
        with pytest.raises(p.Blocked, match="command_denied"):
            native._run(("/usr/bin/bash", "-c", "arbitrary"))
    p.wipe(payload)
    assert calls == [p.SET_PASSWORD] and not any(payload)


def test_bootstrap_fixed_pin_no_stdin_shell_and_publication_guard(tmp_path):
    file = tmp_path / "init-review.sh"
    file.write_text(p.bootstrap_wrapper("a" * 64))
    check = subprocess.run(
        ["/usr/bin/bash", "-n", str(file)], capture_output=True, check=False, timeout=5
    )
    assert check.returncode == 0
    run = subprocess.run(["/usr/bin/bash", str(file)], capture_output=True, check=False, timeout=5)
    assert run.returncode == 1 and b"not published" in run.stderr
    body = file.read_text().split("<<'PYROOT'\n", 1)[1].split("\nPYROOT", 1)[0]
    compile(body, "root-copy-bootstrap", "exec")
    assert "bash -s" not in file.read_text() and "MemorySwapMax=0" in body
    assert "--pty" not in body and "--pipe" in body


def test_enabled_wrapper_is_separate_and_does_not_run_account_commands_in_review(tmp_path):
    text = p.bootstrap_wrapper("a" * 64, enabled=True)
    assert "not published" not in text and "exit 1" not in text.split("set -eu", 1)[0]
    file = tmp_path / "enabled.sh"
    file.write_text(text)
    result = subprocess.run(
        ["/usr/bin/bash", "-n", str(file)], capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0  # syntax only; enabled wrapper is never executed


def test_invalid_cli_arguments_do_not_echo_private_input(capsys):
    with patch.object(sys, "argv", ["fixture", FAKE.decode().strip()]):
        assert p.main() == 2
    value = capsys.readouterr().out
    assert "arguments_denied" in value and FAKE.decode().strip() not in value


def test_default_plan_has_no_secret_or_native_operation():
    with (
        patch("os.open", side_effect=AssertionError("no file reads")),
        patch("subprocess.run", side_effect=AssertionError("no commands")),
    ):
        value = p.plan()
    assert value["secret_value_reads"] == value["host_changes"] == value["token_creation"] == 0
    assert not value["sudo_access_enabled"] and not value["persistent_ops_service"]
