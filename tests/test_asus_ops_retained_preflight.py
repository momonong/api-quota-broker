"""Whole retained preflight in a public virtual host; no real root/native/network calls."""

import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


current = load("retained_preflight_current", ROOT / "deploy/asus/install_ops.py")
sealed = load(
    "retained_preflight_sealed",
    ROOT / "tests/fixtures/asus-history/r12/install_ops.py",
)
entry = load("retained_preflight_entry", ROOT / "deploy/asus/ops_entry.py")
PRIVATE = "PUBLIC_PRIVATE_FAILURE_FIXTURE_DO_NOT_PROJECT"


def virtual_host(monkeypatch, mod, *, agent=False):
    events = []
    gateway = b"PUBLIC_GATEWAY_FIXTURE"
    old_dir = ROOT / "tests/fixtures/asus-history/r11"
    files = {
        name: (old_dir / name).read_bytes() for name in ("ops_entry.py", "broker_ops_policy.py")
    }
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    raw = {
        "/etc/ssh/sshd_config": b"Include /etc/ssh/sshd_config.d/*.conf\n",
        "/etc/ssh/sshd_config.d/50-cloud-init.conf": b"# PUBLIC fixture\n",
        str(mod.SSH_DENY): mod.SSH_DENY_BYTES,
        "/etc/default/ssh": b"SSHD_OPTS=\n",
        "/etc/api-quota-broker/gateway.json": gateway,
        str(mod.BASE / "manifest.json"): json.dumps(
            {"schema": 1, "files": hashes}, sort_keys=True
        ).encode()
        + b"\n",
        str(mod.CONFIG / "policy.json"): json.dumps(
            {
                "schema": 1,
                "config_sha256": hashlib.sha256(gateway).hexdigest(),
                "ops_uid": 994,
                "ops_gid": 981,
                "peer_uid": 1000,
                "scope_verification": "human_dashboard_attestation_only",
                "enabled": False,
                "issued_at": "2026-10-06T09:32:35.390422+00:00",
                "expires_at": "2026-11-05T04:33:31+00:00",
            },
            sort_keys=True,
        ).encode()
        + b"\n",
    }
    raw.update({str(mod.BASE / name): data for name, data in files.items()})
    directories = {
        mod.BASE: {"ops_entry.py", "broker_ops_policy.py", "manifest.json"},
        mod.CONFIG: {"policy.json", "ops_doppler.cred"},
        mod.STATE: {"operation.lock"},
        mod.RUN: set(),
        mod.SSH_DENY.parent: {"05-api-quota-broker-ops-deny.conf", "50-cloud-init.conf"},
        mod.RECOVERY_RECEIPT.parent: {mod.RECOVERY_RECEIPT.name},
    }
    if agent:
        directories[mod.STATE].add("retained-inspect.claim.json")
        raw[str(mod.STATE / "retained-inspect.claim.json")] = (
            json.dumps(
                {
                    "schema": 1,
                    "operation": "inspect",
                    "dispatch_intent": True,
                    "automatic_retry": False,
                    "credential_reuse_selected": True,
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )
    created = []

    def read_root(path, **kw):
        name = str(path)
        assert "ops_doppler.cred" not in name and "credential.secret" not in name
        if name == str(mod.RECOVERY_RECEIPT):
            assert kw.get("sha") == mod.RECOVERY_RECEIPT_SHA
            return b"PUBLIC_PREPARED_RECEIPT_FIXTURE"
        data = raw.get(name, b"PUBLIC_SOURCE_FIXTURE")
        if "sha" in kw:
            assert hashlib.sha256(data).hexdigest() == kw["sha"]
        return data

    def metadata(path, *a, **kw):
        name = str(path)
        if path in directories or path in (mod.LIBEXEC,):
            mode = (
                0o700
                if path in (mod.STATE, mod.RECOVERY_RECEIPT.parent)
                else 0o750
                if path == mod.RUN
                else 0o755
            )
            kind = stat.S_IFDIR
            gid = 1000 if path == mod.RUN else 0
            size = 4096
        elif (
            name in raw
            or name in mod.RECOVERY_BINARIES
            or name == mod.RECOVERY_UNIT
            or name in mod.NATIVE_TOOL_BITS
            or path == mod.RECOVERY_RECEIPT
            or path in (mod.STATE / "operation.lock", mod.CONFIG / "ops_doppler.cred")
            or name == "/var/lib/systemd/credential.secret"
        ):
            kind = stat.S_IFREG
            mode = (
                0o600
                if path
                in (
                    mod.RECOVERY_RECEIPT,
                    mod.STATE / "operation.lock",
                    mod.STATE / "retained-inspect.claim.json",
                    mod.CONFIG / "ops_doppler.cred",
                )
                or name == "/var/lib/systemd/credential.secret"
                else 0o644
            )
            if name in mod.NATIVE_TOOL_BITS:
                mode = 0o755 | mod.NATIVE_TOOL_BITS[name]
            gid = 0
            size = (
                0
                if path == mod.STATE / "operation.lock"
                else len(raw.get(name, b"PUBLIC_SOURCE_FIXTURE"))
            )
        else:
            raise FileNotFoundError(2, "PUBLIC_ABSENT_FIXTURE")
        tick = (
            mod.RECOVERY_LEAF_TIME_NS
            if path in (mod.SSH_DENY, mod.SSH_DENY.parent)
            else mod.RECOVERY_MTIME_NS
            if path == mod.RECOVERY_RECEIPT
            else mod.RECOVERY_MTIME_NS - 1
        )
        return SimpleNamespace(
            st_mode=kind | mode,
            st_uid=0,
            st_gid=gid,
            st_nlink=1,
            st_dev=1,
            st_ino=2,
            st_size=size,
            st_mtime_ns=tick,
            st_ctime_ns=tick,
        )

    def iterdir(path):
        assert path in directories
        return iter(path / name for name in directories[path])

    def mkdir(path, **kw):
        assert path.parent == Path("/var/backups/api-quota-broker")
        assert path.name.startswith("ops-bootstrap-") and kw["mode"] == 0o700
        created.append(path)

    def write(path, data, **kw):
        assert path.parent in created and path.name.startswith("receipt-")
        value = json.loads(data)
        assert value["status"] == "prepared" and value["provider_calls"] == 0
        events.append("prepared_receipt")

    def account(name):
        if name == mod.ACCOUNT:
            return SimpleNamespace(
                pw_name=name,
                pw_uid=994,
                pw_gid=981,
                pw_dir="/nonexistent",
                pw_shell="/usr/sbin/nologin",
            )
        if name == "morris":
            return SimpleNamespace(pw_uid=1000, pw_gid=1000)
        if name == "api-quota-broker":
            return SimpleNamespace(pw_uid=995, pw_gid=982)
        raise KeyError

    def group(name):
        if name == mod.ACCOUNT:
            return SimpleNamespace(gr_name=name, gr_gid=981, gr_mem=[])
        raise KeyError

    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mod.os, "uname", lambda: SimpleNamespace(nodename="asus-ubuntu2604-server"))
    monkeypatch.setattr(mod.os, "isatty", lambda *a: True)
    monkeypatch.setattr(mod.os, "getgrouplist", lambda *a: [981])
    monkeypatch.setattr(mod.pwd, "getpwnam", account)
    monkeypatch.setattr(
        mod.pwd, "getpwall", lambda: [account(mod.ACCOUNT), account("api-quota-broker")]
    )
    monkeypatch.setattr(mod.grp, "getgrnam", group)
    monkeypatch.setattr(mod.grp, "getgrall", lambda: [group(mod.ACCOUNT)])
    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(Path, "stat", metadata)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "glob", lambda p, pattern: iter(p / n for n in directories[p]))
    monkeypatch.setattr(Path, "resolve", lambda p, **kw: p)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 100)
    monkeypatch.setattr(mod.signal, "setitimer", lambda *a: None)
    e = SimpleNamespace(
        Denied=entry.Denied,
        root_dir=lambda *a, **kw: None,
        read_root=read_root,
        strict_json=entry.strict_json,
        SERVICE=entry.SERVICE,
        service_state=lambda *a: {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "1",
            "NRestarts": "0",
            "ExecMainStartTimestampMonotonic": "1",
        },
        broker_pins=lambda sha: None,
        memory_guard=lambda **kw: events.append("memory_guard"),
        write_exclusive=write,
    )
    options = {"retained_diagnosis": True}
    if agent:
        options["retained_agent"] = True
    boot = mod.NativeBootstrap(
        {"broker_ops.sudoers.proposal": b"PUBLIC_SUDOERS_FIXTURE"}, e, **options
    )

    def run(argv, **kw):
        events.append(tuple(argv))
        assert not any(x in argv for x in ("reload", "restart", "start", "--unlock", "--lock"))
        if argv[:2] == ("/usr/bin/passwd", "--status"):
            return b"broker-deploy L PUBLIC\n"
        if argv[:2] == ("/usr/bin/chage", "--list"):
            return b"Account expires : 1970-01-02\n"
        if argv[:2] == ("/usr/sbin/sshd", "-T"):
            return b"denyusers broker-deploy\npasswordauthentication yes\nkbdinteractiveauthentication no\nhostbasedauthentication no\ngssapiauthentication no\nauthorizedkeyscommand none\ntrustedusercakeys none\nauthorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\nsshdsessionpath /usr/lib/openssh/sshd-session\nsshdauthpath /usr/lib/openssh/sshd-auth\n"
        if argv[:3] == ("/usr/bin/systemctl", "show", "ssh.service"):
            if "--property=ExecStart,Environment,EnvironmentFiles,DropInPaths,FragmentPath" in argv:
                return b"ExecStart={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D $SSHD_OPTS ; }\nEnvironment=\nEnvironmentFiles=/etc/default/ssh (ignore_errors=yes)\nDropInPaths=\nFragmentPath=/usr/lib/systemd/system/ssh.service\n"
            return b"CanReload=yes\nActiveState=active\nSubState=running\nMainPID=9\nExecReload={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; }\nExecReload={ path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; }\n"
        if argv[:2] == ("/usr/bin/systemctl", "show"):
            if argv[2] == mod.BOOT_UNIT:
                return (
                    f"MainPID={os.getpid()}\nExecMainStartTimestampMonotonic=100000000\n".encode()
                )
            if "--property=LoadState" in argv:
                return b"not-found\n"
            return b""
        if "--help" in argv:
            return b"--list --iso8601 encrypt --with-key= --name=\n"
        if argv[:2] == ("/usr/bin/systemd-creds", "--version"):
            return b"systemd 259\n"
        if argv[:2] == ("/usr/bin/sudo", "--version"):
            return b"sudo-rs 0.2.13\n"
        return b""

    boot.run = run
    return boot, events


