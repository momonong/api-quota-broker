"""Actual controlling PTY/shell reproduction; all input is synthetic, no sudo."""

import importlib.util
import io
import json
import os
import select
import shlex
import subprocess
import sys
import termios
import time
from pathlib import Path
from unittest.mock import patch

import pytest

FILE = Path(__file__).resolve().parents[1] / "deploy/asus/pool_entry_ttyfix.py"
spec = importlib.util.spec_from_file_location("pool_entry_ttyfix", FILE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
PASSWORD = "FIXTURE_ONLY_PASSWORD_2026_NOT_REAL"
EXTRA = "FIXTURE_ONLY_SECOND_INPUT_NOT_REAL"


def root_fixture(tmp_path):
    # Preserve the legacy ROOT tail's precise line positions, including nested
    # heredoc, but replace all privileged staging/service calls with local mocks.
    child = """import json,sys
print('FIXTURE_CHILD_STARTED',flush=True)
line=sys.stdin.buffer.readline()
print(json.dumps({'application_input_received':bool(line),'stdin_tty':sys.stdin.isatty()}),flush=True)
"""
    lines = ["set -eu", "umask 077", "/usr/bin/python3 -I -B - \"$1\" <<'PYROOT'"]
    code = [
        "from pathlib import Path",
        "import sys",
        "Path(sys.argv[1]).mkdir(mode=0o700)",
    ]
    code += ["# harmless fixture padding"] * (34 - len(code) - 1)
    code += ["print('FIXTURE_BOOTSTRAP_STAGED',flush=True)"]
    lines += code
    lines += [
        "PYROOT",
        "# Restore TTY input after the literal bootstrap heredoc before --pty.",
        "exec </dev/tty >/dev/tty 2>/dev/tty",
        "exec /usr/bin/python3 -I -B -c " + shlex.quote(child),
    ]
    assert lines[39] == "exec </dev/tty >/dev/tty 2>/dev/tty"
    return "\n".join(lines) + "\n", str(tmp_path / "bootstrap-fixture")


def run_pty(tmp_path, fixed):
    root, directory = root_fixture(tmp_path)
    argv = list(p.root_argv(root, directory)) if fixed else ["/usr/bin/bash", "-s", "--", directory]
    launcher = f"""
import fcntl,getpass,json,os,sys,termios
fcntl.ioctl(1,termios.TIOCSCTTY,0)
before=termios.tcgetattr(1)
value=getpass.getpass('FIXTURE sudo password (hidden): ')
print(json.dumps({{'auth_input_length':len(value),'auth_echo_restored':termios.tcgetattr(1)==before,
                  'auth_input_in_argv':value in repr(sys.argv),'auth_input_in_env':value in repr(dict(os.environ))}}),flush=True)
os.execv({argv[0]!r},{argv!r})
"""
    master, slave = os.openpty()
    process, output = None, b""
    before = termios.tcgetattr(slave)
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", launcher],
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        # Legacy reads its program from the pipe; fixed -c already has the same
        # program in argv and receives EOF, never a TTY as its parser source.
        process.stdin.write(root.encode() if not fixed else b"")
        process.stdin.close()
        deadline = time.monotonic() + 8

        def until(marker):
            nonlocal output
            while marker not in output:
                assert time.monotonic() < deadline, "fixture PTY stage timed out"
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output += os.read(master, 8192)
                assert process.poll() is None or marker in output, "fixture exited before stage"

        until(b"FIXTURE sudo password (hidden): ")
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, PASSWORD.encode() + b"\n")
        until(b"FIXTURE_BOOTSTRAP_STAGED")
        if fixed:
            until(b"FIXTURE_CHILD_STARTED")
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, EXTRA.encode() + b"\n")
        until(b'"application_input_received"' if fixed else b"command not found")
        exitcode = process.wait(timeout=3)
        assert PASSWORD.encode() not in output
        assert termios.tcgetattr(slave) == before
        auth = next(
            json.loads(v)
            for v in output.decode().replace("\r", "").splitlines()
            if v.startswith('{"auth_input_length"')
        )
        assert auth == {
            "auth_input_length": len(PASSWORD),
            "auth_echo_restored": True,
            "auth_input_in_argv": False,
            "auth_input_in_env": False,
        }
        return exitcode, output, directory
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)


