"""Bounded offline behavior; no native account/PAM/provider calls."""

import importlib.util
import json
import os
import socket
import stat
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
    "value",
    [
        b"dp.st." + b"A" * 40,
        b"dp.st.dev." + b"B" * 44,
        b"dp.st.ab." + b"0" * 41,
        b"dp.st." + b"a_b-" * 8 + b"abc." + b"Z" * 40,
    ],
)
def test_official_service_token_optional_config_and_alphanumeric_suffix(value):
    assert entry.valid_service_token(value) and entry.valid_service_token(bytearray(value))


@pytest.mark.parametrize(
    "value",
    [
        b"dp.pt." + b"A" * 40,
        b"dp.ct." + b"A" * 40,
        b"dp.sa." + b"A" * 40,
        b"dp.st." + b"A" * 39,
        b"dp.st." + b"A" * 45,
        b"dp.st.x." + b"A" * 40,
        b"dp.st." + b"a" * 36 + b"." + b"A" * 40,
        b"dp.st.DEV." + b"A" * 40,
        b"dp.st.dev." + b"_" * 40,
        b"dp.st.dev." + b"-" * 40,
        b"dp.st." + b"*" * 40,
        b"dp.st." + b"A" * 40 + b"\n",
        b"dp.st." + b"A" * 40 + b"\0",
    ],
)
def test_official_token_fullmatch_rejects_nonservice_wrong_length_chars_and_multiple_lines(value):
    assert not entry.valid_service_token(value)


@pytest.mark.parametrize(
    "envelope",
    [
        lambda t: t,
        lambda t: b" \t" + t + b" \t\r",
        lambda t: b"\x1b[200~" + t + b"\x1b[201~",
    ],
)
def test_tty_token_framing_normalizes_same_mutable_buffer_only(envelope):
    expected = b"dp.st." + b"A" * 40
    raw = bytearray(envelope(expected))
    assert installer.normalize_tty_token(raw) is raw and raw == expected
    assert entry.valid_service_token(raw)
    entry.wipe(raw)
    assert not any(raw)


@pytest.mark.parametrize(
    "value",
    [
        b"\x1b[200~dp.st." + b"A" * 40,
        b"dp.st." + b"A" * 40 + b"\x1b[201~",
        b"dp.st." + b"A" * 20 + b"\n" + b"A" * 20,
        b"dp.st." + b"A" * 20 + b"\r" + b"A" * 20,
    ],
)
def test_incomplete_paste_or_internal_line_break_is_rejected_not_joined(value):
    raw = bytearray(value)
    with pytest.raises(installer.Blocked, match="tty_envelope_unverified"):
        installer.normalize_tty_token(raw)
    entry.wipe(raw)
    assert not any(raw)


@pytest.mark.parametrize("failure", ["eof", "bound", "multiline", "read_error", "restore_error"])
def test_hidden_tty_internal_raw_buffers_are_wiped_on_failure(monkeypatch, failure):
    import termios

    held = []
    real_bytearray = bytearray

    def capture(*args):
        result = real_bytearray(*args)
        held.append(result)
        return result

    monkeypatch.setattr(installer, "bytearray", capture, raising=False)
    monkeypatch.setattr(installer.os, "isatty", lambda fd: True)
    state = [0, 0, 0, termios.ECHO, 0, 0, [b"\0"] * 32]
    monkeypatch.setattr(installer.termios, "tcgetattr", lambda fd: state)
    changes = []

    def set_attrs(fd, how, attrs):
        changes.append(attrs)
        state[3] = attrs[3]
        if failure == "restore_error" and len(changes) == 2:
            raise OSError("PUBLIC_RESTORE_FAILURE")

    monkeypatch.setattr(installer.termios, "tcsetattr", set_attrs)
    monkeypatch.setattr(installer.os, "write", lambda *a: 0)
    sequence = {
        "eof": [b"A", b""],
        "bound": [b"A"] * 5,
        "multiline": [b"A", b"\n", b"PUBLIC_SECOND_LINE\n"],
        "read_error": [b"A", OSError("PUBLIC_READ_FAILURE")],
        "restore_error": [b"A", b"\n"],
    }[failure]

    def read(fd, n):
        value = sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(installer.os, "read", read)
    monkeypatch.setattr(
        installer.select,
        "select",
        lambda *a: ([123], [], []) if failure == "multiline" else ([], [], []),
    )
    with pytest.raises((installer.Blocked, OSError)):
        installer.tty_line(123, "Hidden fixture: ", hidden=True, limit=3)
    assert held and all(not any(buf) for buf in held)


@pytest.mark.parametrize(
    "raw", [b"dp.st." + b"A" * 40, b"dp.st.dev." + b"B" * 44, b"dp.pt." + b"A" * 40]
)
def test_worker_credential_reader_uses_same_official_syntax_and_wipes_invalid(
    monkeypatch, tmp_path, raw
):
    file = tmp_path / "fixture-credential"
    file.write_bytes(raw)
    file.chmod(0o400)
    original_open = os.open
    original_read_text = Path.read_text

    def open_credential(path, flags):
        assert str(path) == "/run/credentials/api-quota-broker-ops@fixture.service/ops_doppler"
        return original_open(file, flags)

    monkeypatch.setattr(entry.os, "open", open_credential)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "0::/system.slice/api-quota-broker-ops@fixture.service"
            if str(path) == "/proc/self/cgroup"
            else original_read_text(path, *a, **kw)
        ),
    )
    wiped = []
    real_wipe = entry.wipe
    monkeypatch.setattr(entry, "wipe", lambda value: (wiped.append(value), real_wipe(value)))
    if raw.startswith(b"dp.pt."):
        with pytest.raises(entry.Denied, match="credential_format"):
            entry.token_read()
        assert len(wiped) == 1 and not any(wiped[0])
    else:
        value = entry.token_read()
        assert value == raw
        real_wipe(value)
        assert not any(value)


@pytest.mark.parametrize("valid", [True, False])
def test_bootstrap_input_uses_shared_matcher_and_wipes_rejected_input(monkeypatch, valid):
    token = bytearray(b"dp.st." + b"A" * 40 if valid else b"dp.pt." + b"A" * 40)
    expiry = (datetime.now(UTC) + timedelta(days=29)).isoformat().encode()
    values = iter([bytearray(b"READONLY30"), bytearray(expiry), token])
    monkeypatch.setattr(installer.os, "open", lambda *a, **kw: 123)
    monkeypatch.setattr(installer.os, "close", lambda fd: None)
    monkeypatch.setattr(installer, "tty_line", lambda *a, **kw: next(values))
    b = installer.NativeBootstrap({}, entry)
    if valid:
        result, _ = b.input_token()
        assert result is token
        entry.wipe(result)
    else:
        with pytest.raises(installer.Blocked, match="token_format"):
            b.input_token()
    assert not any(token)


def test_service_syntax_does_not_authorize_runtime_project_or_other_config(monkeypatch):
    b, t = budget_transaction()
    monkeypatch.setattr(installer.time, "monotonic", lambda: 100)
    paths = []

    def runtime_token_rejected(path, headers):
        paths.append(path)
        assert path == policy.SECRET_PATH
        assert "project=api-quota-broker-ops&config=dev&name=ASUS_BROKER_DEPLOY_PASSWORD" in path
        return 403, b"PUBLIC_DENIED_FIXTURE"

    b.fetch_password = lambda token: policy.password_from_doppler(token, runtime_token_rejected)
    result = installer.initialize(b)
    assert result["status"] == "blocked" and paths == [policy.SECRET_PATH]
    assert (
        "apply_ssh_deny" not in t.calls
        and "create_account" not in t.calls
        and "publish" not in t.calls
    )
    assert not any(t.token)


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


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_existing_host_key_root_only_metadata_accepts_native_0400(mode):
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | mode, st_uid=0, st_gid=0, st_nlink=1, st_size=4112
    )
    installer.host_key_metadata(info)


