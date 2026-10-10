"""Discriminate the sealed r1 matcher from the official sudo-rs 0.2.13 prompt."""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = load("bootstrap_transport")
new = load("bootstrap_transport_v2")
SUCCESS = {
    "status": "passed",
    "stage": "complete",
    "automatic_retry": False,
    "maintenance_installed": True,
    "normal_v1_deployed": False,
}


def child(prompt, after=""):
    code = (
        "import sys,time\n"
        f"sys.stderr.write({prompt!r});sys.stderr.flush()\n"
        "line=sys.stdin.buffer.readline()\n" + after + "\n"
        f"print({json.dumps(SUCCESS)!r},flush=True)\n"
    )
    return [sys.executable, "-I", "-B", "-S", "-c", code]


def test_sealed_r1_rejects_real_sudo_rs_format_before_fetch():
    calls = []
    with pytest.raises(old.Denied):
        old.run_sudo(
            child("[sudo: AQB_SUDO_R1:morris:] Password: "),
            lambda: calls.append(1) or bytearray(b"fixture"),
            1,
        )
    assert calls == []


@pytest.mark.parametrize(
    "prompt", ["AQB_SUDO_R1:morris:", "[sudo: AQB_SUDO_R1:morris:] Password: "]
)
def test_candidate_accepts_only_complete_documented_prompt_once(prompt):
    calls = []
    result = new.run_sudo(
        child(prompt, 'assert line==b"fixture\\n";assert sys.stdin.buffer.read()==b""'),
        lambda: calls.append(1) or bytearray(b"fixture"),
        2,
    )
    assert calls == [1] and result["authentication_submissions"] == 1


@pytest.mark.parametrize(
    "prompt",
    [
        "[sudo: AQB_SUDO_R1:root:] Password: ",
        "[sudo: AQB_SUDO_R1:broker-deploy:] Password: ",
        "[sudo: AQB_SUDO_R1:morris:] Input: ",
    ],
)
def test_candidate_rejects_wrong_principal_or_pam_prompt_without_fetch(prompt):
    with pytest.raises(new.Denied):
        new.run_sudo(child(prompt), lambda: pytest.fail("forbidden fetch"), 1)


def test_candidate_second_rs_prompt_aborts_immediately():
    calls = []
    prompt = "[sudo: AQB_SUDO_R1:morris:] Password: "
    started = time.monotonic()
    with pytest.raises(new.Denied):
        new.run_sudo(
            child(prompt, f"sys.stderr.write({prompt!r});sys.stderr.flush();time.sleep(5)"),
            lambda: calls.append(1) or bytearray(b"fixture"),
            4,
        )
    assert calls == [1] and time.monotonic() - started < 2


def test_candidate_retains_original_once_barrier():
    source = Path(new.__file__).read_text()
    assert "bootstrap-transport-r1.claim.json" in source
    assert "bootstrap-transport-r1.result.json" in source
