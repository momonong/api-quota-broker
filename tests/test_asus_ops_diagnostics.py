"""Public fixtures exercise the diagnostic wire protocol; no sudo/API/service calls."""

import importlib.util
import json
import socket
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


e = module("ops_entry")
i = module("install_ops")
p = module("project_ops_receipts")
REQ = {"operation": "inspect", "request_id": "a" * 32}
PRIVATE = "PUBLIC_PRIVATE_EXCEPTION_FIXTURE_DO_NOT_PROJECT"


@pytest.mark.parametrize(
    "change",
    [
        {"stage": PRIVATE},
        {"code": PRIVATE},
        {"rc": True},
        {"rc": 9999},
        {"stderr": PRIVATE},
        {"request_id": "b" * 32},
        {"service": PRIVATE},
        {"automatic_retry": True},
        {"cleanup_unverified": PRIVATE},
    ],
)
def test_protocol_rejects_unknown_fields_codes_and_request_mismatch(change):
    value = e.diagnostic("helper_guard", e.Denied("core_limit"), req=REQ, rc=1)
    value.update(change)
    with pytest.raises((e.Denied, TypeError)):
        e.validate_diagnostic(value, req=REQ)
    projected = p.safe_fields({"status": "blocked", "native_diagnostic": value})
    # The projector has no caller nonce; any syntactically valid generated nonce is public.
    if change == {"request_id": "b" * 32}:
        assert projected["native_diagnostic"]["request_id"] == "b" * 32
    else:
        assert projected.get("native_diagnostic_unverified")
        assert PRIVATE not in json.dumps(projected)


def test_unknown_exception_and_untrusted_denied_text_are_never_projected():
    for error in (RuntimeError(PRIVATE), e.Denied(PRIVATE), e.Denied([PRIVATE])):
        value = e.diagnostic("helper_guard", error, req=REQ)
        assert value["code"] == "unverified_exception" and PRIVATE not in json.dumps(value)


@pytest.mark.parametrize("point", ["before_ready", "after_ready", "raw_exception"])
def test_real_helper_pipe_to_worker_client_bootstrap_and_root_projection(
    monkeypatch, tmp_path, capsys, point
):
    # Run the actual helper function in a public fixture subprocess. PAM/systemd are fixtures.
    program = f"""
import importlib.util,os,sys
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location("entry",{str(ROOT / "deploy/asus/ops_entry.py")!r})
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
def line():
 raw=bytearray()
 while True:
  b=os.read(0,1)
  if b in (b'',b'\\n'):return raw
  raw.extend(b)
password=line()
assert password not in open('/proc/self/cmdline','rb').read()
assert password not in open('/proc/self/environ','rb').read()
os.geteuid=lambda:0
os.uname=lambda:SimpleNamespace(nodename='asus-ubuntu2604-server')
e.package=lambda:None
e.runtime_policy=lambda:{{'ops_uid':994,'config_sha256':'0'*64,'enabled':False}}
os.environ['SUDO_UID']='994'
e.broker_pins=lambda sha:None
def guard(**kw):
 if {point!r}=='before_ready':raise e.Denied('core_limit')
 if {point!r}=='raw_exception':raise RuntimeError({PRIVATE!r})
e.memory_guard=guard
def operation(*a,**kw):raise e.Denied('native_exit',rc=7)
e.helper_operation=operation
sys.argv=['fixture','helper']
raise SystemExit(e.helper())
"""
    spawn = subprocess.Popen
    monkeypatch.setattr(
        e.subprocess,
        "Popen",
        lambda argv, **kw: spawn([sys.executable, "-I", "-B", "-S", "-c", program], **kw),
    )
    policy = SimpleNamespace(
        password_from_doppler=lambda token, transport: bytearray(b"PUBLIC_PASSWORD")
    )
    result = e.manage(
        REQ,
        lambda: bytearray(b"PUBLIC_TOKEN"),
        lambda *a: None,
        policy,
        guard=lambda: None,
        probe=lambda: None,
    )
    expected_stage = "helper_operation" if point == "after_ready" else "helper_guard"
    expected_code = {
        "before_ready": "core_limit",
        "after_ready": "native_exit",
        "raw_exception": "unverified_exception",
    }[point]
    assert (result["stage"], result["code"], result["rc"]) == (
        expected_stage,
        expected_code,
        7 if point == "after_ready" else 1,
    )
    assert result["request_id"] == REQ["request_id"]
    assert not result["operation_may_have_completed"] and PRIVATE not in json.dumps(result)

    class Connection:
        family = socket.AF_UNIX
        type = socket.SOCK_STREAM

        def getsockopt(self, *a):
            return struct.pack("3i", 3, 1000, 1000)

        def settimeout(self, *a):
            pass

        def recv(self, n):
            raw, self.raw = self.raw, b""
            return raw

        def sendall(self, raw):
            self.result = json.loads(raw)

    connection = Connection()
    connection.raw = json.dumps(REQ).encode()
    e.worker_connection(connection, prepare=lambda: None, run=lambda req: result)
    assert connection.result == result

    # Actual Unix socket exchange through production client with metadata fixtures.
    address = str(tmp_path / "socket")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(address)
    listener.listen(1)
    captured = []

    def server():
        conn, _ = listener.accept()
        with conn:
            raw = bytearray()
            while chunk := conn.recv(4096):
                raw.extend(chunk)
            req = json.loads(raw)
            response = dict(result, operation=req["operation"], request_id=req["request_id"])
            captured.append(response)
            conn.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=server)
    thread.start()
    monkeypatch.setattr(e, "SOCKET", address)
    monkeypatch.setattr(e, "root_dir", lambda *a, **kw: None)
    monkeypatch.setattr(e.os, "getuid", lambda: 1000)
    original_lstat = Path.lstat

    def metadata(path, *args, **kwargs):
        if str(path) == address:
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=1000)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", metadata)
    assert e.client("inspect") == 1
    thread.join(timeout=2)
    listener.close()
    client_result = json.loads(capsys.readouterr().out)
    assert client_result == captured[0]

    # Nonzero client status must be captured/persisted before cleanup.
    boot = i.NativeBootstrap({}, e)
    receipts = []
    boot.run = lambda *a, **kw: b""
    boot.receipt = lambda value: receipts.append(json.loads(json.dumps(value)))
    monkeypatch.setattr(
        e, "native_result", lambda *a, **kw: (1, json.dumps(client_result).encode())
    )
    with pytest.raises(i.Blocked, match="native_diagnostic_blocked"):
        boot.inspect_once()
    assert boot.native_diagnostic == client_result
    assert receipts[0]["native_diagnostic"] == client_result
    projected = p.safe_fields(receipts[0])
    assert projected["native_diagnostic"] == client_result
    assert PRIVATE not in json.dumps(receipts)