@pytest.mark.parametrize(
    "change",
    [
        {"st_mode": stat.S_IFLNK | 0o400},
        {"st_mode": stat.S_IFDIR | 0o400},
        {"st_mode": stat.S_IFREG | 0o440},
        {"st_mode": stat.S_IFREG | 0o640},
        {"st_mode": stat.S_IFREG | 0o644},
        {"st_mode": stat.S_IFREG | 0o4600},
        {"st_uid": 1000},
        {"st_gid": 1000},
        {"st_nlink": 2},
        {"st_size": 0},
        {"st_size": 16385},
    ],
)
def test_host_key_unsafe_metadata_rejected(change):
    fields = {
        "st_mode": stat.S_IFREG | 0o400,
        "st_uid": 0,
        "st_gid": 0,
        "st_nlink": 1,
        "st_size": 4112,
    }
    fields.update(change)
    with pytest.raises(installer.Blocked):
        installer.host_key_metadata(SimpleNamespace(**fields))


def test_host_key_gate_in_preflight_before_any_secret_or_account():
    t = Transaction()

    def preflight():
        t.calls.append("preflight")
        installer.host_key_metadata(
            SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1, st_size=4112
            )
        )

    t.preflight = preflight
    out = installer.initialize(t)
    assert t.calls == ["preflight", "rollback"]
    assert out["stage"] == "preflight" and out["code"] == "host_key_metadata_unverified"
    assert "input_token" not in t.calls and "create_account" not in t.calls


@pytest.mark.parametrize("template_present", [False, True])
def test_native_preflight_unit_or_key_gate_denies_before_token_or_mutation(
    monkeypatch, template_present
):
    calls = []
    fake = SimpleNamespace(
        SERVICE=entry.SERVICE,
        root_dir=lambda p: calls.append(("metadata_parent", str(p))),
        service_state=lambda n: STATUS,
        read_root=lambda p: b'{"targets":[]}',
        broker_pins=lambda h: None,
    )
    boot = installer.NativeBootstrap({}, fake)
    monkeypatch.setattr(boot, "verify_native_tools", lambda: None)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        installer.os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server")
    )

    def user(name):
        if name == installer.ACCOUNT:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=1000, pw_gid=1000)

    monkeypatch.setattr(installer.pwd, "getpwnam", user)
    monkeypatch.setattr(installer.grp, "getgrnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(Path, "exists", lambda p: False)
    monkeypatch.setattr(Path, "is_symlink", lambda p: False)
    monkeypatch.setattr(installer, "path_absent", lambda p: True)

    def metadata(p):
        assert str(p) == "/var/lib/systemd/credential.secret"
        return SimpleNamespace(
            st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o644, st_nlink=1, st_size=4112
        )

    monkeypatch.setattr(Path, "lstat", metadata)

    def run(argv, **kwargs):
        calls.append(("readonly_command", argv))
        if argv == ("/usr/bin/sudo", "--version"):
            return b"sudo-rs 0.2.13\n"
        if argv[:2] == ("/usr/bin/systemctl", "show"):
            # Real systemd rejects @.service with exit 1; only an instantiated
            # name can prove whether a template resolves. Never start it.
            assert argv[2] in (
                "api-quota-broker-ops.socket",
                "api-quota-broker-ops@preflight.service",
            )
            if template_present and argv[2] == "api-quota-broker-ops@preflight.service":
                return b"loaded\n"
            return b"not-found\n"
        assert argv == ("/usr/sbin/visudo", "-c")
        return b""

    monkeypatch.setattr(boot, "run", run)
    monkeypatch.setattr(boot, "input_token", lambda: pytest.fail("secret read"))
    monkeypatch.setattr(boot, "create_account", lambda: pytest.fail("native mutation"))
    out = installer.initialize(boot)
    assert out["code"] == (
        "ops_unit_exists" if template_present else "host_key_metadata_unverified"
    )
    assert out["stage"] == "preflight"
    assert out["rollback_verified"] is True and not boot.creation_attempted
    assert boot.receipt_dir is None
    assert out["check"] == ("template_absence" if template_present else "host_key")
    if template_present:
        assert out["actual"] == "present_or_unknown"
    assert any(
        kind == "readonly_command" and value[2:3] == ("api-quota-broker-ops@preflight.service",)
        for kind, value in calls
    )


@pytest.mark.parametrize(
    "error,code",
    [
        (entry.Denied("native_failed"), "native_failed"),
        (entry.Denied("pin_changed"), "pin_changed"),
        (entry.Denied(FAKE_PASSWORD), "initialization_unverified"),
        (installer.Blocked(FAKE_PASSWORD), "initialization_unverified"),
        (PermissionError(13, FAKE_PASSWORD), "required_path_inaccessible"),
        (FileNotFoundError(2, FAKE_PASSWORD), "required_path_missing"),
        (OSError(5, FAKE_PASSWORD), "native_os_failure"),
        (ValueError(FAKE_PASSWORD), "initialization_unverified"),
    ],
)
def test_preflight_safe_checkpoint_and_error_without_raw_exception(error, code):
    t = Transaction()
    t.e = entry
    t.checkpoint = "template_absence"

    def preflight():
        t.calls.append("preflight")
        raise error

    t.preflight = preflight
    out = installer.initialize(t)
    assert out["code"] == code and out["check"] == "template_absence"
    assert out["expected"] == "LoadState=not-found" and out["actual"] == "query_failed"
    assert t.calls == ["preflight", "rollback"]
    assert FAKE_PASSWORD not in json.dumps(out)


def test_native_systemd_invalid_template_exit_is_diagnosed(monkeypatch):
    class Process:
        returncode = 1

        def communicate(self, *args, **kwargs):
            return b"", None

        def kill(self):
            pass

    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **kw: Process())
    t = Transaction()
    t.e = entry
    t.checkpoint = "template_absence"
    t.preflight = lambda: entry.native(
        (
            "/usr/bin/systemctl",
            "show",
            "api-quota-broker-ops@.service",
            "--property=LoadState",
            "--value",
        )
    )
    out = installer.initialize(t)
    assert out["code"] == "native_exit" and out["check"] == "template_absence"
    assert out["rc"] == 1
    assert out["actual"] == "query_failed" and out["rollback_verified"]


@pytest.mark.parametrize(
    "main,parts",
    [
        ("Match Address 192.0.2.0/24\nPasswordAuthentication yes", []),
        (
            "Include /etc/ssh/sshd_config.d/*.conf",
            ["Match Host *.example\nPasswordAuthentication yes"],
        ),
        ("Include /etc/ssh/sshd_config.d/*.conf", ["Match User morris"]),
        ("Include /home/morris/ssh.conf", []),
        ("Include /etc/ssh/sshd_config.d/*.conf", ["Include /etc/other.conf"]),
    ],
)
def test_address_dependent_or_unsupported_ssh_scope_denies(main, parts):
    with pytest.raises(installer.Blocked):
        installer.ssh_global_config(main, parts)


def test_global_only_standard_ssh_configuration_supported():
    installer.ssh_global_config(
        "Include /etc/ssh/sshd_config.d/*.conf\n# Match ignored comment",
        ["PasswordAuthentication no\nKbdInteractiveAuthentication no"],
    )


@pytest.mark.parametrize("line", ["SSHD_OPTS=", 'SSHD_OPTS=""', "SSHD_OPTS=''", "# no overrides"])
def test_empty_ssh_launch_options_supported(line):
    installer.ssh_options_file(line)


@pytest.mark.parametrize(
    "line",
    [
        'SSHD_OPTS="-o PasswordAuthentication=yes"',
        'SSHD_OPTS="-f /home/morris/sshd_config"',
        "OTHER_ENV=x",
    ],
)
def test_nonempty_or_unknown_ssh_launch_options_deny(line):
    with pytest.raises(installer.Blocked):
        installer.ssh_options_file(line)


def safe_ssh_fields():
    return {
        **{name: "no" for name in installer.SSH_BOOL_FIELDS},
        "authorizedkeyscommand": "none",
        "trustedusercakeys": "none",
        "authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2",
    }


