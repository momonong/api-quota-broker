import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "stage_probe", ROOT / "deploy/asus/runtime_stage_probe.py"
)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(p, "guard", lambda: None)
    monkeypatch.setattr(
        p.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_uid=995, pw_gid=982)
    )


def test_exact_child_launch_is_unprivileged_and_discards_output(prepared, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(p.subprocess, "run", run)
    result = p.probe()
    assert result["stage"] == "child_exit" and result["code"] == "passed"
    args, kw = seen[0]
    assert args[0] == p.CANDIDATE + "/runtime/bin/python"
    assert kw["user"] == 995 and kw["group"] == 982 and kw["extra_groups"] == []
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    assert kw["timeout"] == 45 and kw["env"] == p.ENV


@pytest.mark.parametrize(
    "error,kind,number,timeout",
    [
        (PermissionError(13, "fixture-sensitive-message"), "PermissionError", 13, False),
        (OSError(11, "fixture-sensitive-message"), "BlockingIOError", 11, False),
        (subprocess.TimeoutExpired("fixture-sensitive-command", 45), "TimeoutExpired", None, True),
        (subprocess.SubprocessError("fixture-sensitive-message"), "SubprocessError", None, False),
        (ValueError("fixture-sensitive-message"), "ValueError", None, False),
    ],
)
def test_failures_are_classified_without_text(prepared, monkeypatch, error, kind, number, timeout):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(p.subprocess, "run", run)
    result = p.probe()
    assert result["stage"] == "child_launch"
    assert result["exception_type"] == kind
    assert result["errno"] == number and result["timeout"] == timeout
    assert "fixture-sensitive" not in str(result)


def test_wrong_application_identity_never_launches(prepared, monkeypatch):
    monkeypatch.setattr(p.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_uid=0, pw_gid=0))
    monkeypatch.setattr(
        p.subprocess, "run", lambda *a, **k: pytest.fail("root execution forbidden")
    )
    result = p.probe()
    assert result["stage"] == "account_lookup" and result["exception_type"] == "AssertionError"


@pytest.mark.parametrize("failure", [None, "groups", "gid", "uid", "combined"])
def test_syscall_reduction_is_fixed_and_keeps_failures_distinct(prepared, monkeypatch, failure):
    monkeypatch.setattr(
        p.Path, "read_text", lambda self: "CapEff:\t000000c0\nNoNewPrivs:\t1\nSeccomp:\t2\n"
    )
    calls = []
    names = ["baseline", "groups", "gid", "uid", "combined"]

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) <= 5:
            assert argv == ["/usr/bin/true"]
            assert kwargs["stdout"] == subprocess.DEVNULL
            if names[len(calls) - 1] == failure:
                raise PermissionError(1, "sensitive fixture")
            return types.SimpleNamespace(returncode=0)
        assert argv[:5] == ["/usr/bin/python3.14", "-I", "-B", "-S", "-c"]
        assert "quota_broker" not in argv[-1] and p.CANDIDATE not in argv[-1]
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"step": "done", "errno": None, "uid": 995, "gid": 982, "groups": 0}
            ).encode(),
        )

    monkeypatch.setattr(p.subprocess, "run", run)
    result = p.syscall_probe()
    assert result["cap_setgid"] and result["cap_setuid"]
    assert result["nnp"] == 1 and result["seccomp"] == 2
    for name, record in result["cases"].items():
        assert record == {
            "rc": None if name == failure else 0,
            "errno": 1 if name == failure else None,
            "timeout": False,
        }
    assert len(calls) == 6
    assert calls[4][1]["user"] == 995 and calls[4][1]["group"] == 982
    assert calls[4][1]["extra_groups"] == []
    assert all(kw["env"] == p.ENV and kw["stderr"] == subprocess.DEVNULL for _, kw in calls)
    assert "sensitive" not in str(result)
