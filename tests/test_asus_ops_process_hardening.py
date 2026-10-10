"""Real process-only Linux protection plus failure boundaries; no real secrets/services."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "deploy/asus/ops_entry.py"
spec = importlib.util.spec_from_file_location("dump_protected_entry", SOURCE)
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)
PRIVATE = "PUBLIC_PRIVATE_SYSCALL_FIXTURE_DO_NOT_PROJECT"


@pytest.mark.parametrize("soft", [0, 4096, -1])
def test_real_kernel_core_limits_and_dumpability_are_locked_before_public_input(soft):
    program = f"""
import importlib.util,resource,ctypes,json
spec=importlib.util.spec_from_file_location('e',{str(SOURCE)!r})
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
_,hard=resource.getrlimit(resource.RLIMIT_CORE)
if hard != -1 and ({soft} == -1 or {soft} > hard):
 print(json.dumps({{'unsupported_inherited_hard':True}}));raise SystemExit(0)
resource.setrlimit(resource.RLIMIT_CORE,({soft},hard))
before=resource.getrlimit(resource.RLIMIT_CORE)
e.prevent_process_dumps()
after=resource.getrlimit(resource.RLIMIT_CORE)
dump=ctypes.CDLL(None).prctl(3,0,0,0,0)
public_input=bytearray(b'PUBLIC_INPUT_READ_ONLY_AFTER_HARDENING')
try:resource.setrlimit(resource.RLIMIT_CORE,(1,1));raise_denied=False
except (ValueError,OSError):raise_denied=True
print(json.dumps({{'before':list(before),'after':list(after),'dumpable':dump,'raise_denied':raise_denied,'euid':__import__('os').geteuid()}}))
"""
    p = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program],
        capture_output=True,
        check=True,
        timeout=8,
    )
    r = json.loads(p.stdout)
    if r.get("unsupported_inherited_hard"):
        pytest.skip("inherited hard already zero; cannot synthesize a higher kernel limit")
    assert r["after"] == [0, 0] and r["dumpable"] == 0
    if r["euid"] != 0:
        assert r["raise_denied"]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("set", "core_limit_set_failed"),
        ("query", "core_limit_query_failed"),
        ("readback", "core_limit"),
        ("dump_set", "dumpability"),
        ("dump_query", "dumpability"),
    ],
)
def test_failed_hardening_is_closed_before_any_package_or_input(monkeypatch, failure, code):
    events = []

    def setter(*a):
        events.append("set")
        if failure == "set":
            raise PermissionError(PRIVATE)

    def query(*a):
        events.append("query")
        if failure == "query":
            raise OSError(PRIVATE)
        return (0, -1) if failure == "readback" else (0, 0)

    def prctl(op, *a):
        events.append("prctl")
        if failure == "dump_set" and op == 4:
            return -1
        if failure == "dump_query" and op == 3:
            return 1
        return 0

    monkeypatch.setattr(e.resource, "setrlimit", setter)
    monkeypatch.setattr(e.resource, "getrlimit", query)
    monkeypatch.setattr(e.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(prctl=prctl))
    with pytest.raises(e.Denied, match=code):
        e.prevent_process_dumps()
    if failure == "set":
        assert events == ["set"]
    if failure in ("query", "readback"):
        assert "prctl" not in events
    diagnostic = e.diagnostic("helper_guard", e.Denied(code))
    assert PRIVATE not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "failure", ["unit_scope", "swap_limit", "memory_limit", "sudo_privilege_transition_unavailable"]
)
def test_other_memory_guards_remain_enforced_after_core_repair(monkeypatch, failure):
    events = []
    monkeypatch.setattr(e, "prevent_process_dumps", lambda: events.append("core_and_dump_locked"))

    def text(path):
        if str(path) == "/proc/self/cgroup":
            return (
                "0::/wrong.service"
                if failure == "unit_scope"
                else "0::/system.slice/api-quota-broker-ops@PUBLIC.service"
            )
        if path.name == "memory.swap.max":
            return "1" if failure == "swap_limit" else "0"
        if path.name == "memory.max":
            return "1" if failure == "memory_limit" else "134217728"
        pytest.fail("unexpected file")

    monkeypatch.setattr(Path, "read_text", text)
    monkeypatch.setattr(
        e.ctypes,
        "CDLL",
        lambda *a, **kw: SimpleNamespace(
            prctl=lambda *a: 1 if failure == "sudo_privilege_transition_unavailable" else 0
        ),
    )
    with pytest.raises(e.Denied, match=failure):
        e.memory_guard()
    assert events == ["core_and_dump_locked"]


def test_helper_hardens_before_package_policy_and_stdin(monkeypatch):
    events = []
    monkeypatch.setattr(e.sys, "argv", ["PUBLIC", "helper"])
    monkeypatch.setattr(e.os, "geteuid", lambda: 0)
    monkeypatch.setattr(e.os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server"))

    def failed():
        events.append("hardening")
        raise e.Denied("core_limit_set_failed")

    monkeypatch.setattr(e, "prevent_process_dumps", failed)
    monkeypatch.setattr(e, "package", lambda: pytest.fail("package before protection"))
    monkeypatch.setattr(e, "pipe_line", lambda *a, **kw: pytest.fail("stdin before protection"))
    outputs = []
    monkeypatch.setattr(e.os, "write", lambda fd, raw: outputs.append(json.loads(raw)))
    assert e.helper() == 1 and events == ["hardening"]
    assert outputs[0]["stage"] == "helper_guard" and outputs[0]["code"] == "core_limit_set_failed"