def test_sealed_r12_real_preflight_order_reproduces_missing_baseline(monkeypatch):
    b, events = virtual_host(monkeypatch, sealed)
    assert not hasattr(b, "ssh_before")
    with pytest.raises(AttributeError, match="ssh_before"):
        b.preflight()
    assert b.checkpoint == "recovery_pin"
    assert b.checks_passed == ["identity", "artifact_absence", "parent_metadata"]
    assert not b.retained_claimed and not b.published and b.receipt_dir is None
    assert not any(isinstance(x, tuple) and x[:2] == ("/usr/sbin/sshd", "-T") for x in events)


def test_corrected_whole_retained_preflight_builds_baseline_before_consumption(monkeypatch):
    b, events = virtual_host(monkeypatch, current)
    assert not hasattr(b, "ssh_before")
    b.preflight()
    assert len(b.recovery_source_fingerprint) == 7
    assert b.retained_snapshot and b.recovery_step is None
    assert "receipt_storage" in b.checks_passed and "proposal_syntax" in b.checks_passed
    assert events[-1] == "prepared_receipt"
    assert not b.retained_claimed and not b.published and not b.creation_attempted
    assert b.ssh_before[current.ACCOUNT]["sshdsessionpath"] == "/usr/lib/openssh/sshd-session"


