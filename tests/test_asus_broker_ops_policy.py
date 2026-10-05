"""Privilege/authentication boundaries; no real sudo, account or Doppler calls."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "deploy/asus/broker_ops_policy.py"
spec = importlib.util.spec_from_file_location("ops_policy", FILE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
PASSWORD = b"fixturePASSWORDonlyMEMORY0000000000000001"
TOKEN = b"fixtureOpsBootstrapOnlyMemory0000001"


class Bridge:
    def __init__(self, *, cached=False, nopasswd=False, race=False, bad_password=False):
        self.cached, self.nopasswd, self.race, self.bad_password = (
            cached,
            nopasswd,
            race,
            bad_password,
        )
        self.argv_env = []
        self.effects, self.password_consumed = [], 0
        self.seen_buffers = []
        self.closed = False

    def probe(self, argv, env):
        self.argv_env.append((argv, env))
        assert "-k" in argv and "-n" in argv and argv[-1] == p.CONTROL
        assert len(argv) == 7  # exact zero helper arguments
        if self.nopasswd:
            assert p.helper_plan([], b"")["executed"] is False
            return "auth_gate_open"
        return "password_required"  # cached session ignored by -k

    def start(self, argv, env):
        self.argv_env.append((argv, env))
        assert "-k" in argv and "-S" in argv and len(argv) == 7
        return self

    def password_line(self, value):
        assert isinstance(value, memoryview)
        self.seen_buffers.append(value.obj)
        self.pending = bytes(value) if self.race else b""
        if not self.race:
            assert bytes(value) == PASSWORD
            self.password_consumed += 1

    def ready(self):
        return b"wrong response" if self.bad_password else p.READY

    def commit(self, request):
        # NOPASSWD after probe leaves password unconsumed; helper must not
        # treat it as an operation, even if the agent sent a later JSON line.
        if self.pending:
            p.helper_plan([], self.pending)
        planned = p.helper_plan([], request)
        self.effects.append(planned["command"])
        self.operation = planned["operation"]

    def result(self):
        return {
            "status": "passed",
            "operation": self.operation,
            "service": "api-quota-broker.service",
            "unexpected_private": "ignore me",
        }

    def close(self):
        self.closed = True


def flow(bridge, fail=False, guard_fail=False, response=None):
    calls, token_buffers = [], []

    def load():
        v = bytearray(TOKEN)
        token_buffers.append(v)
        return v

    def transport(path, headers):
        calls.append(path)
        assert path == p.SECRET_PATH and TOKEN.decode() in headers["Authorization"]
        if fail:
            raise RuntimeError(PASSWORD.decode())
        return (
            (
                200,
                json.dumps(
                    {
                        "name": p.SECRET,
                        "value": {"raw": PASSWORD.decode(), "computed": PASSWORD.decode()},
                    }
                ).encode(),
            )
            if response is None
            else response
        )

    def guard():
        if guard_fail:
            raise RuntimeError("swap/core/dumpability unverified")

    result = p.manage("restart", load, transport, bridge, guard)
    assert PASSWORD.decode() not in json.dumps(result)
    assert TOKEN.decode() not in json.dumps(result)
    assert all(not any(v) for v in token_buffers)
    assert all(not any(v) for v in bridge.seen_buffers)
    assert all(
        PASSWORD.decode() not in repr(v) and TOKEN.decode() not in repr(v) for v in bridge.argv_env
    )
    return result, calls


@pytest.mark.parametrize("cached", [False, True])
def test_fresh_password_consumed_without_ticket_or_secret_exposure(cached, capsys, tmp_path):
    before = list(tmp_path.iterdir())
    bridge = Bridge(cached=cached)
    result, calls = flow(bridge)
    assert result["status"] == "passed" and len(calls) == 1
    assert bridge.password_consumed == 1 and len(bridge.effects) == 1 and bridge.closed
    assert bridge.effects[0] == ("/usr/bin/systemctl", "restart", "api-quota-broker.service")
    assert list(tmp_path.iterdir()) == before and capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["nopasswd", "race", "bad_password"])
def test_bypass_or_password_failure_has_no_management_effects(mode):
    bridge = Bridge(**{mode: True})
    result, calls = flow(bridge)
    assert result["status"] == "blocked" and bridge.effects == []
    assert len(calls) == (0 if mode == "nopasswd" else 1)


def test_doppler_failure_does_not_use_cached_password_or_start_sudo():
    bridge = Bridge(cached=True)
    result, calls = flow(bridge, fail=True)
    assert result["status"] == "blocked" and len(calls) == 1
    assert len(bridge.argv_env) == 1 and bridge.effects == []


def test_memory_guard_before_secret_fetch():
    bridge = Bridge()
    result, calls = flow(bridge, guard_fail=True)
    assert result["status"] == "blocked" and calls == [] and bridge.argv_env == []


def test_cleanup_failure_does_not_report_success_or_retry():
    class CleanupFailure(Bridge):
        def close(self):
            raise RuntimeError(PASSWORD.decode())

    bridge = CleanupFailure()
    result, calls = flow(bridge)
    assert result["code"] == "worker_cleanup_unverified"
    assert result["operation_may_have_completed"] is True
    assert result["automatic_retry"] is False
    assert len(calls) == 1 and len(bridge.effects) == 1


def test_real_anonymous_pipe_handshake_has_no_secret_in_argv_env_or_files(tmp_path):
    # Unprivileged mock consumer only; does not invoke sudo/PAM/systemctl.
    script = """
