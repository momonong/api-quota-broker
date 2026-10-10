import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "classified_transport", ROOT / "deploy/asus/bootstrap_transport_v2.py"
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def child(code):
    return [sys.executable, "-I", "-B", "-S", "-c", code]


def ready_then(tail):
    return child("import sys,time,os\n" + f"print({json.dumps(t.READY)!r},flush=True)\n" + tail)


def test_ssh_failure_is_not_a_sudo_or_secret_failure():
    result = t.exchange(child("raise SystemExit(255)"), lambda: pytest.fail("no fetch"), 1)
    assert result == {
        "state": "blocked",
        "diagnostic": {
            "stage": "ssh_connection",
            "code": "child_exit",
            "rc": 255,
            "timeout": False,
            "possible_dispatch": False,
        },
    }


def test_host_gate_safe_frame_never_fetches():
    diag = t.TransportFailure("host_gate", "gate_rejected", possible_dispatch=False).diagnostic
    frame = {"kind": "result", "state": "blocked", "diagnostic": diag}
    result = t.exchange(
        child(f"print({json.dumps(frame)!r},flush=True)"), lambda: pytest.fail("no fetch"), 1
    )
    assert result["diagnostic"] == diag


def test_doppler_failure_after_prompt_proves_no_password_written():
    def fetch():
        raise t.TransportFailure("doppler_fetch", "cli_exit", rc=1, possible_dispatch=False)

    result = t.exchange(ready_then('assert sys.stdin.buffer.read()==b""'), fetch, 1)
    assert result["state"] == "blocked"
    assert result["diagnostic"]["stage"] == "doppler_fetch"
    assert result["diagnostic"]["rc"] == 1
    assert result["diagnostic"]["possible_dispatch"] is False


def test_broken_pipe_after_submission_intent_remains_unknown():
    def fetch():
        time.sleep(0.05)
        return bytearray(b"fixture")

    result = t.exchange(ready_then("os.close(0);time.sleep(2)"), fetch, 1)
    assert result["diagnostic"]["stage"] == "input_write"
    assert result["diagnostic"]["code"] == "io_error"
    assert result["diagnostic"]["possible_dispatch"] is True


def test_bad_result_frame_cannot_reflect_raw_data():
    result = t.exchange(
        ready_then('sys.stdin.buffer.readline();print("fixture-secret",flush=True)'),
        lambda: bytearray(b"fixture"),
        1,
    )
    assert result["diagnostic"]["stage"] == "result_framing"
    assert result["diagnostic"]["code"] == "invalid_frame"
    assert "fixture-secret" not in json.dumps(result)


def test_prompt_and_sudo_auth_stages_are_distinct():
    def run(prompt, tail=""):
        code = "import sys,time\n" + f"sys.stderr.write({prompt!r});sys.stderr.flush()\n" + tail
        try:
            t.run_sudo(child(code), lambda: bytearray(b"fixture"), 0.2)
        except t.TransportFailure as e:
            return e.diagnostic
        pytest.fail("expected failure")

    assert run("wrong")["stage"] == "prompt_gate"
    repeated = run(
        t.PROMPT.decode(),
        "sys.stdin.buffer.readline();sys.stderr.write("
        + repr(t.PROMPT.decode())
        + ");sys.stderr.flush();time.sleep(2)",
    )
    assert repeated["stage"] == "sudo_auth" and repeated["code"] == "second_prompt"
    assert repeated["possible_dispatch"] is False
    timeout = run(t.PROMPT.decode(), "sys.stdin.buffer.readline();time.sleep(2)")
    assert timeout["timeout"] is True and timeout["possible_dispatch"] is True


def test_error_strings_are_not_accepted_as_diagnostic_fields():
    for key in ["stage", "code"]:
        obj = t.TransportFailure("host_gate", "gate_rejected").diagnostic
        obj[key] = "fixture-secret"
        with pytest.raises(t.Denied):
            t.safe_diagnostic(obj)