@pytest.mark.parametrize("name", installer.SSH_FIELDS)
def test_each_ssh_field_has_safe_specific_failure_evidence(name):
    fields = safe_ssh_fields()
    fields[name] = "yes" if name in installer.SSH_BOOL_FIELDS else FAKE_PASSWORD
    finding = installer.ssh_findings(fields)
    assert len(finding) == 1 and finding[0]["field"] == name
    assert FAKE_PASSWORD not in json.dumps(finding)
    assert "ssh_" + name + "_not_isolated" in installer.SAFE_CODES


@pytest.mark.parametrize(
    "value",
    [
        "none",
        ".ssh/authorized_keys",
        ".ssh/authorized_keys2",
        ".ssh/authorized_keys2   .ssh/authorized_keys",
    ],
)
def test_default_home_keyfile_subsets_and_whitespace_are_safe_equivalents(value):
    fields = safe_ssh_fields()
    fields["authorizedkeysfile"] = value
    assert installer.ssh_findings(fields) == []


@pytest.mark.parametrize("value", ["/etc/ssh/keys", "../keys", "%h/keys", "", FAKE_PASSWORD])
def test_external_or_unknown_keyfile_remains_denied_without_path_in_receipt(value):
    fields = safe_ssh_fields()
    fields["authorizedkeysfile"] = value
    finding = installer.ssh_findings(fields)
    assert finding == [
        {
            "field": "authorizedkeysfile",
            "expected": "none_or_default_home_relative",
            "actual": "custom_or_external",
        }
    ]
    assert value not in json.dumps(finding) if value else True


def test_native_ssh_parser_keeps_only_whitelist_and_rejects_duplicate():
    raw = b"passwordauthentication no\nauthorizedkeysfile .ssh/authorized_keys\nunknown PUBLIC_FIXTURE\n"
    assert installer.ssh_fields(raw) == {
        "passwordauthentication": "no",
        "authorizedkeysfile": ".ssh/authorized_keys",
    }
    with pytest.raises(installer.Blocked, match="ssh_output_unverified"):
        installer.ssh_fields(raw + b"passwordauthentication yes\n")


def test_ssh_receipt_reports_all_bad_fields_without_raw_custom_values():
    t = Transaction()
    t.e = entry
    t.checkpoint = "ssh_policy"
    t.ssh_fields = {name: FAKE_PASSWORD for name in installer.SSH_FIELDS}

    def fail():
        raise installer.Blocked("ssh_passwordauthentication_not_isolated")

    t.preflight = fail
    out = installer.initialize(t)
    assert out["check"] == "ssh_policy" and out["code"] == "ssh_passwordauthentication_not_isolated"
    assert len(out["ssh_mismatches"]) == 7
    assert FAKE_PASSWORD not in json.dumps(out)
    assert t.calls == ["rollback"]


def ssh_baseline():
    return {
        **safe_ssh_fields(),
        "passwordauthentication": "yes",
        "denyusers": "old-user legacy@192.0.2.*",
        "allowtcpforwarding": "yes",
        "permitopen": "any",
        "subsystem": "sftp /PUBLIC/fixture",
    }


def ssh_with_deny(before):
    return {**before, "denyusers": (installer.ACCOUNT, *installer.deny_patterns(before))}


def test_denyusers_accumulates_preserving_every_old_pattern_and_other_setting():
    before = ssh_baseline()
    installer.ssh_candidate_equal(before, ssh_with_deny(before))
    reordered = {**before, "denyusers": "legacy@192.0.2.* broker-deploy old-user"}
    installer.ssh_candidate_equal(before, reordered)


def test_effective_native_dump_preserves_repeated_list_settings():
    raw = "".join(k + " " + v + "\n" for k, v in ssh_baseline().items()).encode()
    raw += b"hostkey /PUBLIC/rsa\nhostkey /PUBLIC/ecdsa\nsubsystem custom /PUBLIC/custom\n"
    fields = installer.ssh_effective(raw)
    assert fields["hostkey"] == ("/PUBLIC/rsa", "/PUBLIC/ecdsa")
    assert fields["subsystem"] == ("sftp /PUBLIC/fixture", "custom /PUBLIC/custom")
    installer.ssh_candidate_equal(fields, ssh_with_deny(fields))
    with pytest.raises(installer.Blocked, match="ssh_output_unverified"):
        installer.ssh_effective(raw + b"passwordauthentication no\n")


def test_native_denyusers_repeated_lines_preserve_all_patterns_and_exact_account():
    raw = b"denyusers old-user\ndenyusers broker-deploy\ndenyusers legacy@192.0.2.*\n"
    fields = installer.ssh_fields(raw)
    assert installer.deny_patterns(fields) == ("old-user", "broker-deploy", "legacy@192.0.2.*")
    assert installer.exact_ssh_denial(fields)
    baseline_raw = "".join(k + " " + v + "\n" for k, v in safe_ssh_fields().items()).encode()
    before = installer.ssh_effective(
        baseline_raw + b"denyusers old-user\ndenyusers legacy@192.0.2.*\n"
    )
    after = installer.ssh_effective(baseline_raw + raw)
    installer.ssh_candidate_equal(before, after)


def test_denyusers_multi_pattern_line_flattens_without_losing_duplicates():
    fields = installer.ssh_fields(
        b"denyusers old-user other-user\ndenyusers broker-deploy old-user\n"
    )
    assert installer.deny_patterns(fields) == (
        "old-user",
        "other-user",
        "broker-deploy",
        "old-user",
    )
    assert installer.exact_ssh_denial(fields)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"denyusers": "broker-deploy"}, "ssh_deny_list_changed"),
        ({"denyusers": "* old-user legacy@192.0.2.*"}, "ssh_deny_not_effective"),
        (
            {"denyusers": "broker-deploy@localhost old-user legacy@192.0.2.*"},
            "ssh_deny_not_effective",
        ),
        ({"allowtcpforwarding": "no"}, "ssh_other_settings_changed"),
        ({"passwordauthentication": "no"}, "ssh_other_settings_changed"),
        ({"denyusers": "broker-deploy other old-user legacy@192.0.2.*"}, "ssh_deny_list_changed"),
    ],
)
def test_candidate_rejects_lost_deny_pattern_broader_denial_or_morris_config_change(change, code):
    before = ssh_baseline()
    after = {**ssh_with_deny(before), **change}
    with pytest.raises(installer.Blocked, match=code):
        installer.ssh_candidate_equal(before, after)


@pytest.mark.parametrize("denied", [False, True])
def test_verified_global_exact_deny_allows_password_ssh_for_other_users(monkeypatch, denied):
    boot = installer.NativeBootstrap({}, entry)
    monkeypatch.setattr(boot, "ssh_source", dict)
    fields = ssh_baseline()
    if denied:
        fields = ssh_with_deny(fields)
    raw = "".join(
        k + " " + item + "\n"
        for k, v in fields.items()
        for item in (v if type(v) is tuple else (v,))
    ).encode()
    monkeypatch.setattr(boot, "run", lambda *a, **kw: raw)
    if denied:
        boot.verify_ssh()
    else:
        with pytest.raises(installer.Blocked, match="ssh_passwordauthentication_not_isolated"):
            boot.verify_ssh()


@pytest.mark.parametrize("drift", [False, True])
def test_readonly_ssh_candidate_checks_both_users_before_mutation(monkeypatch, drift):
    boot = installer.NativeBootstrap(
        {"broker_ops.ssh-deny.proposal": installer.SSH_DENY_BYTES},
        entry,
        ssh_change_authorized=True,
    )
    boot.ssh_needs_change = True
    calls = []
    monkeypatch.setattr(boot, "ssh_source", lambda: {"PUBLIC_SOURCE": "PUBLIC_SHA"})
    monkeypatch.setattr(boot, "run", lambda argv, **kw: calls.append(argv) or b"")

    def effective(user, *, candidate=False):
        calls.append(("effective", user, candidate))
        d = ssh_with_deny(ssh_baseline()) if candidate else ssh_baseline()
        if drift and user == "morris" and candidate:
            d["allowtcpforwarding"] = "no"
        return d

    monkeypatch.setattr(boot, "effective_ssh", effective)
    if drift:
        with pytest.raises(installer.Blocked, match="ssh_other_settings_changed"):
            boot.prepare_ssh_deny()
    else:
        boot.prepare_ssh_deny()
    assert ("effective", "morris", True) in calls and (
        "effective",
        installer.ACCOUNT,
        True,
    ) in calls
    assert all("reload" not in c and "create_account" not in c for c in calls)


