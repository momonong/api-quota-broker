"""Host-key initialization fixtures: no live root, credentials or systemd calls."""

import importlib.util
import json
import os
import subprocess
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


credentials = load("asus_credentials_fixture", "import_credentials.py")
installer = load("asus_credentials_installer_fixture", "install.py")
NOW = datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)
EXPIRY = NOW + timedelta(days=30)
TOKEN = "dp.st.dev." + "fixtureOnlyNeverLiveToken" * 3


@pytest.fixture
def case(tmp_path, monkeypatch):
    layout = installer.Layout(tmp_path)
    for path, mode in (
        (layout.config, 0o750),
        (layout.config / "credentials", 0o700),
        (layout.state, 0o700),
        (layout.path("/var/lib/systemd"), 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    monkeypatch.setattr(credentials, "_tool", lambda: installer)
    monkeypatch.setattr(credentials, "operator_gate", lambda: None)
    monkeypatch.setattr(installer, "service_owner", lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr(installer, "stopped_service", lambda: None)
    metadata = installer.metadata
    monkeypatch.setattr(
        installer,
        "metadata",
        lambda path, uid, mode, **kwargs: (
            True if path == Path("/usr/bin/systemd-creds") else metadata(path, uid, mode, **kwargs)
        ),
    )
    calls = []
    read = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:] == ["setup"]:
            key = layout.path("/var/lib/systemd/credential.secret")
            key.write_bytes(b"not-a-real-host-key")
            key.chmod(0o600)
        else:
            destination = Path(argv[-1])
            # Cipher fixtures intentionally do not preserve/derive any plaintext.
            destination.write_bytes(b"ciphertext-fixture")
            destination.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(credentials.subprocess, "run", command)

    def reader():
        read.append(1)
        return TOKEN

    return SimpleNamespace(layout=layout, calls=calls, read=read, reader=reader, command=command)


def initialize(case, **overrides):
    return credentials.initialize(
        EXPIRY,
        human_attested=True,
        token_reader=case.reader,
        layout=case.layout,
        now=NOW,
        root_uid=os.getuid(),
        **overrides,
    )


def test_default_plan_has_no_host_or_secret_effects(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["import_credentials.py"])
    for name in ("operator_gate", "_tool", "hidden_token", "initialize"):
        monkeypatch.setattr(credentials, name, lambda *a, **k: pytest.fail("unexpected effect"))
    assert credentials.main() == 0
    record = json.loads(capsys.readouterr().out)
    assert record["credential_policy"] == "host"
    assert record["remote_scope_verified"] is False
    assert record["maximum_token_days"] == 30


@pytest.mark.parametrize("fault", ["uid", "tty", "host", "cgroup", "swap", "core"])
def test_operator_gate_checks_before_any_token_or_key_generation(monkeypatch, fault):
    monkeypatch.setattr(credentials.os, "geteuid", lambda: 1 if fault == "uid" else 0)
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: fault != "tty"))
    monkeypatch.setattr(
        credentials.socket, "gethostname", lambda: "wrong" if fault == "host" else credentials.HOST
    )
    monkeypatch.setattr(
        credentials.resource, "getrlimit", lambda _: (0, 1) if fault == "core" else (0, 0)
    )

    def kernel(path, bound):
        if path == "/proc/self/cgroup":
            return b"0::/wrong" if fault == "cgroup" else credentials.EXPECTED_GROUP
        return b"max" if fault == "swap" else b"0"

    monkeypatch.setattr(credentials, "_kernel", kernel)
    with pytest.raises(credentials.CredentialError, match="^" + credentials.FAILURE + "$"):
        credentials.operator_gate()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"0::/system.slice/other.service\n",
        credentials.EXPECTED_GROUP + b"\n0::/other\n",
        credentials.EXPECTED_GROUP + b"\r\n",
        b"1:memory:/system.slice/api-quota-broker-install.service",
    ],
)
def test_exact_unified_operator_cgroup_required(monkeypatch, raw):
    monkeypatch.setattr(credentials.os, "geteuid", lambda: 0)
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(credentials.socket, "gethostname", lambda: credentials.HOST)
    monkeypatch.setattr(credentials.resource, "getrlimit", lambda _: (0, 0))
    monkeypatch.setattr(
        credentials, "_kernel", lambda path, _: raw if path == "/proc/self/cgroup" else b"0"
    )
    with pytest.raises(credentials.CredentialError):
        credentials.operator_gate()


