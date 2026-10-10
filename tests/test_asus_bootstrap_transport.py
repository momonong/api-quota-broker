"""Real pipes and child processes with fake credentials; no sudo/Doppler/SSH."""

import hashlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bootstrap_transport", ROOT / "deploy/asus/bootstrap_transport.py"
)
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)
FAKE = b"fixture-only password $ ; quote'"
SUCCESS = {
    "status": "passed",
    "stage": "complete",
    "automatic_retry": False,
    "maintenance_installed": True,
    "normal_v1_deployed": False,
    "provider_posts": 0,
}


def python(code):
    return [sys.executable, "-I", "-B", "-S", "-c", code]


def fake_sudo(tail="", prompt=True):
    return python(
        "import sys,os,json,time\n"
        + (f"sys.stderr.write({t.PROMPT.decode()!r});sys.stderr.flush()\n" if prompt else "")
        + ("value=sys.stdin.buffer.readline().rstrip(b'\\n')\n" if prompt else "")
        + tail
        + f"\nprint({json.dumps(SUCCESS)!r},flush=True)\n"
    )


def test_pinned_original_command_preserved():
    path = ROOT / "tests/fixtures/asus-history/bootstrap-r1.sh"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == t.WRAPPER_SHA
    argv = t.sudo_command(raw)
    assert argv[:6] == ["/usr/bin/sudo", "-S", "-k", "-p", t.PROMPT_FORMAT, "--"]
    assert argv[6:11] == ["/usr/bin/python3.14", "-I", "-B", "-S", "-c"]
    with pytest.raises(t.Denied):
        t.sudo_command(raw + b"\n")


def test_one_submission_eof_no_argv_env_echo_or_secret_in_result():
    supplied = []

    def supply():
        value = bytearray(FAKE)
        supplied.append(value)
        return value

    argv = fake_sudo(
        "assert value not in repr(sys.argv).encode()\n"
        "assert value not in repr(dict(os.environ)).encode()\n"
        "assert sys.stdin.buffer.read()==b''\n"
        "sys.stderr.buffer.write(value);sys.stderr.flush()\n"
    )
    result = t.run_sudo(argv, supply, 3)
    assert result["authentication_submissions"] == 1
    assert FAKE.decode() not in json.dumps(result)
    assert supplied == [bytearray(len(FAKE))]


def test_no_prompt_does_not_fetch_secret():
    def forbidden():
        pytest.fail("secret must not be fetched without a prompt")

    result = t.run_sudo(fake_sudo(prompt=False), forbidden, 3)
    assert result["authentication_submissions"] == 0


def test_split_prompt_is_recognized():
    argv = python(
        "import sys,time\n"
        f"sys.stderr.write({t.PROMPT[:4].decode()!r});sys.stderr.flush();time.sleep(.03)\n"
        f"sys.stderr.write({t.PROMPT[4:].decode()!r});sys.stderr.flush()\n"
        "assert sys.stdin.buffer.readline()\n"
        f"print({json.dumps(SUCCESS)!r},flush=True)"
    )
    assert t.run_sudo(argv, lambda: bytearray(FAKE), 3)["authentication_submissions"] == 1


@pytest.mark.parametrize(
    "scenario", ["second_prompt", "wrong_prompt", "timeout", "stdout_echo", "overflow"]
)
def test_failures_are_bounded_and_never_reflect_secret(scenario):
    calls = []

    def supply():
        calls.append(1)
        return bytearray(FAKE)

    if scenario == "wrong_prompt":
        argv = python(
            "import sys;sys.stderr.write('sudo: a terminal is required');sys.stderr.flush()"
        )
    elif scenario == "timeout":
        argv = python("import time;time.sleep(10)")
    elif scenario == "second_prompt":
        argv = fake_sudo(
            f"sys.stderr.write({t.PROMPT.decode()!r});sys.stderr.flush();time.sleep(10)\n"
        )
    elif scenario == "stdout_echo":
        argv = fake_sudo("sys.stdout.buffer.write(value);sys.stdout.flush()\n")
    else:
        argv = fake_sudo("sys.stderr.write('x'*40000);sys.stderr.flush()\n")
    with pytest.raises((t.Denied, ValueError)) as error:
        t.run_sudo(argv, supply, 0.3)
    assert FAKE.decode() not in str(error.value)
    assert len(calls) <= 1