@pytest.mark.parametrize(
    "failure", [None, "pre_create", "partial_write", "syntax", "reload", "source_drift"]
)
def test_isolated_ssh_fragment_transaction_and_safe_rollback(monkeypatch, tmp_path, failure):
    fragment = tmp_path / "deny.conf"
    monkeypatch.setattr(installer, "SSH_DENY", fragment)
    calls = []
    failed = False
    drift = False

    def write(path, raw, **kw):
        assert path == fragment and kw == {"mode": 0o644}
        calls.append("write")
        if failure == "pre_create":
            raise OSError("PUBLIC_CREATE_FAILURE")
        with path.open("xb") as f:
            f.write(raw[:10] if failure == "partial_write" else raw)
        if failure == "partial_write":
            raise OSError("PUBLIC_WRITE_FAILURE")

    def read(path, *, sha, mode):
        assert path == fragment and mode == 0o644
        raw = path.read_bytes()
        if __import__("hashlib").sha256(raw).hexdigest() != sha:
            raise entry.Denied("pin_changed")
        return raw

    fake = SimpleNamespace(root_dir=lambda p: None, write_exclusive=write, read_root=read)
    boot = installer.NativeBootstrap({}, fake, ssh_change_authorized=True)
    boot.ssh_needs_change = True
    boot.ssh_hashes = {"PUBLIC_SOURCE": "ORIGINAL"}
    boot.ssh_before = {u: ssh_baseline() for u in (installer.ACCOUNT, "morris")}

    def sources():
        d = {"PUBLIC_SOURCE": "DRIFTED" if drift else "ORIGINAL"}
        if fragment.exists():
            d[str(fragment)] = "FIXTURE_HASH"
        return d

    monkeypatch.setattr(boot, "ssh_source", sources)
    monkeypatch.setattr(
        boot,
        "effective_ssh",
        lambda user: ssh_with_deny(ssh_baseline()) if fragment.exists() else ssh_baseline(),
    )
    monkeypatch.setattr(boot, "verify_ssh_reload", lambda: calls.append("reload_interface"))
    monkeypatch.setattr(boot, "verify_ssh", lambda: calls.append("exact_deny_verified"))
    monkeypatch.setattr(boot, "preservation", lambda: calls.append("services_unchanged"))

    def run(argv, **kw):
        nonlocal failed, drift
        calls.append(argv)
        if not failed and (
            (failure == "syntax" and argv == ("/usr/sbin/sshd", "-t"))
            or (failure in ("reload", "source_drift") and "reload" in argv)
        ):
            failed = True
            drift = failure == "source_drift"
            raise entry.Denied("native_failed")
        return b""

    monkeypatch.setattr(boot, "run", run)
    if failure:
        with pytest.raises((OSError, entry.Denied)):
            boot.apply_ssh_deny()
    else:
        boot.apply_ssh_deny()
        assert fragment.read_bytes() == installer.SSH_DENY_BYTES
    safe = boot.rollback()
    if failure in ("partial_write", "source_drift"):
        assert safe is False and fragment.exists()
    else:
        assert safe and not fragment.exists()
    assert all("restart" not in c for c in calls)
    if failure in ("partial_write", "pre_create"):
        assert not any(type(c) is tuple and "reload" in c for c in calls)


def test_ssh_mutation_requires_authorization_and_follows_prepared_token_and_password(monkeypatch):
    boot = installer.NativeBootstrap({}, entry)
    with pytest.raises(installer.Blocked, match="ssh_change_authorization_required"):
        boot.apply_ssh_deny()
    t = Transaction()
    t.ssh_change_authorized = True
    t.apply_ssh_deny = lambda: t.calls.append("apply_ssh_deny")
    result = installer.initialize(t)
    assert result["status"] == "passed"
    assert (
        t.calls.index("input_token")
        < t.calls.index("fetch_password")
        < t.calls.index("apply_ssh_deny")
        < t.calls.index("create_account")
    )
    t = Transaction()
    t.ssh_change_authorized = True

    def fail():
        raise installer.Blocked("ssh_reload_unverified")

    t.apply_ssh_deny = fail
    result = installer.initialize(t)
    assert result["stage"] == "ssh_account_isolation" and "create_account" not in t.calls
    assert not any(t.token) and not any(t.password)


@pytest.mark.parametrize("failure", ["input_token", "fetch_password"])
def test_token_or_password_failure_has_no_ssh_account_or_publish_effect(failure):
    t = Transaction(failure)
    t.ssh_change_authorized = True
    t.apply_ssh_deny = lambda: pytest.fail("SSH mutation before token/password ready")
    result = installer.initialize(t)
    assert result["status"] == "blocked"
    assert not any(stage in t.calls for stage in ("create_account", "set_password", "publish"))


def budget_transaction(failure=None):
    t = Transaction(failure)
    b = installer.NativeBootstrap({}, entry, ssh_change_authorized=True)
    b.deadline = 300
    for name in Transaction.stages:
        setattr(b, name, getattr(t, name))
    b.rollback = t.rollback
    b.receipt = t.receipt
    b.apply_ssh_deny = lambda: t.calls.append("apply_ssh_deny")
    return b, t


def test_insufficient_remaining_time_stops_before_doppler_or_ssh_mutation(monkeypatch):
    b, t = budget_transaction()
    monkeypatch.setattr(installer.time, "monotonic", lambda: 121)
    out = installer.initialize(b)
    assert out["code"] == "bootstrap_budget_insufficient"
    assert (
        "fetch_password" not in t.calls
        and "apply_ssh_deny" not in t.calls
        and "create_account" not in t.calls
    )
    assert not any(t.token)


def test_doppler_time_consumption_rechecks_budget_before_first_mutation(monkeypatch):
    b, t = budget_transaction()
    clock = iter([100, 121])
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(clock))
    out = installer.initialize(b)
    assert out["code"] == "bootstrap_budget_insufficient" and "fetch_password" in t.calls
    assert "apply_ssh_deny" not in t.calls and "create_account" not in t.calls
    assert not any(t.token) and not any(t.password)


def test_native_commands_leave_reserved_rollback_time(monkeypatch):
    calls = []
    b = installer.NativeBootstrap(
        {}, SimpleNamespace(native=lambda argv, **kw: calls.append(kw["timeout"]) or b"")
    )
    b.deadline = 300
    monkeypatch.setattr(installer.time, "monotonic", lambda: 230)
    b.run(("/usr/sbin/sshd", "-t"), timeout=15)
    assert calls == [9.5]
    monkeypatch.setattr(installer.time, "monotonic", lambda: 241)
    with pytest.raises(installer.Blocked, match="bootstrap_budget_insufficient"):
        b.run(("/usr/sbin/sshd", "-t"))
    b.rolling_back = True
    b.run(("/usr/sbin/sshd", "-t"), timeout=15)
    assert calls == [9.5, 15]


@pytest.mark.parametrize(
    "code", ["bootstrap_budget_insufficient", "bootstrap_termination_requested"]
)
def test_fixed_budget_signal_rolls_back_and_wipes_without_ssh_when_waiting_for_input(code):
    t = Transaction()
    t.ssh_change_authorized = True

    def stop():
        raise installer.BootstrapStop(code)

    t.input_token = stop
    t.apply_ssh_deny = lambda: pytest.fail("SSH while waiting for human")
    out = installer.initialize(t)
    assert out["code"] == code and out["rollback_verified"]
    assert "create_account" not in t.calls and "publish" not in t.calls