@pytest.mark.parametrize(
    "step,method",
    [
        ("pin", "verify_recovery_pin"),
        ("ssh_source", "ssh_source"),
        ("ssh_effective", "effective_ssh"),
        ("source_fingerprint", "recovery_sources"),
        ("ssh_reload_interface", "verify_ssh_reload"),
    ],
)
def test_programmer_error_gets_fixed_substep_without_raw_text(monkeypatch, step, method):
    b, _events = virtual_host(monkeypatch, current)

    def failure(*args, **kw):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(b, method, failure)

    def cleanup():
        b.recovery_step = "ssh_reload_interface"
        return True

    b.rollback = cleanup
    result = current.initialize(b)
    assert result["status"] == "blocked" and result["check"] == "recovery_pin"
    assert result["code"] == current.RECOVERY_STEPS[step] and result["recovery_step"] == step
    assert PRIVATE not in json.dumps(result) and not b.retained_claimed


@pytest.mark.parametrize(
    "step,method", [("ssh_source", "ssh_source"), ("ssh_effective", "effective_ssh")]
)
def test_wrong_return_type_stops_at_producer_with_fixed_code(monkeypatch, step, method):
    b, _events = virtual_host(monkeypatch, current)
    monkeypatch.setattr(b, method, lambda *a, **kw: None)
    with pytest.raises(current.Blocked, match=current.RECOVERY_STEPS[step]):
        b.prepare_retained_ssh()
    assert b.recovery_step == step and not b.retained_claimed