import hashlib,json,sys
from pathlib import Path
password=sys.stdin.buffer.readline().rstrip(b'\\n')
argv=Path('/proc/self/cmdline').read_bytes()
env=Path('/proc/self/environ').read_bytes()
print('BROKER_AUTH_READY',flush=True)
request=json.loads(sys.stdin.buffer.readline())
print(json.dumps({'digest':hashlib.sha256(password).hexdigest(),
                 'exposed':password in argv or password in env,
                 'operation':request['operation']}),flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=p.SAFE_ENV,
        cwd=tmp_path,
    )
    try:
        process.stdin.write(PASSWORD + b"\n")
        process.stdin.flush()
        assert process.stdout.readline() == p.READY
        process.stdin.write(b'{"operation":"inspect"}\n')
        process.stdin.flush()
        result = json.loads(process.stdout.readline())
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and stdout == stderr == b""
        assert result == {
            "digest": hashlib.sha256(PASSWORD).hexdigest(),
            "exposed": False,
            "operation": "inspect",
        }
        assert PASSWORD not in json.dumps(result).encode()
        assert list(tmp_path.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"operation":"restart","path":"/tmp/code.py"}',
        b'{"operation":"restart","operation":"inspect"}',
        b'{"operation":"systemd-run"}',
        b'{"operation":"orderflow"}',
        b'{"operation":"hp"}',
        b'{"operation":"credentials"}',
        b'{"operation":"journal"}',
        b'{"operation":"activate"}',
        b'{"operation":null}',
        b'{"operation":[]}',
        b"x" * 129,
        PASSWORD,
    ],
)
def test_overreach_or_unconsumed_password_is_rejected(raw):
    with pytest.raises(p.Denied):
        p.helper_plan([], raw)


@pytest.mark.parametrize(
    "args", [["restart"], ["/tmp/code.py"], ["--hook=evil"], ["--service=orderflow.service"]]
)
def test_helper_rejects_every_argument(args):
    with pytest.raises(p.Denied):
        p.helper_plan(args, b'{"operation":"inspect"}')


@pytest.mark.parametrize(
    "relative", ["../x", "/etc/shadow", "x/../y", "x/./y", "x//y", "x/", "x\\y"]
)
def test_path_escape_rejected_before_read(tmp_path, relative):
    with pytest.raises(p.Denied):
        p.read_pinned(tmp_path, relative, "a" * 64, owner=os.getuid())


def test_writable_foreign_symlink_and_unpinned_code(tmp_path):
    tmp_path.chmod(0o700)
    file = tmp_path / "helper.py"
    file.write_bytes(b"fixture code")
    file.chmod(0o644)
    sha = hashlib.sha256(file.read_bytes()).hexdigest()
    assert p.read_pinned(tmp_path, "helper.py", sha, owner=os.getuid()) == b"fixture code"
    for mode in (0o666, 0o664, 0o4755):
        file.chmod(mode)
        with pytest.raises(p.Denied):
            p.read_pinned(tmp_path, "helper.py", sha, owner=os.getuid())
    file.chmod(0o644)
    with pytest.raises(p.Denied):
        p.read_pinned(tmp_path, "helper.py", sha, owner=os.getuid() + 1)
    with pytest.raises(p.Denied):
        p.read_pinned(tmp_path, "helper.py", "a" * 64, owner=os.getuid())
    (tmp_path / "link.py").symlink_to(file)
    with pytest.raises(OSError):
        p.read_pinned(tmp_path, "link.py", sha, owner=os.getuid())
    os.link(file, tmp_path / "hard.py")
    with pytest.raises(p.Denied):
        p.read_pinned(tmp_path, "helper.py", sha, owner=os.getuid())


@pytest.mark.parametrize(
    "response",
    [
        (403, b'{"error":"private"}'),
        (200, b'{"name":"OTHER","value":{}}'),
        (
            200,
            b'{"name":"ASUS_BROKER_DEPLOY_PASSWORD","value":{"raw":"${MORRIS_PASSWORD}","computed":"private"}}',
        ),
        (200, b"[]"),
        (200, b"x" * 8193),
    ],
)
def test_doppler_scope_reference_and_body_bounds_fail_closed(response):
    bridge = Bridge()
    result, calls = flow(bridge, response=response)
    assert result["status"] == "blocked" and bridge.effects == [] and len(calls) == 1


def test_plan_does_not_grant_pool_or_release_and_sudoers_exact_zero_args():
    plan = p.plan()
    assert (
        plan["installable"] is False
        and not plan["pool_once_allowed"]
        and not plan["new_release_allowed"]
    )
    assert "PASSWD:" in p.SUDOERS and "NOPASSWD" not in p.SUDOERS
    assert p.CONTROL + ' ""' in p.SUDOERS
    assert "*" not in p.SUDOERS and "timestamp_timeout=0" in p.SUDOERS
    assert "-v" not in p.sudo_command() and "-A" not in p.sudo_command()
    assert plan["account_shell"] == "/usr/sbin/nologin"
    assert plan["account_ssh_keys"] is False