def test_native_child_is_collected_and_fixed_interrupt_propagates(monkeypatch):
    calls = []

    class Process:
        def communicate(self, *a, **kw):
            calls.append("communicate")
            if calls == ["communicate"]:
                raise installer.BootstrapStop("bootstrap_termination_requested")
            return b"", None

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **kw: Process())
    with pytest.raises(installer.BootstrapStop, match="bootstrap_termination_requested"):
        entry.native(("/usr/sbin/sshd", "-t"))
    assert calls == ["communicate", "kill", "communicate"]


@pytest.mark.parametrize("pid_matches", [True, False])
def test_boot_budget_uses_real_service_main_start_not_human_wall_clock(monkeypatch, pid_matches):
    timers = []
    b = installer.NativeBootstrap({}, SimpleNamespace(memory_guard=lambda **kw: None))
    monkeypatch.setattr(installer.os, "isatty", lambda fd: True)
    monkeypatch.setattr(installer.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(installer.signal, "setitimer", lambda kind, seconds: timers.append(seconds))
    pid = os.getpid() if pid_matches else os.getpid() + 1
    monkeypatch.setattr(
        b,
        "run",
        lambda *a, **kw: f"MainPID={pid}\nExecMainStartTimestampMonotonic=25000000\n".encode(),
    )
    if pid_matches:
        b.guard()
        assert b.deadline == 325 and timers == [165]
    else:
        with pytest.raises(installer.Blocked, match="bootstrap_budget_unverified"):
            b.guard()
        assert timers == []


@pytest.mark.parametrize("authorized", [False, True])
def test_normal_initialization_still_refuses_preexisting_fragment(monkeypatch, authorized):
    b = installer.NativeBootstrap(
        {}, SimpleNamespace(root_dir=lambda p: None), ssh_change_authorized=authorized
    )
    # The actual continuation decision is deferred; default gate stays strict.
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        installer.os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server")
    )
    monkeypatch.setattr(
        installer.pwd,
        "getpwnam",
        lambda n: (
            SimpleNamespace(pw_uid=1000, pw_gid=1000)
            if n == "morris"
            else (_ for _ in ()).throw(KeyError(n))
        ),
    )
    monkeypatch.setattr(installer.grp, "getgrnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(Path, "exists", lambda p: p == installer.SSH_DENY)
    monkeypatch.setattr(Path, "is_symlink", lambda p: False)
    monkeypatch.setattr(installer, "path_absent", lambda p: Path(p) != installer.SSH_DENY)
    with pytest.raises(installer.Blocked, match="ssh_fragment_exists"):
        b.preflight()


@pytest.mark.parametrize(
    "change",
    [
        None,
        "receipt_time_2ns",
        "leaf_time_2ns",
        "receipt_hash",
        "leaf_hash",
        "owner",
        "hardlink",
        "extra_receipt",
    ],
)
def test_explicit_recovery_pins_original_receipt_and_leaf_without_mutation(
    monkeypatch, tmp_path, change
):
    import hashlib

    projection = json.loads(
        (ROOT / "tests/fixtures/asus-history/r8-prepared-projection.json").read_text()
    )["projection"]["receipts"][0]
    assert projection["mtime_ns"] == "1791260682969549998"
    data = {
        k: projection[k]
        for k in (
            "status",
            "mode",
            "preflight_checks_passed",
            "ssh_change_needed",
            "provider_calls",
        )
    }
    raw = (json.dumps(data, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == installer.RECOVERY_RECEIPT_SHA
    folder = tmp_path / "old-bootstrap"
    folder.mkdir()
    receipt = folder / "receipt.json"
    receipt.write_bytes(raw)
    receipt.chmod(0o600)
    leaf = tmp_path / "deny.conf"
    leaf.write_bytes(installer.SSH_DENY_BYTES)
    leaf.chmod(0o644)
    monkeypatch.setattr(installer, "RECOVERY_RECEIPT", receipt)
    monkeypatch.setattr(installer, "SSH_DENY", leaf)
    if change == "extra_receipt":
        (folder / "another.json").write_text("PUBLIC_FIXTURE")
    if change == "receipt_hash":
        receipt.write_bytes(raw + b" ")
    if change == "leaf_hash":
        leaf.write_bytes(b"PUBLIC_UNREVIEWED_RULE")
    original = Path.lstat

    def stat_info(path):
        s = original(path)
        values = {
            k: getattr(s, k)
            for k in ("st_mode", "st_uid", "st_gid", "st_nlink", "st_mtime_ns", "st_ctime_ns")
        }
        values.update(st_uid=0, st_gid=0)
        timestamp = (
            installer.RECOVERY_MTIME_NS if path == receipt else installer.RECOVERY_LEAF_TIME_NS
        )
        values.update(st_mtime_ns=timestamp, st_ctime_ns=timestamp)
        if change == "receipt_time_2ns" and path == receipt:
            values["st_mtime_ns"] += 2
        if change == "leaf_time_2ns" and path == leaf:
            values["st_ctime_ns"] += 2
        if change == "owner" and path == leaf:
            values["st_uid"] = 1000
        if change == "hardlink" and path == leaf:
            values["st_nlink"] = 2
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, "lstat", stat_info)

    def read(path, *, sha, **kw):
        value = path.read_bytes()
        if hashlib.sha256(value).hexdigest() != sha:
            raise entry.Denied("pin_changed")
        return value

    boot = installer.NativeBootstrap(
        {},
        SimpleNamespace(root_dir=lambda *a, **kw: None, read_root=read),
        ssh_change_authorized=True,
        continue_pinned_ssh=True,
    )
    if change:
        with pytest.raises(installer.Blocked, match="recovery_pin_unverified"):
            boot.verify_recovery_pin()
    else:
        boot.verify_recovery_pin()
    assert not boot.ssh_written and not boot.ssh_write_intent


@pytest.mark.parametrize(
    "change",
    [
        None,
        "mtime",
        "ctime",
        "unit",
        "binary",
        "missing_include",
        "extra_include",
        "directory_changed",
        "helper_missing",
        "helper_custom",
        "legacy_hyphen_keys",
    ],
)
def test_recovery_time_fence_and_exact_sources_reject_pending_configuration(monkeypatch, change):
    hashes = {
        name: "PUBLIC_HASH" for name in installer.RECOVERY_CONFIG_PATHS | {str(installer.SSH_DENY)}
    }
    if change == "missing_include":
        hashes.pop("/etc/ssh/sshd_config.d/50-cloud-init.conf")
    if change == "extra_include":
        hashes["/etc/ssh/sshd_config.d/extra.conf"] = "PUBLIC_HASH"
    b = installer.NativeBootstrap(
        {},
        SimpleNamespace(read_root=lambda *a, **kw: b"PUBLIC_SOURCE"),
        ssh_change_authorized=True,
        continue_pinned_ssh=True,
    )
    b.ssh_before = {
        u: {
            "sshdsessionpath": "/usr/lib/openssh/sshd-session",
            "sshdauthpath": "/usr/lib/openssh/sshd-auth",
        }
        for u in (installer.ACCOUNT, "morris")
    }
    native = (ROOT / "tests/fixtures/asus-sshd-10.2-public-helper-fields.txt").read_text()
    helper_values = dict(line.split(" ", 1) for line in native.splitlines())
    assert helper_values == b.ssh_before[installer.ACCOUNT]
    if change == "helper_missing":
        b.ssh_before[installer.ACCOUNT].pop("sshdsessionpath")
    if change == "helper_custom":
        b.ssh_before[installer.ACCOUNT]["sshdauthpath"] = FAKE_PASSWORD
    if change == "legacy_hyphen_keys":
        b.ssh_before[installer.ACCOUNT] = {
            "sshd-sessionpath": "/usr/lib/openssh/sshd-session",
            "sshd-authpath": "/usr/lib/openssh/sshd-auth",
        }
    monkeypatch.setattr(b, "ssh_source", lambda: hashes)

    def info(path):
        values = {
            "st_mode": stat.S_IFREG | 0o644,
            "st_uid": 0,
            "st_gid": 0,
            "st_nlink": 1,
            "st_dev": 1,
            "st_ino": 2,
            "st_size": 13,
            "st_mtime_ns": installer.RECOVERY_MTIME_NS - 1,
            "st_ctime_ns": installer.RECOVERY_MTIME_NS - 1,
        }
        if path == installer.SSH_DENY:
            values.update(
                st_mtime_ns=installer.RECOVERY_LEAF_TIME_NS,
                st_ctime_ns=installer.RECOVERY_LEAF_TIME_NS,
            )
        if path == installer.SSH_DENY.parent:
            values.update(st_mode=stat.S_IFDIR | 0o755)
        if (
            (change in ("mtime", "ctime") and str(path) == "/etc/ssh/sshd_config")
            or (change == "unit" and str(path) == installer.RECOVERY_UNIT)
            or (change == "binary" and str(path) == "/usr/sbin/sshd")
        ):
            values["st_ctime_ns" if change == "ctime" else "st_mtime_ns"] = (
                installer.RECOVERY_MTIME_NS + 1
            )
        if change == "directory_changed" and path == installer.SSH_DENY.parent:
            values["st_ctime_ns"] = installer.RECOVERY_LEAF_TIME_NS + 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, "lstat", info)
    if change:
        with pytest.raises(installer.Blocked, match="recovery_source_changed"):
            b.recovery_sources()
        if change in ("helper_missing", "helper_custom", "legacy_hyphen_keys"):
            assert b.recovery_diagnostic["predicate"] == "helper_effective_path"
            assert b.recovery_diagnostic["user"] == installer.ACCOUNT
            assert b.recovery_diagnostic["field"] in ("sshdsessionpath", "sshdauthpath")
            assert FAKE_PASSWORD not in json.dumps(b.recovery_diagnostic)
    else:
        assert len(b.recovery_sources()) == 7


def test_recovery_receipt_preserves_first_safe_predicate_before_cleanup_changes_diagnostic():
    t = Transaction()
    t.e = entry
    t.checkpoint = "ssh_candidate"
    initial = {
        "predicate": "helper_effective_path",
        "user": "broker-deploy",
        "field": "sshdsessionpath",
        "actual": "missing",
    }
    t.recovery_diagnostic = initial

    def fail():
        raise installer.Blocked("recovery_source_changed")

    def rollback():
        t.recovery_diagnostic = {"predicate": "ctime_fence"}
        return True

    t.preflight = fail
    t.rollback = rollback
    result = installer.initialize(t)
    assert result["recovery_source_failure"] == initial


@pytest.mark.parametrize("failure", [None, "pin", "reload", "source_drift"])
def test_pinned_continuation_reloads_once_and_never_deletes_or_rewrites_existing_leaf(
    monkeypatch, tmp_path, failure
):
    leaf = tmp_path / "deny.conf"
    leaf.write_bytes(installer.SSH_DENY_BYTES)
    before = leaf.stat()
    monkeypatch.setattr(installer, "SSH_DENY", leaf)
    b = installer.NativeBootstrap({}, entry, ssh_change_authorized=True, continue_pinned_ssh=True)
    b.ssh_preexisting = True
    b.ssh_hashes = {"public": "HASH"}
    b.recovery_source_fingerprint = {"public": "OLD"}
    b.ssh_before = {u: ssh_with_deny(ssh_baseline()) for u in (installer.ACCOUNT, "morris")}
    calls = []
    drift = False

    def pin():
        if failure == "pin":
            raise installer.Blocked("recovery_pin_unverified")

    monkeypatch.setattr(b, "verify_recovery_pin", pin)
    monkeypatch.setattr(b, "ssh_source", lambda: b.ssh_hashes)
    monkeypatch.setattr(b, "recovery_sources", lambda: {"public": "CHANGED" if drift else "OLD"})
    monkeypatch.setattr(b, "effective_ssh", lambda user: b.ssh_before[user])
    monkeypatch.setattr(b, "verify_ssh_reload", lambda: None)
    monkeypatch.setattr(b, "verify_ssh", lambda: None)
    monkeypatch.setattr(b, "preservation", lambda: None)
    monkeypatch.setattr(b, "audit", lambda point: calls.append(point))

    def run(argv, **kw):
        nonlocal drift
        calls.append(argv)
        if "reload" in argv:
            if failure == "reload":
                raise entry.Denied("native_failed")
            if failure == "source_drift":
                drift = True
        return b""

    monkeypatch.setattr(b, "run", run)
    if failure:
        with pytest.raises((installer.Blocked, entry.Denied)):
            b.apply_ssh_deny()
    else:
        b.apply_ssh_deny()
    b.rollback()
    after = leaf.stat()
    assert leaf.read_bytes() == installer.SSH_DENY_BYTES
    assert (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    assert not b.ssh_written and not b.ssh_write_intent
    assert sum(type(c) is tuple and "reload" in c for c in calls) == (0 if failure == "pin" else 1)


@pytest.mark.parametrize("bad", [False, True])
def test_native_tool_metadata_uses_correct_chage_and_allows_known_privilege_bits(monkeypatch, bad):
    parents = []
    boot = installer.NativeBootstrap({}, SimpleNamespace(root_dir=lambda p: parents.append(str(p))))
    monkeypatch.setattr(Path, "resolve", lambda p, **kw: p)

    def info(p):
        mode = 0o755
        if str(p) in ("/usr/bin/sudo", "/usr/bin/passwd"):
            mode = 0o4755
        if str(p) == "/usr/bin/chage":
            mode = 0o2775 if bad else 0o2755
        return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | mode)

    monkeypatch.setattr(Path, "lstat", info)
    monkeypatch.setattr(
        boot, "run", lambda argv, **kw: b"--list --iso8601 encrypt --with-key= --name="
    )
    if bad:
        with pytest.raises(installer.Blocked, match="native_tool_unverified"):
            boot.verify_native_tools()
    else:
        boot.verify_native_tools()
    assert "/usr/bin" in parents


def test_uncertain_account_lock_keeps_new_ssh_denial(monkeypatch, tmp_path):
    fragment = tmp_path / "deny.conf"
    fragment.write_bytes(installer.SSH_DENY_BYTES)
    monkeypatch.setattr(installer, "SSH_DENY", fragment)
    boot = installer.NativeBootstrap({}, entry, ssh_change_authorized=True)
    boot.created = True
    boot.ssh_write_intent = True
    boot.ssh_written = True
    monkeypatch.setattr(
        boot, "account_identity", lambda: (_ for _ in ()).throw(entry.Denied("account_identity"))
    )
    assert boot.rollback() is False and fragment.read_bytes() == installer.SSH_DENY_BYTES


def test_ssh_reload_multi_property_lines_preserve_both_commands(monkeypatch):
    boot = installer.NativeBootstrap({}, entry)
    raw = b"CanReload=yes\nActiveState=active\nSubState=running\nMainPID=123\nExecReload={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; }\nExecReload={ path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; }\n"
    monkeypatch.setattr(boot, "run", lambda *a, **kw: raw)
    boot.verify_ssh_reload()
    assert boot.ssh_main_pid == "123"
    monkeypatch.setattr(boot, "run", lambda *a, **kw: raw.replace(b"MainPID=123", b"MainPID=124"))
    with pytest.raises(installer.Blocked, match="ssh_reload_unverified"):
        boot.verify_ssh_reload()


def test_failed_native_command_label_is_not_overwritten_by_rollback():
    t = Transaction()
    t.e = entry
    t.checkpoint = "ssh_candidate"
    t.command_label = "ssh_candidate_morris"

    def fail():
        raise entry.Denied("native_failed")

    def rollback():
        t.command_label = "ssh_reload"
        return True

    t.preflight = fail
    t.rollback = rollback
    out = installer.initialize(t)
    assert out["command"] == "ssh_candidate_morris" and out["code"] == "native_failed"


def test_successful_native_command_has_no_stale_label_on_later_metadata_error():
    boot = installer.NativeBootstrap({}, SimpleNamespace(native=lambda *a, **kw: b""))

    def preflight():
        boot.checkpoint = "host_key"
        boot.run(("/usr/sbin/sshd", "-t"))
        raise installer.Blocked("host_key_metadata_unverified")

    boot.preflight = preflight
    out = installer.initialize(boot)
    assert out["code"] == "host_key_metadata_unverified" and "command" not in out


def test_real_asus_cron_spool_1730_is_specialized_not_general_root_trust():
    info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o1730, st_uid=0, st_gid=987, st_nlink=2)
    installer.cron_spool_metadata(info, 987)
    assert info.st_mode & 0o022  # The unchanged general root_dir must reject it.


