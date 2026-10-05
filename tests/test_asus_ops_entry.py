"""Bounded offline behavior; no native account/PAM/provider calls."""

import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

ROOT = Path(__file__).parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = module("ops_entry")
installer = module("install_ops")
policy = module("broker_ops_policy")
REQ = {"operation": "inspect", "request_id": "a" * 32}
FAKE_TOKEN = b"dp.st.dev." + b"0" * 40
FAKE_PASSWORD = "PUBLIC_FIXTURE_NOT_A_REAL_PASSWORD_00000000"
STATUS = {
    "ActiveState": "active",
    "SubState": "running",
    "MainPID": "12",
    "NRestarts": "0",
    "ExecMainStartTimestampMonotonic": "123",
}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b"[]",
        b"null",
        b"X" * 193,
        b'{"operation":"inspect","operation":"restart"}',
        json.dumps(REQ | {"path": "/tmp/source"}).encode(),
        json.dumps(REQ | {"operation": "deploy"}).encode(),
        json.dumps(REQ | {"request_id": "../claim"}).encode(),
        json.dumps(REQ | {"request_id": 1}).encode(),
        json.dumps(REQ).encode() + json.dumps(REQ).encode(),
    ],
)
def test_malformed_request_is_denied(raw):
    with pytest.raises(entry.Denied):
        entry.request(raw)


def test_inspect_never_opens_or_writes_state(tmp_path):
    missing = tmp_path / "state-does-not-exist"
    called = []
    out = entry.helper_operation(
        REQ,
        state=missing,
        pins=lambda: called.append("pins"),
        inspect=lambda: STATUS,
        restart=lambda: pytest.fail("restart"),
    )
    assert out["state"] == STATUS and out["status"] == "passed"
    assert called == ["pins"] and not missing.exists()
    assert list(tmp_path.iterdir()) == []


def synthetic_root_storage(monkeypatch, tmp_path):
    # Actual isolated user-owned files/flock/EXCL/fsync. Root metadata is injected;
    # this is not native ownership, account, cgroup or PAM acceptance evidence.
    tmp_path.chmod(0o700)
    (tmp_path / "operation.lock").touch(mode=0o600)
    real_stat, real_write = os.fstat, entry.write_exclusive

    def simulated_stat(fd):
        s = real_stat(fd)
        return SimpleNamespace(st_uid=0, st_mode=s.st_mode, st_nlink=s.st_nlink)

    monkeypatch.setattr(entry.os, "fstat", simulated_stat)
    monkeypatch.setattr(entry, "root_dir", lambda *a, **kw: None)
    monkeypatch.setattr(
        entry,
        "write_exclusive",
        lambda p, raw: real_write(p, raw, uid=os.getuid(), gid=os.getgid()),
    )


def test_restart_intent_survives_failure_and_same_id_cannot_replay(monkeypatch, tmp_path):
    synthetic_root_storage(monkeypatch, tmp_path)
    req = REQ | {"operation": "restart"}
    calls = []

    def restart():
        assert (tmp_path / (req["request_id"] + ".claim.json")).exists()
        calls.append("restart")
        raise OSError("PUBLIC_PRIVATE_ERROR_FIXTURE")

    with pytest.raises(OSError):
        entry.helper_operation(
            req, state=tmp_path, pins=lambda: None, inspect=lambda: STATUS, restart=restart
        )
    with pytest.raises(FileExistsError):
        entry.helper_operation(
            req, state=tmp_path, pins=lambda: None, inspect=lambda: STATUS, restart=restart
        )
    assert calls == ["restart"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "a" * 32 + ".claim.json",
        "operation.lock",
    ]
    assert FAKE_PASSWORD.encode() not in (tmp_path / ("a" * 32 + ".claim.json")).read_bytes()


def test_restart_records_one_result_and_serializes(monkeypatch, tmp_path):
    synthetic_root_storage(monkeypatch, tmp_path)
    req = REQ | {"operation": "restart"}
    calls = []
    states = iter([STATUS, STATUS | {"MainPID": "13", "ExecMainStartTimestampMonotonic": "124"}])
    out = entry.helper_operation(
        req,
        state=tmp_path,
        pins=lambda: None,
        inspect=lambda: next(states),
        restart=lambda: calls.append("restart"),
    )
    assert out["status"] == "passed" and calls == ["restart"]
    assert json.loads((tmp_path / ("a" * 32 + ".result.json")).read_bytes()) == out