def test_kernel_reads_are_bounded_and_reject_symlink(tmp_path):
    file = tmp_path / "kernel"
    file.write_bytes(b"0\n")
    assert credentials._kernel(str(file), 2) == b"0\n"
    with pytest.raises(credentials.CredentialError):
        credentials._kernel(str(file), 1)
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(OSError):
        credentials._kernel(str(link), 32)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-03",
        "2026-10-03T00:00:00+00:00",
        "2026-10-03T00:00:00Z\n",
        "2026-99-03T00:00:00Z",
        " 2026-10-03T00:00:00Z",
        1,
    ],
)
def test_expiry_requires_exact_utc(value):
    with pytest.raises(credentials.CredentialError):
        credentials.parse_utc(value)


@pytest.mark.parametrize(
    "expiry,attested",
    [
        (NOW, True),
        (NOW - timedelta(seconds=1), True),
        (EXPIRY + timedelta(seconds=1), True),
        (EXPIRY, False),
        (EXPIRY, 1),
        (EXPIRY.replace(tzinfo=None), True),
    ],
)
def test_expiry_and_attestation_boundaries(expiry, attested):
    with pytest.raises(credentials.CredentialError):
        credentials.metadata_record(expiry, human_attested=attested, now=NOW)


def test_success_atomic_set_independent_values_and_stdin_only(case):
    output = initialize(case)
    directory = case.layout.config / "credentials"
    assert case.read == [1]
    assert not list(case.layout.config.glob(".credential-init.*"))
    assert {p.name for p in directory.iterdir()} == {
        name + ".cred" for name in credentials.NAMES
    } | {credentials.METADATA, "import-evidence.json"}
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    commands = case.calls[1:]
    assert case.calls[0][0] == ["/usr/bin/systemd-creds", "setup"]
    assert len(commands) == 5
    values = []
    for name, (argv, kwargs) in zip(credentials.NAMES, commands, strict=True):
        assert argv[:3] == ["/usr/bin/systemd-creds", "--with-key=host", "--name=" + name]
        assert argv[-3:-1] == ["encrypt", "-"]
        assert (any(a.startswith("--not-after=") for a in argv)) == (
            name == "doppler_service_token"
        )
        assert kwargs["env"] == credentials.CLEAN_ENV
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["umask"] == 0o077
        assert TOKEN not in str(argv) + str(kwargs["env"])
        values.append(kwargs["input"])
    assert len(set(values)) == 5
    assert len(values[0]) == len(values[3]) == 32
    assert len(values[1]) >= 32 and len(values[2]) >= 32
    assert values[4] == TOKEN.encode()
    assert TOKEN not in json.dumps(output)
    record = credentials.validate_metadata(
        directory / credentials.METADATA, now=NOW, root_uid=os.getuid()
    )
    assert record == output["metadata"]
    assert record["remote_scope_verified"] is False
    assert record["created_at_source"] == "local_import_clock"
    assert record["approval_user_message_id"] == "01a0ffaa-84ae-7030-b193-a004ca233d8e"
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert case.read == [1]


@pytest.mark.parametrize("existing", ["cipher", "ledger", "wal", "unknown_state", "stage"])
def test_initial_only_never_overwrites_or_retries_existing(case, existing):
    targets = {
        "cipher": case.layout.config / "credentials/queue_key.cred",
        "ledger": case.layout.state / "ledger.sqlite3",
        "wal": case.layout.state / "ledger.sqlite3-wal",
        "unknown_state": case.layout.state / "other-private-state",
        "stage": case.layout.config / ".credential-init.previous",
    }
    target = targets[existing]
    target.write_bytes(b"preserved-fixture")
    target.chmod(0o600)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert target.read_bytes() == b"preserved-fixture"
    assert case.read == [] and case.calls == []


@pytest.mark.parametrize("fault", ["permission", "symlink"])
def test_unsafe_placeholder_rejected_before_setup(case, fault):
    directory = case.layout.config / "credentials"
    if fault == "permission":
        directory.chmod(0o755)
    else:
        directory.rmdir()
        directory.symlink_to(case.layout.state, target_is_directory=True)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert case.calls == [] and case.read == []


def test_host_key_check_only_metadata(case, monkeypatch):
    host = case.layout.path("/var/lib/systemd/credential.secret")
    host.write_bytes(b"fixture-host-key-must-not-read")
    host.chmod(0o400)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read host key"))
    monkeypatch.setattr(installer, "file_hash", lambda *a: pytest.fail("hash host key"))
    assert credentials.check_host_key(host, root_uid=os.getuid())
    initialize(case)
    assert all(argv[1:] != ["setup"] for argv, _ in case.calls)