def test_legacy_real_pty_reproduces_line41_tty_input_as_command(tmp_path):
    exitcode, output, directory = run_pty(tmp_path, fixed=False)
    assert exitcode == 127
    assert b"line 41:" in output and EXTRA.encode() in output
    assert b"FIXTURE_CHILD_STARTED" not in output
    assert Path(directory).is_dir()  # staging completed before parser switched input


def test_fixed_real_pty_launches_child_and_never_parses_tty_input(tmp_path):
    exitcode, output, directory = run_pty(tmp_path, fixed=True)
    assert exitcode == 0 and b"command not found" not in output
    assert b'"application_input_received": true' in output and b'"stdin_tty": true' in output
    assert Path(directory).is_dir()


def test_candidate_is_disabled_and_bash_syntax_valid(tmp_path):
    file = tmp_path / "candidate.sh"
    file.write_text(p.wrapper())
    check = subprocess.run(
        ["/usr/bin/bash", "-n", str(file)], capture_output=True, timeout=5, check=False
    )
    assert check.returncode == 0 and check.stdout == check.stderr == b""
    run = subprocess.run(["/usr/bin/bash", str(file)], capture_output=True, timeout=5, check=False)
    assert run.returncode == 1 and b"private-state review required" in run.stderr
    compile(p.bootstrap_python(), "candidate-inline-bootstrap", "exec")


def test_boundary_error_never_prints_private_exception_text():
    stdout = io.StringIO()
    with (
        patch("os.geteuid", return_value=0),
        patch.object(Path, "lstat", side_effect=PermissionError(PASSWORD)),
        patch.object(sys, "argv", ["fixture", "/unused.tar", "/fixed/bootstrap"]),
        patch.object(sys, "stdout", stdout),
        pytest.raises(SystemExit) as error,
    ):
        exec(compile(p.bootstrap_python(), "fixture-inline", "exec"), {})  # noqa: S102 - fixed reviewed code; root metadata syscalls replaced by fixture errors.
    assert error.value.code == 2
    result = json.loads(stdout.getvalue())
    assert result["stage"] == "bootstrap_metadata"
    assert result["code"] == "bootstrap_boundary_unverified"
    assert PASSWORD not in stdout.getvalue()


@pytest.mark.parametrize("node", ["directory", "symlink", "file"])
def test_collision_error_has_stage_and_safe_actual_metadata_without_changes(tmp_path, node):
    # Execute the actual inline bootstrap only up to collision. Root metadata
    # syscalls use explicit fixture shims; later archive/service work cannot run.
    destination = tmp_path / "collision"
    if node == "directory":
        destination.mkdir()
    elif node == "symlink":
        destination.symlink_to(tmp_path / "absent")
    else:
        destination.write_text("nonsecret fixture")
    actual = destination.lstat()
    namespace = {"__name__": "fixture"}
    real_stat = Path.lstat

    def mapped_root(path):
        if (
            path == destination
            or path == destination.parent / "provider-pool-2026-10-04-r1.claim.json"
            or path == destination.parent / "provider-pool-2026-10-04-r1.json"
        ):
            return real_stat(path)

        # Ancestor owner/mode fixture only, no host permission mutation.
        class RootDirectory:
            st_mode = 0o40700
            st_uid = st_gid = 0

        return RootDirectory()

    stdout = io.StringIO()
    with (
        patch("os.geteuid", return_value=0),
        patch.object(Path, "lstat", mapped_root),
        patch.object(sys, "argv", ["fixture", str(tmp_path / "unused.tar"), str(destination)]),
        patch.object(sys, "stdout", stdout),
        pytest.raises(SystemExit) as error,
    ):
        exec(compile(p.bootstrap_python(), "fixture-inline", "exec"), namespace)  # noqa: S102 - fixed reviewed code; privileged metadata syscalls are fixture shims.
    assert error.value.code == 2
    result = json.loads(stdout.getvalue())
    assert result["code"] == "bootstrap_path_present" and result["stage"] == "bootstrap_collision"
    assert result["actual"]["type"] == ("regular" if node == "file" else node)
    assert result["automatic_retry"] is False and result["this_entry_provider_dispatch"] is False
    assert destination.lstat() == actual
    assert not (tmp_path / "unused.tar").exists()