@pytest.mark.parametrize(
    "change",
    [
        {"st_mode": stat.S_IFLNK | 0o1730},
        {"st_mode": stat.S_IFREG | 0o1730},
        {"st_mode": stat.S_IFDIR | 0o730},
        {"st_mode": stat.S_IFDIR | 0o1777},
        {"st_mode": stat.S_IFDIR | 0o3730},
        {"st_uid": 1000},
        {"st_gid": 1000},
        {"st_nlink": 3},
    ],
)
def test_unsafe_or_wrong_cron_spool_metadata_remains_denied(change):
    fields = {"st_mode": stat.S_IFDIR | 0o1730, "st_uid": 0, "st_gid": 987, "st_nlink": 2}
    fields.update(change)
    with pytest.raises(installer.Blocked, match="cron_metadata_unverified"):
        installer.cron_spool_metadata(SimpleNamespace(**fields), 987)


@pytest.mark.parametrize(
    "raw,reference",
    [
        (b"# broker-deploy comment\nSHELL=/bin/sh\n", False),
        (b"* * * * * root PUBLIC_FIXTURE\n", False),
        (b"* * * * * broker-deploy PUBLIC_FIXTURE\n", True),
        (b"@reboot broker-deploy PUBLIC_FIXTURE\n", True),
        (b"@daily root PUBLIC_FIXTURE\n", False),
        (b"* * * * * broker-deploy-other PUBLIC_FIXTURE\n", False),
    ],
)
def test_system_cron_direct_account_reference_without_emitting_other_jobs(raw, reference):
    assert installer.cron_account_reference(raw) is reference