def test_successful_restart_command_without_new_start_is_unknown(monkeypatch, tmp_path):
    synthetic_root_storage(monkeypatch, tmp_path)
    with pytest.raises(entry.Denied):
        entry.helper_operation(
            REQ | {"operation": "restart"},
            state=tmp_path,
            pins=lambda: None,
            inspect=lambda: STATUS,
            restart=lambda: None,
        )
    assert (tmp_path / ("a" * 32 + ".claim.json")).exists()
    assert not (tmp_path / ("a" * 32 + ".result.json")).exists()


@pytest.mark.parametrize(
    "bad", [None, "wrong_uid", "group_read", "other_read", "write", "extra_entry"]
)
def test_root_credential_acl_requires_only_exact_service_read(bad):
    undefined = 0xFFFFFFFF
    entries = [
        (1, 4, undefined),
        (2, 4, 996),
        (4, 0, undefined),
        (16, 4, undefined),
        (32, 0, undefined),
    ]
    if bad == "wrong_uid":
        entries[1] = (2, 4, 995)
    if bad == "group_read":
        entries[2] = (4, 4, undefined)
    if bad == "other_read":
        entries[4] = (32, 4, undefined)
    if bad == "write":
        entries[1] = (2, 6, 996)
    if bad == "extra_entry":
        entries.insert(2, (2, 4, 1000))
    raw = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *item) for item in entries)
    info = SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100440, st_nlink=1, st_size=60)
    if bad is None:
        entry.credential_metadata(info, 996, raw)
    else:
        with pytest.raises(entry.Denied):
            entry.credential_metadata(info, 996, raw)


def test_service_owned_credential_read_only_and_no_shared_permission():
    info = SimpleNamespace(st_uid=996, st_gid=981, st_mode=0o100400, st_nlink=1, st_size=60)
    entry.credential_metadata(info, 996)
    info.st_mode = 0o100440
    with pytest.raises(entry.Denied):
        entry.credential_metadata(info, 996)


def test_partial_useradd_failure_reidentifies_locks_and_expires(monkeypatch):
    boot = installer.NativeBootstrap({}, entry)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv == installer.CREATE:
            raise entry.Denied("native_failed")
        if argv[:2] == ("/usr/bin/passwd", "--status"):
            return b"broker-deploy L 2026-10-05\n"
        if argv[:2] == ("/usr/bin/chage", "--list"):
            return b"Account expires : 1970-01-02\n"
        return b""

    monkeypatch.setattr(boot, "run", run)
    monkeypatch.setattr(boot, "account_identity", lambda: (996, 981))
    with pytest.raises(entry.Denied):
        boot.create_account()
    assert not boot.created and boot.creation_attempted
    assert boot.rollback() is True and installer.LOCK in calls
    assert calls.count(installer.CREATE) == 1 and boot.uid == 996


def test_partial_create_unverified_identity_is_never_locked(monkeypatch):
    boot = installer.NativeBootstrap({}, entry)
    boot.creation_attempted = True

    def identity():
        raise installer.Blocked("account_identity")

    monkeypatch.setattr(boot, "account_identity", identity)
    monkeypatch.setattr(boot, "run", lambda *a, **kw: pytest.fail("unsafe account mutation"))
    assert boot.rollback() is False


def test_sealed_copy_wrapper_compiles_restores_tty_and_uses_pty(tmp_path):
    builder = module("build_ops_review")
    result = builder.build(tmp_path / "review")
    path = tmp_path / "review/ops-bootstrap-once.sh"
    subprocess.run(["/bin/bash", "-n", str(path)], check=True)
    text = path.read_text()
    code = text.split("<<'PYROOT'\n", 1)[1].rsplit("PYROOT\n", 1)[0]
    compile(code, "<root-review>", "exec")
    assert "os.dup2(tty,number)" in code and '"--pty"' in code
    assert '"--pipe"' not in code and "bash -s" not in text.replace("historical bash -s", "")
    assert result["uploaded"] is False and result["token_created"] is False
    assert set(json.loads((tmp_path / "review/seal.json").read_bytes())["files"]) == set(
        installer.NAMES
    )
    assert (tmp_path / "review").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "review").iterdir())


