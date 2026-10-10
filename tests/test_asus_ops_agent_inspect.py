"""Persistent inspect repair uses old history and a new intent, never replays diagnostics."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_asus_ops_retained import e, i
from test_asus_ops_retained import (
    retained as retained_host_fixture,  # noqa: F401 - fixture registration
)


@pytest.fixture
def agent(retained_host_fixture, monkeypatch):  # noqa: F811 - pytest fixture injection
    b, events, account, originals = retained_host_fixture
    raw = (
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
    old = i.STATE / "retained-inspect.claim.json"
    old.write_bytes(raw)
    old.chmod(0o600)
    old_meta = old.lstat()
    before = (old_meta.st_dev, old_meta.st_ino, old_meta.st_mtime_ns, old_meta.st_ctime_ns)
    b.retained_agent = True
    b.retained_snapshot = b.retained_state()
    base_run = b.run
    account["expiry"] = "1970-01-02"

    def run(argv, **kw):
        result = base_run(argv, **kw)
        if argv[0] == "/usr/sbin/usermod":
            account["expiry"] = (
                "1970-01-02" if "--lock" in argv else argv[argv.index("--expiredate") + 1]
            )
        if argv[:2] == ("/usr/bin/chage", "--list"):
            return ("Account expires : " + account["expiry"] + "\n").encode()
        if argv[:2] == ("/usr/bin/systemctl", "enable"):
            parent = i.SYSTEM / "sockets.target.wants"
            parent.mkdir(exist_ok=True)
            (parent / "api-quota-broker-ops.socket").symlink_to(
                i.SYSTEM / "api-quota-broker-ops.socket"
            )
        if argv[:2] == ("/usr/bin/systemctl", "disable"):
            link = i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket"
            if not i.path_absent(link):
                link.unlink()
        if argv[:2] == ("/usr/bin/systemctl", "is-active"):
            return b"active\n"
        return result

    b.run = run
    monkeypatch.setattr(e, "CONFIG", i.CONFIG)
    monkeypatch.setattr(
        e.pwd,
        "getpwnam",
        lambda n: SimpleNamespace(
            pw_uid=994, pw_gid=981, pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin"
        ),
    )
    monkeypatch.setattr(e.os, "getgrouplist", lambda *a: [981])
    b.verify_ssh = lambda: events.append(("PUBLIC_SSH_DENY_VERIFY",))
    b.verify_jobs = lambda: events.append(("PUBLIC_JOB_ABSENCE",))
    b.preflight = lambda: None
    b.guarded = True
    return b, events, account, originals, raw, before


def test_success_keeps_agent_entry_with_original_expiry_and_no_restart(agent):
    b, events, account, _originals, raw, before = agent
    result = i.initialize(b)
    assert result["status"] == "passed" and result["agent_inspect_ready"]
    assert result["operations"] == ["inspect"] and not result["restart_enabled"]
    assert result["agent_password_prompt"] is False and not result["diagnosis_only"]
    assert account["locked"] is False and account["expiry"] == "2026-11-05"
    assert result["policy_expiry_not_extended"] and result["os_account_expiry_bounded"]
    assert result["account_expiry_date"] == "2026-11-05"
    assert result["activation_claim_retained"] and result["r13_claim_preserved"]
    assert json.loads((i.CONFIG / "policy.json").read_bytes())["enabled"] is False
    old = i.STATE / "retained-inspect.claim.json"
    meta = old.lstat()
    assert (
        old.read_bytes() == raw
        and (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns) == before
    )
    assert (i.STATE / "agent-inspect-activation.claim.json").is_file()
    assert all(path.exists() for path in b.published)
    assert events.count(("PUBLIC_INSPECT_ONCE",)) == 1
    assert not any("--lock" in argv or "reload" in argv or "restart" in argv for argv in events)
    assert (i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket").is_symlink()
    with pytest.raises(i.Blocked, match="retained_claims_present"):
        b.retained_state()


@pytest.mark.parametrize("point", ["publish", "unlock", "inspect", "enable", "post_enable"])
def test_failure_locks_revokes_and_restores_without_deleting_r13_history(agent, point):
    b, events, account, originals, raw, before = agent

    def failure(*a, **kw):
        raise e.Denied("core_limit_set_failed")

    if point == "publish":
        original = b.publish_retained

        def publish():
            original()
            failure()

        b.publish_retained = publish
    elif point == "unlock":
        original = b.run

        def run(argv, **kw):
            result = original(argv, **kw)
            if "--unlock" in argv:
                failure()
            return result

        b.run = run
    elif point == "inspect":
        b.inspect_once = failure
    elif point == "enable":
        original = b.run

        def run(argv, **kw):
            if argv[:2] == ("/usr/bin/systemctl", "enable"):
                failure()
            return original(argv, **kw)

        b.run = run
    else:
        original = b.enable_agent_inspect

        def enable():
            original()
            failure()

        b.enable_agent_inspect = enable
    result = i.initialize(b)
    assert result["status"] == "blocked" and not result["agent_inspect_ready"]
    assert result["rollback_verified"] and result["retained_contract_restored"]
    assert result["diagnosis_only"] is False and not result["manual_recovery_required"]
    assert account["locked"] and account["expiry"] == "1970-01-02"
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not any(path.exists() for path in b.published)
    old = i.STATE / "retained-inspect.claim.json"
    meta = old.lstat()
    assert (
        old.read_bytes() == raw
        and (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns) == before
    )
    assert (i.STATE / "agent-inspect-activation.claim.json").exists()
    assert i.path_absent(i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket")
    assert events.count(("PUBLIC_INSPECT_ONCE",)) <= 1


@pytest.mark.parametrize("bad", ["unknown_claim", "old_claim_changed", "already_activation"])
def test_unknown_history_or_existing_activation_never_replayed(agent, bad):
    b, events, account, _originals, _raw, _before = agent
    if bad == "unknown_claim":
        (i.STATE / "other.claim.json").write_text("PUBLIC")
    elif bad == "old_claim_changed":
        (i.STATE / "retained-inspect.claim.json").write_bytes(b"PUBLIC_OTHER_ROOT_HISTORY")
    else:
        (i.STATE / "agent-inspect-activation.claim.json").write_bytes(b"PUBLIC_PENDING")
    result = i.initialize(b)
    assert result["status"] == "blocked" and not b.retained_claimed
    assert account["locked"] and not any("--unlock" in row for row in events)
    assert events.count(("PUBLIC_INSPECT_ONCE",)) == 0


def test_os_expiry_never_is_rejected_for_agent_mode(agent):
    b, _events, account, _originals, _raw, _before = agent
    account["expiry"] = "never"
    with pytest.raises(i.Blocked, match="account_expiry_active"):
        b.verify_active_account_expiry()


def test_new_builder_mode_is_separate_from_preserved_diagnostic_mode(tmp_path):
    import importlib.util

    path = Path(__file__).parents[1] / "deploy/asus/build_ops_review.py"
    spec = importlib.util.spec_from_file_location("agent_review_builder", path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    r = builder.build(tmp_path / "agent", mode="--restore-agent-inspect")
    wrapper = (tmp_path / "agent/ops-bootstrap-once.sh").read_text()
    assert r["remote_directory"] == builder.AGENT_REMOTE and r["uploaded"] is False
    assert "--restore-agent-inspect" in wrapper and "--diagnose-retained-inspect" not in wrapper
    assert "--apply-with-ssh-deny" not in wrapper and builder.REMOTE not in wrapper


def test_drifted_enable_link_is_preserved_and_never_disabled(agent):
    b, events, account, _originals, raw, _before = agent
    original = b.enable_agent_inspect

    def enable():
        original()
        link = i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket"
        link.unlink()
        other = i.SYSTEM / "PUBLIC_OTHER_UNIT"
        other.write_bytes(b"PUBLIC_OTHER_ROOT_WORK")
        link.symlink_to(other)
        raise e.Denied("core_limit_set_failed")

    b.enable_agent_inspect = enable
    result = i.initialize(b)
    assert result["manual_recovery_required"] and not result["agent_inspect_ready"]
    link = i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket"
    assert link.is_symlink() and link.resolve().name == "PUBLIC_OTHER_UNIT"
    assert not any(row[:2] == ("/usr/bin/systemctl", "disable") for row in events)
    assert account["locked"] and (i.STATE / "retained-inspect.claim.json").read_bytes() == raw


def test_final_public_entry_readback_detects_drift_after_inspect(agent):
    b, _events, account, _originals, raw, _before = agent

    def inspect():
        (i.LIBEXEC / "api-quota-broker-control").write_bytes(b"PUBLIC_OTHER_ROOT_WORK")

    b.inspect_once = inspect
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["manual_recovery_required"]
    assert account["locked"] and not result["agent_inspect_ready"]
    assert (i.LIBEXEC / "api-quota-broker-control").read_bytes() == b"PUBLIC_OTHER_ROOT_WORK"
    assert (i.STATE / "retained-inspect.claim.json").read_bytes() == raw
