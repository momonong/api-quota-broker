"""Retained incident contract with real temporary files and public auth/service fixtures."""

import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
REVIEW = ROOT / "tests/fixtures/asus-history/r11"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


e = module("ops_entry")
i = module("install_ops")


@pytest.fixture
def retained(monkeypatch, tmp_path):
    # Root/account metadata is synthetic; actual files, O_EXCL, rename and fsync are real.
    for name in ("BASE", "CONFIG", "STATE", "RUN", "LIBEXEC", "SYSTEM"):
        path = tmp_path / name
        path.mkdir(mode={"STATE": 0o700, "RUN": 0o750}.get(name, 0o755))
        monkeypatch.setattr(i, name, path)
    monkeypatch.setattr(i, "SUDOERS", tmp_path / "sudoers")
    monkeypatch.setattr(i, "TMPFILES", tmp_path / "tmpfiles")
    monkeypatch.setattr(i, "SSH_DENY", tmp_path / "ssh-deny")
    i.SSH_DENY.write_bytes(i.SSH_DENY_BYTES)
    i.SSH_DENY.chmod(0o644)
    old = {n: (REVIEW / n).read_bytes() for n in ("ops_entry.py", "broker_ops_policy.py")}
    for n, raw in old.items():
        (i.BASE / n).write_bytes(raw)
        (i.BASE / n).chmod(0o644)
    manifest = {
        "schema": 1,
        "files": {n: hashlib.sha256(raw).hexdigest() for n, raw in old.items()},
    }
    (i.BASE / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (i.BASE / "manifest.json").chmod(0o644)
    source = {name: (ROOT / "deploy/asus" / name).read_bytes() for name in i.NAMES}
    b = i.NativeBootstrap(source, e, retained_diagnosis=True)
    b.uid, b.gid, b.config_sha = 994, 981, "0" * 64
    b.issued, b.expires = "2026-10-06T09:32:35.390422+00:00", "2026-11-05T04:33:31+00:00"
    (i.CONFIG / "policy.json").write_bytes(b.policy_bytes(enabled=False))
    (i.CONFIG / "policy.json").chmod(0o644)
    cipher = i.CONFIG / "ops_doppler.cred"
    cipher.write_bytes(b"PUBLIC_CIPHERTEXT_FIXTURE_NOT_A_TOKEN")
    cipher.chmod(0o600)
    (i.STATE / "operation.lock").write_bytes(b"")
    (i.STATE / "operation.lock").chmod(0o600)
    b.receipt_dir = tmp_path / "receipts"
    b.receipt_dir.mkdir(mode=0o700)
    original_lstat = Path.lstat

    def metadata(path, *args, **kw):
        v = original_lstat(path, *args, **kw)
        if path.is_relative_to(tmp_path):
            return SimpleNamespace(
                st_mode=v.st_mode,
                st_nlink=v.st_nlink,
                st_size=v.st_size,
                st_ino=v.st_ino,
                st_dev=v.st_dev,
                st_mtime_ns=v.st_mtime_ns,
                st_ctime_ns=v.st_ctime_ns,
                st_uid=0,
                st_gid=1000 if path == i.RUN else 0,
            )
        return v

    monkeypatch.setattr(Path, "lstat", metadata)
    # read_root's fd metadata must use the same synthetic root ownership.
    original_fstat = os.fstat

    def fd_metadata(fd):
        v = original_fstat(fd)
        return SimpleNamespace(
            st_mode=v.st_mode,
            st_nlink=v.st_nlink,
            st_size=v.st_size,
            st_ino=v.st_ino,
            st_dev=v.st_dev,
            st_mtime_ns=v.st_mtime_ns,
            st_ctime_ns=v.st_ctime_ns,
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(e.os, "fstat", fd_metadata)

    def root_dir(path, mode=None):
        v = Path(path).lstat()
        e.require(
            stat.S_ISDIR(v.st_mode) and v.st_uid == 0 and not v.st_mode & 0o022,
            "root_directory_untrusted",
        )
        e.require(mode is None or stat.S_IMODE(v.st_mode) == mode, "directory_mode_untrusted")

    monkeypatch.setattr(e, "root_dir", root_dir)
    # Any opening of ciphertext is a forbidden fixture failure, including hashing/decryption.
    original_open = os.open

    def guarded_open(path, *args, **kw):
        assert Path(path) != cipher, "ciphertext content must never be opened by bootstrap"
        return original_open(path, *args, **kw)

    monkeypatch.setattr(e.os, "open", guarded_open)
    b.account_identity = lambda: (994, 981)
    monkeypatch.setattr(
        i.pwd, "getpwall", lambda: [SimpleNamespace(pw_uid=994, pw_gid=981, pw_name=i.ACCOUNT)]
    )
    monkeypatch.setattr(i.grp, "getgrall", lambda: [SimpleNamespace(gr_gid=981, gr_name=i.ACCOUNT)])
    events = []
    account = {"locked": True}

    def run(argv, **kw):
        events.append(tuple(argv))
        if argv[0] == "/usr/sbin/usermod":
            account["locked"] = "--lock" in argv
        if argv[:2] == ("/usr/bin/passwd", "--status"):
            return ("broker-deploy " + ("L" if account["locked"] else "P") + " PUBLIC\n").encode()
        if argv[:2] == ("/usr/bin/chage", "--list"):
            return (
                "Account expires : " + ("1970-01-02" if account["locked"] else "never") + "\n"
            ).encode()
        return b""

    b.run = run
    b.deadline = 300
    monkeypatch.setattr(i.time, "monotonic", lambda: 100)
    monkeypatch.setattr(i.signal, "setitimer", lambda *a: None)
    b.verify_recovery_pin = lambda: e.read_root(
        i.SSH_DENY, sha=hashlib.sha256(i.SSH_DENY_BYTES).hexdigest(), mode=0o644
    )
    b.recovery_source_fingerprint = {"PUBLIC": "SOURCE"}
    b.recovery_sources = lambda: b.recovery_source_fingerprint
    b.preservation = lambda: b.verify_recovery_pin()
    b.native_checks = lambda: events.append(("PUBLIC_NATIVE_AUTH_CHECKS",))
    b.inspect_once = lambda: events.append(("PUBLIC_INSPECT_ONCE",))
    b.ssh_preexisting = True
    # Avoid native chown even in the fixture: production checks fstat after real writes.
    monkeypatch.setattr(e.os, "fchown", lambda *a: None)
    b.retained_snapshot = b.retained_state()
    originals = {p: p.read_bytes() for p in i.BASE.iterdir()}
    originals[i.CONFIG / "policy.json"] = (i.CONFIG / "policy.json").read_bytes()
    return b, events, account, originals


def test_retained_diagnosis_reuses_credential_restores_public_bytes_and_locks(retained):
    b, events, account, originals = retained
    public_before = {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in (i.BASE / "ops_entry.py", i.BASE / "manifest.json")
    }
    result = i.diagnose_retained(b)
    assert all(
        (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns) != before
        for p, before in public_before.items()
    )
    assert result["status"] == "passed" and result["diagnosis_only"]
    assert result["retained_contract_restored"] and account["locked"]
    assert result["public_bytes_restored"] and result["public_metadata_restored"] is False
    assert result["cipher_metadata_preserved"] and result["policy_bytes_preserved"]
    assert result["attempt_claim_retained"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert (i.STATE / "retained-inspect.claim.json").is_file()
    assert events.count(("PUBLIC_INSPECT_ONCE",)) == 1
    assert not any(
        "reload" in c or "restart" in c or "encrypt" in c or "--system" in c for c in events
    )
    assert not any(path.exists() for path in b.published)
    assert not list(i.BASE.glob("*.diagnosis-stage"))
    with pytest.raises(i.Blocked, match="retained_claims_present"):
        b.retained_state()


@pytest.mark.parametrize("point", ["publish", "unlock", "auth", "inspect"])
def test_failure_revokes_only_owned_files_and_preserves_history(retained, point):
    b, events, account, originals = retained

    def failed():
        raise e.Denied("core_limit")

    if point == "publish":
        original = b.replace_retained

        raised = []

        def replace(path, raw, **kw):
            original(path, raw, **kw)
            if path.name == "ops_entry.py" and not raised:
                raised.append(True)
                failed()

        b.replace_retained = replace
    elif point == "unlock":
        original = b.run

        def run(argv, **kw):
            output = original(argv, **kw)
            if "--unlock" in argv:
                failed()
            return output

        b.run = run
    elif point == "auth":
        b.native_checks = failed
    else:
        b.inspect_once = failed
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["rollback_verified"]
    assert result["retained_contract_restored"] and not result["manual_recovery_required"]
    assert account["locked"] and all(path.read_bytes() == raw for path, raw in originals.items())
    assert (i.STATE / "retained-inspect.claim.json").exists()
    assert not any("reload" in c or "restart" in c or "encrypt" in c for c in events)
    assert events.count(("PUBLIC_INSPECT_ONCE",)) <= 1


@pytest.mark.parametrize(
    "drift",
    [
        "unknown_claim",
        "policy_enabled",
        "cipher_mode",
        "cipher_symlink",
        "lock_hardlink",
        "base_mode",
        "run_gid",
        "module",
        "uid",
    ],
)
def test_retained_unknown_state_rejected_before_claim_or_unlock(retained, monkeypatch, drift):
    b, events, account, _originals = retained
    if drift == "unknown_claim":
        (i.STATE / "unknown.claim.json").write_text("PUBLIC")
    elif drift == "policy_enabled":
        (i.CONFIG / "policy.json").write_bytes(b.policy_bytes(enabled=True))
    elif drift == "cipher_mode":
        (i.CONFIG / "ops_doppler.cred").chmod(0o644)
    elif drift == "cipher_symlink":
        (i.CONFIG / "ops_doppler.cred").unlink()
        (i.CONFIG / "ops_doppler.cred").symlink_to(i.STATE / "operation.lock")
    elif drift == "lock_hardlink":
        os.link(i.STATE / "operation.lock", i.RUN / "PUBLIC_LINK")
    elif drift == "base_mode":
        i.BASE.chmod(0o775)
    elif drift == "run_gid":
        previous = Path.lstat

        def lstat(path, *a, **kw):
            v = previous(path, *a, **kw)
            if path == i.RUN:
                v.st_gid = 1001
            return v

        monkeypatch.setattr(Path, "lstat", lstat)
    elif drift == "module":
        (i.BASE / "ops_entry.py").write_bytes(b"PUBLIC_CHANGED_MODULE")
    else:
        b.account_identity = lambda: (993, 981)
    with pytest.raises((i.Blocked, e.Denied)):
        i.diagnose_retained(b)
    assert account["locked"] and not (i.STATE / "retained-inspect.claim.json").exists()
    assert not b.retained_updated and not b.published
    assert not any("--unlock" in c for c in events)


def test_policy_drift_after_publication_blocks_unlock_and_stays_manual(retained):
    b, events, account, originals = retained
    original = b.publish_retained

    def publish():
        original()
        (i.CONFIG / "policy.json").write_bytes(b.policy_bytes(enabled=True))

    b.publish_retained = publish
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["manual_recovery_required"]
    assert not result["retained_contract_restored"] and account["locked"]
    assert not any("--unlock" in c for c in events)
    assert json.loads((i.CONFIG / "policy.json").read_bytes())["enabled"] is True
    assert all(path.read_bytes() == raw for path, raw in originals.items() if path.parent == i.BASE)


def test_drifted_new_module_not_overwritten_during_cleanup(retained):
    b, _events, account, _originals = retained

    def inspect():
        (i.BASE / "ops_entry.py").write_bytes(b"PUBLIC_OTHER_ROOT_WORK")

    b.inspect_once = inspect
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["manual_recovery_required"]
    assert account["locked"] and (i.BASE / "ops_entry.py").read_bytes() == b"PUBLIC_OTHER_ROOT_WORK"
    assert not any(path.exists() for path in b.published)


def test_retained_account_lock_expiry_and_duplicate_identity_checks(retained, monkeypatch):
    b, _events, account, _originals = retained
    account["locked"] = False
    with pytest.raises(i.Blocked, match="retained_account_unverified"):
        b.verify_retained_account()
    account["locked"] = True
    monkeypatch.setattr(
        i.pwd, "getpwall", lambda: [SimpleNamespace(pw_uid=994, pw_gid=981, pw_name="other")]
    )
    with pytest.raises(i.Blocked, match="retained_account_unverified"):
        b.verify_retained_account()


def test_budget_rejects_before_claim_and_account_change(retained, monkeypatch):
    b, _events, account, _originals = retained
    monkeypatch.setattr(i.time, "monotonic", lambda: 121)
    with pytest.raises(i.Blocked, match="bootstrap_budget_insufficient"):
        i.diagnose_retained(b)
    assert not (i.STATE / "retained-inspect.claim.json").exists() and account["locked"]


def test_persistent_public_restore_failure_is_manual_and_claim_not_replayed(retained):
    b, events, account, _originals = retained
    original = b.replace_retained

    def replace(path, raw, **kw):
        original(path, raw, **kw)
        if path.name == "ops_entry.py":
            raise e.Denied("core_limit")

    b.replace_retained = replace
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["manual_recovery_required"]
    assert not result["retained_contract_restored"] and account["locked"]
    claim = (i.STATE / "retained-inspect.claim.json").read_bytes()
    with pytest.raises(i.Blocked, match="retained_claims_present"):
        i.diagnose_retained(b)
    assert (i.STATE / "retained-inspect.claim.json").read_bytes() == claim
    assert events.count(("PUBLIC_INSPECT_ONCE",)) == 0


@pytest.mark.parametrize("field,value", [("schema", True), ("enabled", 0), ("ops_uid", 994.0)])
def test_retained_policy_type_confusion_is_rejected(retained, field, value):
    b, _events, _account, _originals = retained
    data = json.loads((i.CONFIG / "policy.json").read_bytes())
    data[field] = value
    (i.CONFIG / "policy.json").write_text(json.dumps(data, sort_keys=True) + "\n")
    with pytest.raises(i.Blocked, match="retained_policy_unverified"):
        b.retained_state()


def test_existing_claim_drift_preserved_during_cleanup(retained):
    b, _events, account, _originals = retained

    def inspect():
        (i.STATE / "retained-inspect.claim.json").write_bytes(b"PUBLIC_OTHER_ROOT_WORK")

    b.inspect_once = inspect
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["manual_recovery_required"] and not result["retained_contract_restored"]
    assert account["locked"]
    assert (i.STATE / "retained-inspect.claim.json").read_bytes() == b"PUBLIC_OTHER_ROOT_WORK"


@pytest.mark.parametrize("drift", ["bytes", "mode"])
def test_restore_return_is_not_proof_without_final_readback(retained, drift):
    b, _events, account, originals = retained
    original = b.replace_retained

    def replace(path, raw, **kw):
        original(path, raw, **kw)
        if path.name == "ops_entry.py" and raw == originals[path]:
            if drift == "bytes":
                path.write_bytes(b"PUBLIC_OTHER_ROOT_WRITE_AFTER_RESTORE")
            else:
                path.chmod(0o600)

    b.replace_retained = replace
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["manual_recovery_required"] and not result["retained_contract_restored"]
    assert result["public_metadata_restored"] is False and account["locked"]
    assert not any(path.exists() for path in b.published)


@pytest.mark.parametrize("kind", ["dangling", "regular", "worker", "enable_link"])
def test_cleanup_final_gate_rejects_residue_after_access_revocation(retained, monkeypatch, kind):
    b, _events, account, originals = retained
    original = b.replace_retained
    residue = i.LIBEXEC / "api-quota-broker-control"
    injected = []

    def replace(path, raw, **kw):
        original(path, raw, **kw)
        if path.name == "ops_entry.py" and raw == originals[path] and not injected:
            injected.append(True)
            if kind == "dangling":
                residue.symlink_to(i.RUN / "PUBLIC_MISSING_TARGET")
            elif kind == "regular":
                residue.write_bytes(b"PUBLIC_OTHER_ROOT_WORK")
            elif kind == "enable_link":
                parent = i.SYSTEM / "sockets.target.wants"
                parent.mkdir()
                (parent / "api-quota-broker-ops.socket").symlink_to(i.RUN / "PUBLIC_MISSING_TARGET")

    b.replace_retained = replace
    run = b.run

    def with_worker(argv, **kw):
        result = run(argv, **kw)
        if kind == "worker" and argv[:2] == ("/usr/bin/systemctl", "list-units"):
            return b"PUBLIC_WORKER_ROW_DO_NOT_PROJECT\n"
        return result

    b.run = with_worker
    monkeypatch.setattr(i.time, "sleep", lambda *a: None)
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["manual_recovery_required"] and not result["retained_contract_restored"]
    assert account["locked"] and "PUBLIC_WORKER_ROW" not in json.dumps(result)
    if kind == "dangling":
        assert residue.is_symlink() and not residue.exists()
    elif kind == "regular":
        assert residue.read_bytes() == b"PUBLIC_OTHER_ROOT_WORK"
    elif kind == "enable_link":
        assert (i.SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket").is_symlink()


def test_dangling_new_access_file_is_not_skipped_by_generic_rollback(retained):
    b, _events, account, _originals = retained
    b.claim_retained()
    b.publish_retained()
    target = i.LIBEXEC / "api-quota-broker-control"
    target.unlink()
    target.symlink_to(i.RUN / "PUBLIC_MISSING_TARGET")
    assert b.rollback() is False
    assert target.is_symlink() and not target.exists() and account["locked"]


def test_socket_start_intent_never_proves_credential_consumption(retained):
    b, _events, _account, _originals = retained

    def inspect():
        b.socket_started = True
        raise e.Denied("native_exit", rc=1)

    b.inspect_once = inspect
    b.preflight = lambda: None
    b.guarded = True
    result = i.initialize(b)
    assert result["status"] == "blocked" and result["credential_reuse_selected"]
    assert result["credential_consumption_unverified"] and "credential_reused" not in result
    claim = json.loads((i.STATE / "retained-inspect.claim.json").read_bytes())
    assert claim["credential_reuse_selected"] and "credential_reused" not in claim