@pytest.mark.parametrize("finish", ["input", "eof", "interrupt"])
def test_real_controlling_pty_hidden_input_is_not_echoed(finish):
    import select
    import termios
    import time

    fixture = "PUBLIC_TTY_FIXTURE_NO_SECRET"
    code = f"""import fcntl,termios,os,json,importlib.util
fcntl.ioctl(0,termios.TIOCSCTTY,0)
spec=importlib.util.spec_from_file_location("pty_ops",{str(ROOT / "deploy/asus/install_ops.py")!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
before=termios.tcgetattr(0)
try:
    value=m.tty_line(0,"Hidden fixture: ",hidden=True)
    result={{"status":"returned","match":value=={fixture.encode()!r}}}
except BaseException:
    result={{"status":"cancelled"}}
result['restored']=termios.tcgetattr(0)==before
print(json.dumps(result),flush=True)
"""
    master, slave = os.openpty()
    p = None
    raw = b""
    try:
        p = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        end = time.monotonic() + 5
        while b"Hidden fixture: " not in raw:
            assert time.monotonic() < end
            if select.select([master], [], [], 0.1)[0]:
                raw += os.read(master, 8192)
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(
            master,
            {"input": fixture.encode() + b"\n", "eof": b"\x04", "interrupt": b"\x03"}[finish],
        )
        while b'"restored"' not in raw:
            assert time.monotonic() < end
            if select.select([master], [], [], 0.1)[0]:
                raw += os.read(master, 8192)
        assert p.wait(timeout=2) == 0 and fixture.encode() not in raw
        report = json.JSONDecoder().raw_decode(raw.decode()[raw.decode().index("{") :])[0]
        assert report["restored"]
        if finish == "input":
            assert report["match"]
    finally:
        if p is not None and p.poll() is None:
            p.kill()
            p.wait(timeout=2)
        os.close(master)
        os.close(slave)


def transport(_path, _headers):
    return 200, json.dumps(
        {"name": policy.SECRET, "value": {"raw": FAKE_PASSWORD, "computed": FAKE_PASSWORD}}
    ).encode()


class Session:
    def __init__(self):
        self.password = None
        self.closed = False

    def execute(self, password, req):
        self.password = password
        assert password.decode() == FAKE_PASSWORD
        return {"status": "passed", "operation": req["operation"]}

    def close(self):
        self.closed = True


def test_worker_fresh_fetch_each_operation_and_wipes(tmp_path):
    calls = []
    tokens = []
    sessions = []

    def load():
        tokens.append(bytearray(FAKE_TOKEN))
        return tokens[-1]

    def fetched(path, headers):
        calls.append((path, headers.keys()))
        return transport(path, headers)

    def session():
        sessions.append(Session())
        return sessions[-1]

    for _ in range(2):
        out = entry.manage(
            REQ,
            load,
            fetched,
            policy,
            guard=lambda: None,
            probe=lambda: None,
            session_factory=session,
        )
        assert out["status"] == "passed"
    assert len(calls) == 2 and len(tokens) == 2
    assert all(not any(t) for t in tokens)
    assert all(s.closed and not any(s.password) for s in sessions)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("gate", ["guard", "probe", "load", "revoked", "wrong_password", "cleanup"])
def test_worker_failure_redacts_and_never_retries(gate):
    calls = []

    def fail():
        raise OSError(FAKE_PASSWORD)

    class FailedSession(Session):
        def execute(self, pw, req):
            calls.append("sudo")
            if gate == "wrong_password":
                fail()
            return super().execute(pw, req)

        def close(self):
            if gate == "cleanup":
                fail()
            super().close()

    def load():
        calls.append("load")
        if gate == "load":
            fail()
        return bytearray(FAKE_TOKEN)

    def fetched(p, h):
        calls.append("GET")
        return (403, b"PUBLIC_PRIVATE_RESPONSE_FIXTURE") if gate == "revoked" else transport(p, h)

    out = entry.manage(
        REQ,
        load,
        fetched,
        policy,
        guard=fail if gate == "guard" else lambda: None,
        probe=fail if gate == "probe" else lambda: None,
        session_factory=FailedSession,
    )
    assert out["status"] == "blocked" and out["automatic_retry"] is False
    assert FAKE_PASSWORD not in json.dumps(out)
    assert calls.count("GET") <= 1 and calls.count("sudo") <= 1
    if gate in ("guard", "probe"):
        assert calls == []


def test_unix_peer_credentials_and_single_request_no_state(tmp_path):
    if os.getuid() != 1000:
        pytest.skip("fixture peer must be configured UID1000")
    path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    called = []

    def serve():
        conn, _ = listener.accept()
        with conn:
            entry.worker_connection(
                conn,
                prepare=lambda: called.append("prepare"),
                run=lambda req: {"status": "passed", "operation": req["operation"]},
            )

    thread = threading.Thread(target=serve)
    thread.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.connect(str(path))
        c.sendall(json.dumps(REQ).encode())
        c.shutdown(socket.SHUT_WR)
        out = json.loads(c.recv(4096))
    thread.join(timeout=2)
    listener.close()
    assert not thread.is_alive() and out["status"] == "passed" and called == ["prepare"]
    assert len(list(tmp_path.iterdir())) == 1