def test_missing_baseline_is_guarded_before_reading_sources(monkeypatch):
    b, events = virtual_host(monkeypatch, current)
    with pytest.raises(current.Blocked, match="recovery_dependency_unverified"):
        b.recovery_sources()
    assert events == []


def test_effective_helper_path_drift_still_preserves_existing_safe_predicate(monkeypatch):
    b, _events = virtual_host(monkeypatch, current)
    run = b.run

    def changed(argv, **kw):
        data = run(argv, **kw)
        if argv[:2] == ("/usr/sbin/sshd", "-T"):
            return data.replace(b"/usr/lib/openssh/sshd-auth", PRIVATE.encode())
        return data

    b.run = changed
    with pytest.raises(current.Blocked, match="recovery_source_changed"):
        b.prepare_retained_ssh()
    assert b.recovery_step == "source_fingerprint"
    assert b.recovery_diagnostic["predicate"] == "helper_effective_path"
    assert b.recovery_diagnostic["actual"] == "different"
    assert PRIVATE not in json.dumps(b.recovery_diagnostic)


def test_new_safe_substep_survives_standalone_root_projection():
    projector = load("preflight_projector", ROOT / "deploy/asus/project_ops_receipts.py")
    result = projector.safe_fields(
        {
            "status": "blocked",
            "check": "recovery_pin",
            "code": "recovery_fingerprint_unverified",
            "recovery_step": "source_fingerprint",
            "stderr": PRIVATE,
        }
    )
    assert result["recovery_step"] == "source_fingerprint"
    assert result["code"] == "recovery_fingerprint_unverified" and PRIVATE not in json.dumps(result)
    assert "recovery_step" not in projector.safe_fields({"recovery_step": PRIVATE})


def test_sealed_transaction_reproduces_native_generic_receipt_and_zero_claim(monkeypatch):
    b, events = virtual_host(monkeypatch, sealed)
    result = sealed.initialize(b)
    assert result["stage"] == "preflight" and result["check"] == "recovery_pin"
    assert result["code"] == "initialization_unverified"
    assert result["preflight_checks_passed"] == ["identity", "artifact_absence", "parent_metadata"]
    assert result["rollback_verified"] and not result["manual_recovery_required"]
    assert not result["attempt_claim_retained"] and not result["retained_contract_restored"]
    assert not b.published and not b.creation_attempted and b.receipt_dir is None
    assert "prepared_receipt" not in events


def test_agent_mode_whole_preflight_validates_existing_r13_claim_without_replay(monkeypatch):
    b, events = virtual_host(monkeypatch, current, agent=True)
    assert b.retained_agent
    b.preflight()
    assert "r13_claim" in b.retained_snapshot and "receipt_storage" in b.checks_passed
    assert not b.retained_claimed and not b.published and not b.creation_attempted
    assert events[-1] == "prepared_receipt"
