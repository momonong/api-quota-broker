import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "identity_probe", Path(__file__).resolve().parents[1] / "deploy/asus/runtime_identity_probe.py"
)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        p.os, "uname", lambda: types.SimpleNamespace(nodename="asus-ubuntu2604-server")
    )
    monkeypatch.setattr(p.os, "statvfs", lambda _: types.SimpleNamespace(f_flag=p.os.ST_RDONLY))
    monkeypatch.setattr(p.resource, "setrlimit", lambda *a: None)
    monkeypatch.setattr(
        p.ctypes, "CDLL", lambda _: types.SimpleNamespace(prctl=lambda n, *a: int(n == 39))
    )

    def read(path):
        if path.name == "cgroup":
            return "0::/system.slice/" + p.UNIT
        if path.name == "memory.max":
            return "402653184"
        if path.name == "memory.swap.max":
            return "0"
        assert str(path) == "/proc/self/status"
        return "CapEff:\t000000c0"

    monkeypatch.setattr(p.Path, "read_text", read)


@pytest.mark.parametrize("outcome", ["passed", "failed", "eperm", "timeout"])
def test_native_child_identity_is_enforced_before_import(prepared, monkeypatch, outcome):
    expected = {
        "uid": 995,
        "gid": 982,
        "groups": 0,
        "effective_caps": 0,
        "permitted_caps": 0,
        "ambient_caps": 0,
        "nnp": 1,
        "version": "1.0.0",
    }

    def run(argv, **kw):
        assert kw["user"] == 995 and kw["group"] == 982 and kw["extra_groups"] == []
        assert kw["env"] == p.ENV and kw["stderr"] == subprocess.DEVNULL
        assert argv[-1].index("assert os.getresuid()") < argv[-1].index("import quota_broker")
        assert argv[-1].index('"CapEff","CapPrm","CapAmb"') < argv[-1].index("import quota_broker")
        if outcome == "eperm":
            raise PermissionError(1, "secret")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("secret", 45)
        return types.SimpleNamespace(
            returncode=int(outcome == "failed"), stdout=json.dumps(expected).encode()
        )

    monkeypatch.setattr(p.subprocess, "run", run)
    result = p.probe()
    assert result["root_setuid"] and result["root_setgid"]
    assert result["child"] == (expected if outcome == "passed" else None)
    assert result["errno"] == (1 if outcome == "eperm" else None)
    assert result["timeout"] == (outcome == "timeout")
    assert "secret" not in str(result)