def test_other_peer_denied_before_prepare_or_secret():
    class Conn:
        family = socket.AF_UNIX
        type = socket.SOCK_STREAM

        def getsockopt(self, *args):
            return struct.pack("3i", 123, 1001, 1001)

        def sendall(self, data):
            self.result = json.loads(data)

        def recv(self, *args):
            pytest.fail("read request before peer check")

    conn = Conn()
    entry.worker_connection(
        conn, prepare=lambda: pytest.fail("prepare"), run=lambda req: pytest.fail("run")
    )
    assert conn.result["status"] == "blocked"


@pytest.mark.parametrize("offset", [timedelta(days=31), timedelta(minutes=10), timedelta(days=-1)])
def test_expiry_rejects_invalid_window(offset):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    with pytest.raises(installer.Blocked):
        installer.expiry((now + offset).isoformat(), now)


def test_installer_policy_matches_worker_schema(monkeypatch):
    boot = installer.NativeBootstrap({}, entry)
    boot.uid = 996
    boot.gid = 981
    boot.config_sha = "0" * 64
    boot.expires = (datetime.now(UTC) + timedelta(days=29)).isoformat()
    boot.issued = datetime.now(UTC).isoformat()
    data = boot.policy_bytes(enabled=False)
    monkeypatch.setattr(entry, "read_root", lambda *a, **kw: data)
    monkeypatch.setattr(
        entry.pwd,
        "getpwnam",
        lambda n: SimpleNamespace(
            pw_uid=996, pw_gid=981, pw_shell="/usr/sbin/nologin", pw_dir="/nonexistent"
        ),
    )
    monkeypatch.setattr(entry.os, "getgrouplist", lambda *a: [981])
    assert entry.runtime_policy()["enabled"] is False
    d = json.loads(data)
    d["expires_at"] = (datetime.now(UTC) + timedelta(days=31)).isoformat()
    monkeypatch.setattr(entry, "read_root", lambda *a, **kw: json.dumps(d).encode())
    with pytest.raises(entry.Denied):
        entry.runtime_policy()


class Transaction:
    stages: ClassVar[list[str]] = [
        "preflight",
        "guard",
        "input_token",
        "fetch_password",
        "create_account",
        "set_password",
        "publish",
        "native_checks",
        "inspect_once",
        "activate",
        "preservation",
    ]

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.token = bytearray(FAKE_TOKEN)
        self.password = bytearray(FAKE_PASSWORD, "ascii")

    def __getattr__(self, name):
        if name not in self.stages:
            raise AttributeError(name)

        def call(*args):
            self.calls.append(name)
            if name == self.fail:
                raise OSError(FAKE_PASSWORD)
            if name == "input_token":
                return self.token, "2026-11-01T00:00:00+00:00"
            if name == "fetch_password":
                return self.password

        return call

    def rollback(self):
        self.calls.append("rollback")
        return True

    def receipt(self, r):
        self.saved = r


@pytest.mark.parametrize("stage", Transaction.stages)
def test_initializer_each_failure_closes_access_redacts_and_preserves(stage):
    t = Transaction(stage)
    out = installer.initialize(t)
    assert out["status"] == "blocked" and out["rollback_verified"] is True
    assert t.calls[-1] == "rollback" and t.calls.count(stage) == 1
    assert FAKE_PASSWORD not in json.dumps(out) and "restart" not in t.calls
    assert out["automatic_retry"] is False
    if Transaction.stages.index(stage) > 2:
        assert not any(t.token)
    if Transaction.stages.index(stage) > 3:
        assert not any(t.password)


def test_initializer_complete_flow_no_restart():
    t = Transaction()
    out = installer.initialize(t)
    assert t.calls == t.stages
    assert out["native_inspect_verified"] and out["native_restart_executed"] is False
    assert out["scope_verification"] == "human_dashboard_attestation_only"
    assert not any(t.token) and not any(t.password)