@pytest.mark.parametrize(
    "raw", [b"@unknown root PUBLIC_FIXTURE", b"malformed PUBLIC_FIXTURE", b"\0PUBLIC_FIXTURE"]
)
def test_unparseable_cron_schedule_is_fixed_safe_failure(raw):
    with pytest.raises(installer.Blocked, match="cron_schedule_unverified"):
        installer.cron_account_reference(raw)


@pytest.mark.parametrize("job_kind", [None, "system_reference", "spool_regular", "spool_symlink"])
def test_cron_checks_fixed_spool_job_without_enumerating_other_spool_users(monkeypatch, job_kind):
    parent_checks = []
    read_paths = []

    def read(path):
        read_paths.append(str(path))
        if str(path) == "/etc/cron.d/other-job" and job_kind == "system_reference":
            return b"* * * * * broker-deploy PUBLIC_FIXTURE\n"
        return b"* * * * * root PUBLIC_FIXTURE\n"

    boot = installer.NativeBootstrap(
        {}, SimpleNamespace(root_dir=lambda p: parent_checks.append(str(p)), read_root=read)
    )

    def metadata(p):
        name = str(p)
        if name == "/etc/crontab" or name == "/etc/cron.d/other-job":
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1)
        if name == "/etc/cron.d":
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0, st_nlink=2)
        if name == str(installer.CRON_SPOOL):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o1730, st_uid=0, st_gid=987, st_nlink=2)
        if name == str(installer.CRON_SPOOL / installer.ACCOUNT) and job_kind in (
            "spool_regular",
            "spool_symlink",
        ):
            return SimpleNamespace(
                st_mode=(stat.S_IFLNK if job_kind == "spool_symlink" else stat.S_IFREG) | 0o600
            )
        raise FileNotFoundError(name)

    monkeypatch.setattr(Path, "lstat", metadata)

    def entries(p):
        assert str(p) == "/etc/cron.d", "user spool must not be enumerated"
        return iter([Path("/etc/cron.d/other-job")])

    monkeypatch.setattr(Path, "iterdir", entries)
    monkeypatch.setattr(
        installer.grp,
        "getgrnam",
        lambda n: SimpleNamespace(gr_name="crontab", gr_gid=987, gr_mem=[]),
    )
    monkeypatch.setattr(installer.pwd, "getpwall", list)
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    if job_kind:
        with pytest.raises(installer.Blocked, match="ops_job_exists"):
            boot.verify_jobs()
    else:
        boot.verify_jobs()
    assert str(installer.CRON_SPOOL) not in parent_checks
    assert (
        str(installer.CRON_SPOOL.parent) in parent_checks
        if job_kind != "system_reference"
        else True
    )
    assert all(not p.startswith(str(installer.CRON_SPOOL)) for p in read_paths)


@pytest.mark.parametrize("kind", ["system_crontab", "system_directory", "user_spool"])
def test_dangling_symlink_at_each_fixed_cron_path_does_not_count_as_absent(monkeypatch, kind):
    boot = installer.NativeBootstrap({}, SimpleNamespace(root_dir=lambda p: None))
    target = {
        "system_crontab": "/etc/crontab",
        "system_directory": "/etc/cron.d",
        "user_spool": str(installer.CRON_SPOOL),
    }[kind]

    def info(p):
        if str(p) != target:
            raise FileNotFoundError(str(p))
        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_gid=987, st_nlink=1)

    monkeypatch.setattr(Path, "lstat", info)
    monkeypatch.setattr(
        installer.grp,
        "getgrnam",
        lambda n: SimpleNamespace(gr_name="crontab", gr_gid=987, gr_mem=[]),
    )
    monkeypatch.setattr(installer.pwd, "getpwall", list)
    with pytest.raises(installer.Blocked, match="cron_metadata_unverified"):
        boot.verify_jobs()


@pytest.mark.parametrize(
    "code",
    [
        "core_limit",
        "unit_scope",
        "swap_limit",
        "memory_limit",
        "dumpability",
        "sudo_privilege_transition_unavailable",
    ],
)
def test_real_bootstrap_guard_failure_keeps_fixed_kernel_code_before_token(code):
    t = Transaction()
    t.e = entry
    t.checkpoint = "bootstrap_guard"

    def fail():
        raise entry.Denied(code)

    t.preflight = fail
    result = installer.initialize(t)
    assert result["code"] == code and result["check"] == "bootstrap_guard"
    assert "input_token" not in t.calls


def test_native_guard_prechecked_once_is_not_repeated_before_token():
    t = Transaction()
    t.guarded = True
    result = installer.initialize(t)
    assert result["status"] == "passed" and "guard" not in t.calls


def test_absence_check_does_not_accept_dangling_home_symlink(tmp_path):
    link = tmp_path / "home"
    link.symlink_to(tmp_path / "missing-target")
    assert not installer.path_absent(link)
    assert installer.path_absent(tmp_path / "truly-absent")