@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink", "empty"])
def test_unsafe_existing_host_key_never_replaced(case, fault):
    host = case.layout.path("/var/lib/systemd/credential.secret")
    host.write_bytes(b"fixture")
    host.chmod(0o600)
    if fault == "mode":
        host.chmod(0o644)
    elif fault == "symlink":
        host.unlink()
        host.symlink_to(case.layout.state / "missing")
    elif fault == "hardlink":
        os.link(host, host.with_name("copy"))
    else:
        host.write_bytes(b"")
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert case.read == [] and case.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "dp.st.prd." + "a" * 40,
        TOKEN + "\n",
        " " + TOKEN,
        "dp.st.dev.short",
        TOKEN + "\x00",
        None,
    ],
)
def test_bad_token_never_reflected_or_encrypted(case, bad):
    case.reader = lambda: bad
    with pytest.raises(credentials.CredentialError) as error:
        initialize(case)
    assert str(error.value) == credentials.FAILURE
    assert len(case.calls) == 1  # host setup only
    assert not list((case.layout.config / "credentials").iterdir())
    evidence = next(case.layout.config.glob(".credential-init.*")) / "import-evidence.json"
    assert json.loads(evidence.read_text())["encrypted"] == []


def test_partial_encrypt_error_retains_private_cipher_evidence_no_reflection(case, monkeypatch):
    def failure(argv, **kwargs):
        if "--name=admin_token" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr=TOKEN.encode())
        return case.command(argv, **kwargs)

    monkeypatch.setattr(credentials.subprocess, "run", failure)
    with pytest.raises(credentials.CredentialError) as error:
        initialize(case)
    assert TOKEN not in str(error.value)
    assert list((case.layout.config / "credentials").iterdir()) == []
    stage = next(case.layout.config.glob(".credential-init.*"))
    assert stage.stat().st_mode & 0o777 == 0o700
    assert {p.name for p in stage.iterdir()} == {
        "digest_key.cred",
        "client_token.cred",
        "import-evidence.json",
    }
    evidence = json.loads((stage / "import-evidence.json").read_text())
    assert evidence["encrypted"] == ["digest_key", "client_token"]
    assert "no retry" in evidence["status"]
    assert TOKEN not in json.dumps(evidence)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert case.read == [1]


def test_late_ledger_creation_prevents_publication(case, monkeypatch):
    def command(argv, **kwargs):
        result = case.command(argv, **kwargs)
        if "--name=doppler_service_token" in argv:
            (case.layout.state / "ledger.sqlite3").write_bytes(b"unknown-must-preserve")
        return result

    monkeypatch.setattr(credentials.subprocess, "run", command)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert not list((case.layout.config / "credentials").iterdir())
    assert (case.layout.state / "ledger.sqlite3").read_bytes() == b"unknown-must-preserve"


def test_duplicate_random_keys_stop_before_encrypt(case, monkeypatch):
    monkeypatch.setattr(credentials.secrets, "token_bytes", lambda _: b"a" * 32)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert len(case.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_policy", "tpm2"),
        ("project", "other"),
        ("config", "prd"),
        ("access", "write"),
        ("human_dashboard_attested", 1),
        ("remote_scope_verified", 0),
        ("local_keys_expire", 0),
        ("schema_version", True),
        ("expires_at", "2026-10-03T00:00:00Z"),
    ],
)
def test_policy_metadata_is_strict_unexpired_and_truthful(case, field, value):
    record = credentials.metadata_record(EXPIRY, human_attested=True, now=NOW)
    record[field] = value
    path = case.layout.config / "credentials" / credentials.METADATA
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialError):
        credentials.validate_metadata(path, now=NOW, root_uid=os.getuid())


def test_interactive_api_prompt_order_and_safe_return(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials, "operator_gate", lambda: calls.append("gate"))
    answers = iter([credentials.stamp(EXPIRY), "ATTEST"])
    monkeypatch.setattr(
        "builtins.input", lambda prompt: calls.append("public_prompt") or next(answers)
    )

    def initialized(expiry, *, human_attested, installer=None, allow_host_key_setup=True):
        calls.append("initialize_hidden_token_once")
        assert expiry == EXPIRY and human_attested is True
        return {"phase": "credentials-initialized"}

    monkeypatch.setattr(credentials, "initialize", initialized)
    assert credentials.interactive_initialize(clock=lambda: NOW) == {
        "phase": "credentials-initialized"
    }
    assert calls == ["gate", "public_prompt", "public_prompt", "initialize_hidden_token_once"]