def test_default_cli_is_zero_effect_and_invalid_arg_redacted(tmp_path):
    for name in ("ops_entry.py", "install_ops.py"):
        command = [sys.executable, "-I", "-B", str(ROOT / "deploy/asus" / name)]
        out = subprocess.run(command, capture_output=True, cwd=tmp_path, check=True)
        assert json.loads(out.stdout)["apply"] is False
        out = subprocess.run(
            command + [FAKE_PASSWORD], capture_output=True, cwd=tmp_path, check=False
        )
        assert out.returncode == 1 and FAKE_PASSWORD.encode() not in out.stdout + out.stderr
    assert list(tmp_path.iterdir()) == []


def test_units_preserve_sudo_and_bound_workers():
    unit = (ROOT / "deploy/asus/api-quota-broker-ops@.service").read_text()
    socket_unit = (ROOT / "deploy/asus/api-quota-broker-ops.socket").read_text()
    assert "Slice=system.slice" in unit and "NoNewPrivileges=no" in unit
    assert "MemorySwapMax=0" in unit and "LimitCORE=0" in unit
    assert "Restart=no" in unit and "StandardError=null" in unit
    assert "LoadCredentialEncrypted=ops_doppler:" in unit
    assert "Accept=yes" in socket_unit and "MaxConnections=1" in socket_unit
    assert "ListenStream=/run/" in socket_unit and "ListenStream=0.0.0.0" not in socket_unit


@pytest.mark.parametrize("bypass", [False, True])
def test_real_pipe_sudo_adapter_handshake_without_secret_argv_env(monkeypatch, bypass):
    # Native pipes + public fake password + nonprivileged program, not PAM.
    program = """import os,json,sys
def line():
    raw=bytearray()
    while True:
        b=os.read(0,1)
        if b in (b'',b'\\n'):return bytes(raw)
        raw.extend(b)
if sys.argv[1]=='bypass':
    os.write(1,b'BROKER_AUTH_READY\\n')
    raw=line()
    try:json.loads(raw)
    except ValueError:os.write(1,b'{"status":"blocked"}\\n');raise SystemExit(1)
else:
    password=line()
    assert password not in open('/proc/self/cmdline','rb').read()
    assert password not in open('/proc/self/environ','rb').read()
    os.write(1,b'BROKER_AUTH_READY\\n')
    req=json.loads(line())
    state={'ActiveState':'active','SubState':'running','MainPID':'12','NRestarts':'0',
           'ExecMainStartTimestampMonotonic':'123'}
    os.write(1,json.dumps({'status':'passed','operation':req['operation'],
        'request_id':req['request_id'],'service':'api-quota-broker.service','state':state}).encode()+b'\\n')
"""
    original = subprocess.Popen

    def spawn(argv, **kwargs):
        assert argv == entry.sudo_argv()
        assert kwargs["env"] == entry.ENV and FAKE_PASSWORD not in repr(kwargs["env"])
        return original(
            [sys.executable, "-I", "-B", "-S", "-c", program, "bypass" if bypass else "password"],
            **kwargs,
        )

    monkeypatch.setattr(entry.subprocess, "Popen", spawn)
    session = entry.SudoSession()
    try:
        if bypass:
            with pytest.raises(entry.Denied):
                session.execute(bytearray(FAKE_PASSWORD, "ascii"), REQ)
        else:
            assert session.execute(bytearray(FAKE_PASSWORD, "ascii"), REQ)["state"] == STATUS
    finally:
        session.close()


@pytest.mark.parametrize("status", [200, 302, 403])
def test_http_adapter_fixed_single_get_no_redirect_proxy_or_tls_keylog(
    monkeypatch, tmp_path, status
):
    called = []
    monkeypatch.setenv("HTTPS_PROXY", "http://INVALID_PUBLIC_PROXY_FIXTURE")
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "must-not-exist"))

    class Response:
        def read(self, n):
            assert n == 8193
            return b"PUBLIC_RESPONSE_FIXTURE"

    class Connection:
        def __init__(self, host, timeout, context):
            assert host == "api.doppler.com" and timeout == 8 and context.check_hostname
            assert context.keylog_filename is None

        def request(self, method, path, headers):
            called.append((method, path))

        def getresponse(self):
            r = Response()
            r.status = status
            return r

        def close(self):
            called.append("closed")

    monkeypatch.setattr(entry.http.client, "HTTPSConnection", Connection)
    out = entry.doppler_transport(policy.SECRET_PATH, {"Authorization": "Bearer PUBLIC_FIXTURE"})
    assert out[0] == status and called == [("GET", policy.SECRET_PATH), "closed"]
    assert not (tmp_path / "must-not-exist").exists()