@pytest.mark.parametrize(
    "rc,raw",
    [
        (0, b"PRIVATE_NOT_JSON"),
        (1, b""),
        (2, b'{"status":"blocked"}'),
        (1, b'{"status":"blocked","stderr":"PRIVATE"}'),
    ],
)
def test_bootstrap_malformed_nonzero_output_does_not_disclose_raw(monkeypatch, rc, raw):
    b = i.NativeBootstrap({}, e)
    b.run = lambda *a, **kw: b""
    receipts = []
    b.receipt = receipts.append
    monkeypatch.setattr(e, "native_result", lambda *a, **kw: (rc, raw))
    with pytest.raises((ValueError, e.Denied, i.Blocked)):
        b.inspect_once()
    assert b.native_diagnostic["stage"] == "native_client"
    assert "PRIVATE" not in json.dumps(receipts)


def test_primary_helper_rejection_survives_cleanup_failure():
    primary = e.diagnostic("helper_pins", e.Denied("pin_changed"), req=REQ, rc=1)

    class Session:
        def execute(self, *a):
            e.deny_diagnostic(primary)

        def close(self):
            raise RuntimeError(PRIVATE)

    result = e.manage(
        REQ,
        lambda: bytearray(b"PUBLIC_TOKEN"),
        lambda *a: None,
        SimpleNamespace(password_from_doppler=lambda *a: bytearray(b"PUBLIC_PASSWORD")),
        guard=lambda: None,
        probe=lambda: None,
        session_factory=Session,
    )
    assert result["stage"] == "helper_pins" and result["code"] == "pin_changed"
    assert result["cleanup_unverified"] and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize(
    "status,code",
    [
        (403, "doppler_auth_denied"),
        (429, "doppler_rate_limited"),
        (500, "doppler_response_unverified"),
    ],
)
def test_fixed_doppler_http_classification_before_sudo(status, code):
    policy = module("broker_ops_policy")
    calls = []
    result = e.manage(
        REQ,
        lambda: bytearray(b"dp.st.dev." + b"0" * 40),
        lambda *a: (status, PRIVATE.encode()),
        policy,
        guard=lambda: None,
        probe=lambda: None,
        session_factory=lambda: calls.append("sudo"),
    )
    assert (result["stage"], result["code"]) == ("worker_doppler", code)
    assert not calls and PRIVATE not in json.dumps(result)


def test_projector_static_allowlists_match_wire_schema():
    assert p.NATIVE_FIELDS == e.DIAGNOSTIC_FIELDS
    assert p.NATIVE_CODES == e.DIAGNOSTIC_CODES and p.NATIVE_STAGES == e.DIAGNOSTIC_STAGES


def test_rejected_request_diagnostics_never_relay_unvalidated_request_values():
    value = e.diagnostic(
        "worker_request",
        e.Denied("request_denied"),
        req={"operation": PRIVATE, "request_id": PRIVATE},
    )
    assert value["request_id"] is None and value["operation"] is None
    assert PRIVATE not in json.dumps(value)