@pytest.mark.parametrize(
    "value,reason",
    [
        ("not-a-date", "expiry_invalid"),
        ("2026-10-03T03:36:17Z", "expiry_past"),
        ("2026-12-03T03:36:17Z", "expiry_over30days"),
    ],
)
def test_bad_expiry_retries_three_times_without_token_keys_or_stage(
    case, monkeypatch, capsys, value, reason
):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or value)
    monkeypatch.setattr(
        credentials, "initialize", lambda *a, **k: pytest.fail("initialized bad expiry")
    )
    monkeypatch.setattr(credentials.secrets, "token_bytes", lambda *a: pytest.fail("created key"))
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.interactive_initialize(
            clock=lambda: datetime(2026, 10, 3, 15, 8, 39, tzinfo=UTC)
        )
    assert caught.value.reason == reason and len(prompts) == 3
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["reason"] for row in reports] == [reason] * 3
    assert [row["attempts_remaining"] for row in reports] == [2, 1, 0]
    assert case.read == case.calls == []
    assert not list(case.layout.config.glob(".credential-init.*"))


def test_corrected_dashboard_expiry_after_past_input_reads_token_only_once(
    case, monkeypatch, capsys
):
    now = datetime(2026, 10, 3, 15, 8, 39, tzinfo=UTC)
    answers = iter(["2026-10-03T03:36:17Z", "2026-11-02T03:36:17Z", "ATTEST"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    original = credentials.initialize
    monkeypatch.setattr(
        credentials,
        "initialize",
        lambda expiry, human_attested, installer=None, allow_host_key_setup=True: original(
            expiry,
            human_attested=human_attested,
            installer=installer,
            token_reader=case.reader,
            layout=case.layout,
            now=now,
            root_uid=os.getuid(),
        ),
    )
    result = credentials.interactive_initialize(installer=installer, clock=lambda: now)
    assert result["metadata"]["expires_at"] == "2026-11-02T03:36:17Z"
    assert case.read == [1] and len(case.calls) == 6
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]["reason"] == "expiry_past"
    assert TOKEN not in json.dumps(result) + json.dumps(reports)


def test_initialize_rechecks_expiry_before_token_key_and_stage(case, monkeypatch):
    monkeypatch.setattr(credentials.secrets, "token_bytes", lambda *a: pytest.fail("created key"))
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.initialize(
            NOW,
            human_attested=True,
            now=NOW,
            layout=case.layout,
            token_reader=case.reader,
            root_uid=os.getuid(),
            installer=installer,
        )
    assert caught.value.reason == "expiry_past"
    assert case.read == case.calls == []
    assert not list(case.layout.config.glob(".credential-init.*"))


def test_pinned_installer_injection_never_loads_external_sibling(case, monkeypatch):
    monkeypatch.setattr(credentials, "_tool", lambda: pytest.fail("unreviewed sibling loader"))
    result = initialize(case, installer=installer)
    assert result["phase"] == "credentials-initialized" and case.read == [1]


def test_controlling_tty_missing_fails_without_echo_fallback(monkeypatch):
    monkeypatch.setattr(
        credentials.os, "open", lambda *a: (_ for _ in ()).throw(OSError("fixture"))
    )
    monkeypatch.setattr(credentials.getpass, "getpass", lambda *a, **k: pytest.fail("fallback"))
    with pytest.raises(credentials.CredentialError):
        credentials.hidden_token()


def test_getpass_noecho_failure_cannot_fallback(case, monkeypatch):
    file = case.layout.state / "tty-fixture"
    file.write_text("")
    real_open = os.open
    monkeypatch.setattr(credentials.os, "open", lambda path, flags: real_open(file, os.O_RDWR))
    monkeypatch.setattr(credentials.os, "isatty", lambda fd: True)

    def warning(*a, **k):
        warnings.warn("no echo available", credentials.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("warning failed to stop fallback")

    monkeypatch.setattr(credentials.getpass, "getpass", warning)
    with pytest.raises(credentials.CredentialError):
        credentials.hidden_token()


def test_broker_unit_disables_core_dumps():
    assert "\nLimitCORE=0\n" in (DEPLOY / "api-quota-broker.service").read_text()


def test_secret_reader_exception_is_content_free_and_stops_once(case):
    def reader():
        raise RuntimeError(TOKEN)

    case.reader = reader
    with pytest.raises(credentials.CredentialError) as error:
        initialize(case)
    assert str(error.value) == credentials.FAILURE
    assert len(case.calls) == 1
    assert not list((case.layout.config / "credentials").iterdir())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"token":"invalid-fixture",',
        b"\xff",
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
    ],
)
def test_malformed_metadata_errors_remain_safe(case, raw):
    path = case.layout.config / "credentials" / credentials.METADATA
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialError) as error:
        credentials.validate_metadata(path, now=NOW, root_uid=os.getuid())
    assert str(error.value) == credentials.FAILURE