@pytest.mark.parametrize("identity", ["host", "principal"])
def test_target_mismatch_before_any_file_or_sudo(monkeypatch, identity):
    monkeypatch.setattr(t.os, "getuid", lambda: 1000)
    monkeypatch.setattr(t.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        t.pwd,
        "getpwuid",
        lambda _: types.SimpleNamespace(pw_name="wrong" if identity == "principal" else "morris"),
    )
    monkeypatch.setattr(
        t.os,
        "uname",
        lambda: types.SimpleNamespace(nodename="wrong" if identity == "host" else t.HOST),
    )
    with pytest.raises(t.Denied):
        t.target_command()


def test_file_hash_and_symlink_gates(tmp_path):
    path = tmp_path / "wrapper"
    path.write_bytes(b"wrong sealed bytes")
    path.chmod(0o600)
    with pytest.raises(t.Denied):
        t.read_pinned(path, t.WRAPPER_SHA, 32768)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        t.read_pinned(link, t.WRAPPER_SHA, 32768)


def test_exchange_ready_gates_fetch_and_sanitizes_output():
    calls = []
    answer = {
        "kind": "result",
        "state": "received",
        "authentication_submissions": 1,
        "bootstrap": SUCCESS,
    }
    argv = python(
        "import sys,json\n"
        f"print({json.dumps(t.READY)!r},flush=True)\n"
        "assert sys.stdin.buffer.readline();assert sys.stdin.buffer.read()==b''\n"
        f"print({json.dumps(answer)!r},flush=True)\n"
    )

    def supply():
        calls.append(1)
        return bytearray(FAKE)

    result = t.exchange(argv, supply, 3)
    assert calls == [1] and result["bootstrap"]["status"] == "passed"
    assert FAKE.decode() not in json.dumps(result)
    wrong = {**t.READY, "host": "wrong"}
    with pytest.raises(t.Denied):
        t.exchange(python(f"print({json.dumps(wrong)!r},flush=True)"), supply, 3)
    assert calls == [1]


def test_exchange_no_prompt_never_reads_secret():
    answer = {
        "kind": "result",
        "state": "received",
        "authentication_submissions": 0,
        "bootstrap": SUCCESS,
    }
    result = t.exchange(
        python(f"print({json.dumps(answer)!r},flush=True)"),
        lambda: pytest.fail("secret fetched without prompt"),
        3,
    )
    assert result["authentication_submissions"] == 0


@pytest.mark.parametrize("secret", [b"", b"x\ny", b"x\ry", b"x\0y", b"x" * 1025])
def test_reject_non_single_line_secret(secret):
    with pytest.raises(t.Denied):
        t.secret_valid(bytearray(secret))


def test_claim_is_exclusive_and_no_replay(tmp_path):
    path = tmp_path / "claim.json"
    t.exclusive_json(path, {"dispatch_intent": True})
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        t.exclusive_json(path, {"dispatch_intent": True})


def test_remote_source_is_complete_python_and_uses_no_tty():
    import shlex

    argv = t.remote_argv()
    assert argv[:2] == ["/usr/bin/ssh", "-T"]
    remote = shlex.split(argv[-1])
    assert "MemorySwapMax=0" in remote and "LimitCORE=0" in remote
    compile(remote[-1], "remote_adapter", "exec")
    assert remote[-1].endswith("sys.exit(remote_main())")


def test_management_source_drift_refused_before_dispatch():
    with pytest.raises(t.Denied):
        t.remote_argv("0" * 64)
    current = hashlib.sha256(Path(t.__file__).read_bytes()).hexdigest()
    assert t.remote_argv(current)[0] == "/usr/bin/ssh"


def test_wrong_sudo_password_principal_does_not_fetch():
    argv = python("import sys;sys.stderr.write('AQB_SUDO_R1:root:');sys.stderr.flush()")
    with pytest.raises(t.Denied):
        t.run_sudo(argv, lambda: pytest.fail("must not fetch morris password for root"), 1)


def test_ssh_environment_preserves_only_agent_reference(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/fixture-agent.sock")
    monkeypatch.setenv("UNRELATED_CREDENTIAL", "fixture-env-must-not-propagate")
    env = t.ssh_environment()
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/fixture-agent.sock"
    assert "UNRELATED_CREDENTIAL" not in env