@pytest.mark.parametrize(
    "source_uid,target_mode,target_uid,version,allowed",
    [
        (0, stat.S_IFREG | 0o755, 0, b"systemd 259 (259.1)\n", True),
        (1000, stat.S_IFREG | 0o755, 0, b"systemd 259\n", False),
        (0, stat.S_IFREG | 0o755, 1000, b"systemd 259\n", False),
        (0, stat.S_IFDIR | 0o755, 0, b"systemd 259\n", False),
        (0, stat.S_IFREG | 0o775, 0, b"systemd 259\n", False),
        (0, stat.S_IFREG | 0o4755, 0, b"systemd 259\n", False),
        (0, stat.S_IFREG | 0o644, 0, b"systemd 259\n", False),
        (0, stat.S_IFREG | 0o755, 0, b"systemd 258\n", False),
    ],
)
def test_credential_tool_metadata_and_version_gate(
    monkeypatch, source_uid, target_mode, target_uid, version, allowed
):
    calls = []
    fake = SimpleNamespace(root_dir=lambda p: calls.append(("parent", str(p))))
    boot = installer.NativeBootstrap({}, fake)
    target = Path("/PUBLIC_FIXTURE/systemd-creds")
    monkeypatch.setattr(Path, "resolve", lambda p, **kwargs: target)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda p: (
            SimpleNamespace(st_uid=source_uid)
            if str(p) == "/usr/bin/systemd-creds"
            else SimpleNamespace(st_uid=target_uid, st_mode=target_mode)
        ),
    )

    def run(argv, **kwargs):
        assert argv == ("/usr/bin/systemd-creds", "--version")
        calls.append(("version", argv))
        return version

    monkeypatch.setattr(boot, "run", run)
    if allowed:
        boot.verify_credential_tool()
        assert calls[-1][0] == "version"
    else:
        with pytest.raises(installer.Blocked, match="credential_tool_unverified"):
            boot.verify_credential_tool()
        if source_uid != 0 or target_uid != 0 or target_mode != stat.S_IFREG | 0o755:
            assert all(kind != "version" for kind, _ in calls)


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
    result = builder.build(tmp_path / "review", mode="--diagnose-retained-inspect")
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


@pytest.mark.parametrize("finish", ["input", "crlf", "paste", "multiline", "eof", "interrupt"])
def test_real_controlling_pty_hidden_input_is_not_echoed(finish):
    import select
    import termios
    import time

    fixture = "dp.st." + "A" * 40  # Public fabricated service-token syntax.
    code = f"""import fcntl,termios,os,json,importlib.util
fcntl.ioctl(0,termios.TIOCSCTTY,0)
spec=importlib.util.spec_from_file_location("pty_ops",{str(ROOT / "deploy/asus/install_ops.py")!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
before=termios.tcgetattr(0)
try:
    value=m.tty_line(0,"Hidden fixture: ",hidden=True)
    m.normalize_tty_token(value)
    result={{"status":"returned","match":value=={fixture.encode()!r}}}
    value[:]=b'\\0'*len(value)
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
            {
                "input": fixture.encode() + b"\n",
                "crlf": fixture.encode() + b"\r\n",
                "paste": b"\x1b[200~" + fixture.encode() + b"\x1b[201~\n",
                "multiline": fixture.encode() + b"\nPUBLIC_SECOND_LINE\n",
                "eof": b"\x04",
                "interrupt": b"\x03",
            }[finish],
        )
        while b'"restored"' not in raw:
            assert time.monotonic() < end
            if select.select([master], [], [], 0.1)[0]:
                raw += os.read(master, 8192)
        assert p.wait(timeout=2) == 0 and fixture.encode() not in raw
        report = json.JSONDecoder().raw_decode(raw.decode()[raw.decode().index("{") :])[0]
        assert report["restored"]
        if finish in ("input", "crlf", "paste"):
            assert report["match"]
        else:
            assert report["status"] == "cancelled"
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


@pytest.mark.parametrize("point", ["before_reload", "reload_unknown", "after_verified_reload"])
def test_resume_failure_receipt_separates_owned_cleanup_from_unknown_original_live_ssh(
    monkeypatch, tmp_path, point
):
    leaf = tmp_path / "deny.conf"
    leaf.write_bytes(installer.SSH_DENY_BYTES)
    monkeypatch.setattr(installer, "SSH_DENY", leaf)
    failure = (
        "input_token"
        if point == "before_reload"
        else "publish"
        if point == "after_verified_reload"
        else None
    )
    b, _t = budget_transaction(failure)
    b.continue_pinned_ssh = True
    b.ssh_preexisting = True
    b.rollback = installer.NativeBootstrap.rollback.__get__(b)
    monkeypatch.setattr(installer.time, "monotonic", lambda: 100)
    monkeypatch.setattr(installer.signal, "setitimer", lambda *a: None)
    b.ssh_hashes = {"public": "HASH"}
    b.ssh_before = {u: ssh_baseline() for u in (installer.ACCOUNT, "morris")}
    monkeypatch.setattr(b, "verify_recovery_pin", lambda: None)
    monkeypatch.setattr(b, "ssh_source", lambda: b.ssh_hashes)
    monkeypatch.setattr(b, "effective_ssh", lambda user: b.ssh_before[user])
    monkeypatch.setattr(b, "verify_ssh_reload", lambda: None)

    def apply():
        b.ssh_reload_attempted = True
        if point == "reload_unknown":
            raise entry.Denied("native_failed")
        b.ssh_reload_verified = True

    b.apply_ssh_deny = apply
    out = installer.initialize(b)
    assert out["ssh_preexisting_retained"] and out["owned_artifact_rollback_verified"]
    assert out["ssh_reload_attempted"] == (point != "before_reload")
    assert out["ssh_reload_verified"] == (point == "after_verified_reload")
    assert out["rollback_verified"] == (point == "before_reload")
    assert out["manual_recovery_required"] == (point != "before_reload")
    assert out["original_live_ssh_state_restored"] is False
    assert leaf.read_bytes() == installer.SSH_DENY_BYTES


def test_recovery_builder_requires_explicit_mode_and_pins_correct_entry(tmp_path):
    builder = module("build_ops_review")
    with pytest.raises(TypeError):
        builder.build(tmp_path / "ambiguous-default")
    with pytest.raises(ValueError, match="review_mode_unverified"):
        builder.build(tmp_path / "wrong-mode", mode="--apply-with-ssh-deny")
    assert list(tmp_path.iterdir()) == []
    result = builder.build(tmp_path / "recovery-review", mode="--diagnose-retained-inspect")
    text = (tmp_path / "recovery-review/ops-bootstrap-once.sh").read_text()
    assert "--diagnose-retained-inspect" in text and "--continue-pinned-ssh" not in text
    assert builder.REMOTE in text
    assert result["entry_mode"] == "--diagnose-retained-inspect" and result["uploaded"] is False


def test_resume_cleanup_revokes_only_new_helper_and_locks_new_account_keeps_old_rule(
    monkeypatch, tmp_path
):
    import hashlib

    leaf = tmp_path / "existing.conf"
    leaf.write_bytes(installer.SSH_DENY_BYTES)
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    helper = helpers / "api-quota-broker-control"
    helper.write_bytes(b"PUBLIC_NEW_HELPER")
    monkeypatch.setattr(installer, "SSH_DENY", leaf)
    monkeypatch.setattr(installer, "LIBEXEC", helpers)

    def read(path, *, sha):
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == sha
        return raw

    b = installer.NativeBootstrap(
        {}, SimpleNamespace(read_root=read), ssh_change_authorized=True, continue_pinned_ssh=True
    )
    b.created = True
    b.uid = 996
    b.gid = 997
    b.ssh_preexisting = True
    b.ssh_reload_attempted = True
    b.published[helper] = hashlib.sha256(helper.read_bytes()).hexdigest()
    monkeypatch.setattr(b, "account_identity", lambda: (996, 997))
    monkeypatch.setattr(b, "verify_recovery_pin", lambda: None)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv == ("/usr/bin/passwd", "--status", installer.ACCOUNT):
            return b"broker-deploy L\n"
        if argv == ("/usr/bin/chage", "--list", "--iso8601", installer.ACCOUNT):
            return b"Account expires : 1970-01-02\n"
        assert argv == installer.LOCK
        return b""

    monkeypatch.setattr(b, "run", run)
    assert b.rollback() is False and b.owned_rollback_verified
    assert (
        installer.LOCK in calls
        and not helper.exists()
        and leaf.read_bytes() == installer.SSH_DENY_BYTES
    )