def test_metadata_expires_even_when_original_receipt_was_valid(case):
    path = case.layout.config / "credentials" / credentials.METADATA
    path.write_text(json.dumps(credentials.metadata_record(EXPIRY, human_attested=True, now=NOW)))
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialError):
        credentials.validate_metadata(path, now=EXPIRY, root_uid=os.getuid())


def test_interactive_gate_failure_precedes_all_prompts(monkeypatch):
    monkeypatch.setattr(
        credentials, "operator_gate", lambda: (_ for _ in ()).throw(credentials.CredentialError())
    )
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompt before gate"))
    with pytest.raises(credentials.CredentialError):
        credentials.interactive_initialize()


def test_interactive_attestation_failure_precedes_token_setup(case, monkeypatch):
    answers = iter([credentials.stamp(EXPIRY), "NO"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    original = credentials.initialize
    monkeypatch.setattr(
        credentials,
        "initialize",
        lambda expiry, human_attested, installer=None, allow_host_key_setup=True: original(
            expiry,
            human_attested=human_attested,
            token_reader=case.reader,
            layout=case.layout,
            now=NOW,
            root_uid=os.getuid(),
        ),
    )
    with pytest.raises(credentials.CredentialError):
        credentials.interactive_initialize()
    assert case.read == [] and case.calls == []


@pytest.mark.parametrize("fault", ["empty", "oversize", "permission", "symlink", "hardlink"])
def test_encrypt_success_without_safe_cipher_file_never_publishes(case, monkeypatch, fault):
    def command(argv, **kwargs):
        result = case.command(argv, **kwargs)
        if "--name=digest_key" in argv:
            file = Path(argv[-1])
            if fault == "empty":
                file.write_bytes(b"")
            elif fault == "oversize":
                file.write_bytes(b"x" * 65537)
            elif fault == "permission":
                file.chmod(0o644)
            elif fault == "symlink":
                file.unlink()
                file.symlink_to(case.layout.state / "missing")
            else:
                os.link(file, file.with_name("unexpected-hardlink"))
        return result

    monkeypatch.setattr(credentials.subprocess, "run", command)
    with pytest.raises(credentials.CredentialError):
        initialize(case)
    assert not list((case.layout.config / "credentials").iterdir())
    assert len(case.calls) == 2  # setup and first encryption, no retry


def test_resume_without_host_key_stops_before_setup_or_secret(case):
    with pytest.raises(credentials.CredentialError) as caught:
        initialize(case, allow_host_key_setup=False)
    assert caught.value.operation == "host_key_check"
    assert case.read == [] and case.calls == []
    assert not list((case.layout.config / "credentials").iterdir())
    stages = list(case.layout.config.glob(".credential-init.*"))
    assert len(stages) == 1
    evidence = json.loads((stages[0] / "import-evidence.json").read_text())
    assert evidence["encrypted"] == []
    assert evidence["failure"]["operation"] == "host_key_check"


@pytest.mark.parametrize(
    "fault,reason,extra",
    [
        (OSError(13, TOKEN), "credential_os_error", {"errno": 13}),
        (
            subprocess.CalledProcessError(7, [TOKEN], stderr=TOKEN),
            "credential_tool_exit",
            {"exit_code": 7},
        ),
        (
            subprocess.TimeoutExpired([TOKEN], 20, output=TOKEN, stderr=TOKEN),
            "credential_tool_timeout",
            {},
        ),
        (EOFError(TOKEN), "credential_eof", {}),
        (KeyboardInterrupt(TOKEN), "credential_cancelled", {}),
    ],
)
def test_hidden_input_failures_leave_only_safe_operation_evidence(case, fault, reason, extra):
    def interrupted():
        case.read.append(1)
        raise fault

    case.reader = interrupted
    with pytest.raises(credentials.CredentialError) as caught:
        initialize(case)
    expected = {"reason": reason, "operation": "hidden_token", **extra}
    assert credentials.failure_details(caught.value) == expected
    assert case.read == [1] and len(case.calls) == 1
    evidence = json.loads(
        next(case.layout.config.glob(".credential-init.*"))
        .joinpath("import-evidence.json")
        .read_text()
    )
    assert evidence["failure"] == expected and evidence["encrypted"] == []
    assert TOKEN not in json.dumps(evidence) + str(caught.value)
    assert not list((case.layout.config / "credentials").iterdir())
