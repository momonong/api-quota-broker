"""Fixed prerequisite packet validation with real temporary files; no root/SSH."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def historical(path):
    # The fixed October-12 packet must not follow later release-builder additions.
    raw = (
        (ROOT / "tests/fixtures/asus-history/release-verifier-pdf.py").read_bytes()
        if path == "scripts/build_asus_release.py"
        else (ROOT / path).read_bytes()
    )
    if path.endswith(".service"):
        return (
            raw.replace(b"    --google-pdf-count-tokens --serialize-work \\\n", b"")
            .replace(b"--max-media-input-bytes 8388608", b"--max-media-input-bytes 1048576")
            .replace(b"--max-request-bytes 16777216", b"--max-request-bytes 2097152")
            .replace(b"MemoryHigh=384M", b"MemoryHigh=256M")
            .replace(b"MemoryMax=512M", b"MemoryMax=384M")
        )
    return raw.replace(b'    "src/quota_broker/token_preflight.py",\n', b"")


@pytest.fixture
def packet(tmp_path, monkeypatch):
    module = load("prepare_pdf_maintenance")
    base, state, backup, system = (
        tmp_path / name for name in ("base", "state", "backup", "system")
    )
    for directory in (base, state, backup, system):
        directory.mkdir(mode=0o700)
    old = {
        "broker-v1.service": historical("deploy/asus/api-quota-broker-v1.service"),
        "release_verifier.py": historical("scripts/build_asus_release.py"),
    }
    assert all(module.digest(raw) == module.PINS[name] for name, raw in old.items())
    manifest = {
        "schema": 2,
        "files": {name: module.digest(raw) for name, raw in old.items()},
        "units": {},
    }
    old["manifest.json"] = module.wire(manifest)
    for name, raw in old.items():
        (base / name).write_bytes(raw)
        (base / name).chmod(0o644)
    (state / "operation.lock").touch(mode=0o600)
    current = tmp_path / "current"
    current.symlink_to(module.EXPECTED_RELEASE)
    monkeypatch.setattr(module, "BASE", base)
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "BACKUP", backup / "once")
    monkeypatch.setattr(module, "CURRENT", current)
    monkeypatch.setattr(module, "PINS", {name: module.digest(raw) for name, raw in old.items()})
    monkeypatch.setattr(module, "services", lambda: b"stable fixture pids")
    return module, old, system


def test_precise_delta_preserves_backup_and_blocks_repeat(packet):
    module, old, system = packet
    result = module.prepare(owner=os.getuid(), system=system)
    assert result["state"] == "passed"
    assert result["service_restarts"] == result["provider_posts"] == result["database_writes"] == 0
    assert (module.BASE / "broker-v1.service").read_bytes() == (
        ROOT / "deploy/asus/api-quota-broker-v1.service"
    ).read_bytes()
    assert (module.BASE / "release_verifier.py").read_bytes() == (
        ROOT / "tests/fixtures/asus-history/release-verifier-pdf.py"
    ).read_bytes()
    assert all((module.BACKUP / name).read_bytes() == raw for name, raw in old.items())
    with pytest.raises(ValueError):
        module.prepare(owner=os.getuid(), system=system)


@pytest.mark.parametrize("failure", ["pin", "symlink", "current"])
def test_preflight_denies_drift_without_mutation(packet, failure):
    module, old, system = packet
    if failure == "current":
        module.CURRENT.unlink()
        module.CURRENT.symlink_to("different")
    else:
        target = module.BASE / "broker-v1.service"
        target.unlink()
        if failure == "symlink":
            target.symlink_to(module.BASE / "release_verifier.py")
        else:
            target.write_bytes(b"different")
            target.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        module.prepare(owner=os.getuid(), system=system)
    assert not module.BACKUP.exists()
    assert (module.BASE / "manifest.json").read_bytes() == old["manifest.json"]


def test_failure_after_first_replacement_restores_only_exact_owned_files(packet, monkeypatch):
    module, old, system = packet
    original = module.replace_file
    calls = 0

    def fail(path, raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture failure")
        original(path, raw)

    monkeypatch.setattr(module, "replace_file", fail)
    assert module.prepare(owner=os.getuid(), system=system)["state"] == "restored"
    assert all((module.BASE / name).read_bytes() == raw for name, raw in old.items())
    assert json.loads((module.BACKUP / "result.json").read_text())["state"] == "restored"


def test_transport_accepts_only_bounded_receipt():
    transport = load("prepare_pdf_transport")
    with pytest.raises(ValueError):
        transport.projection({"state": "passed", "automatic_retry": False, "secret": "fixture"})
    argv = transport.remote(b"print('fixture')")
    assert argv[:3] == ["/usr/bin/ssh", "-T", "-o"]
    assert "PrivateNetwork=yes" in argv[-1] and "MemorySwapMax=0" in argv[-1]
    assert (
        "fetch_secret()" in argv[-1]
    )  # Definition only; local exchange fetches after exact prompt.
    assert "target_command()[:6]" not in argv[-1]
    restored = transport.remote(b"print('fixture')", restore=True)
    assert "--unit=aqb-pdf-prerequisite-restore-r1" in restored[-1]
    assert "--restore" in restored[-1]


def test_rename_then_fsync_failure_cannot_claim_false_restore(packet, monkeypatch):
    module, old, system = packet
    original = module.replace_file
    calls = 0

    def fail(path, raw):
        nonlocal calls
        calls += 1
        original(path, raw)
        if calls == 1:
            raise OSError("rename succeeded, fsync failed")

    monkeypatch.setattr(module, "replace_file", fail)
    assert module.prepare(owner=os.getuid(), system=system)["state"] == "restored"
    assert all((module.BASE / name).read_bytes() == raw for name, raw in old.items())


@pytest.mark.parametrize("after", [1, 2, 3])
def test_interrupted_prep_has_independent_hash_fenced_recovery(packet, monkeypatch, after):
    module, old, system = packet
    original = module.replace_file
    calls = 0

    def crash(path, raw):
        nonlocal calls
        calls += 1
        original(path, raw)
        if calls == after:
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "replace_file", crash)
    with pytest.raises(KeyboardInterrupt):
        module.prepare(owner=os.getuid(), system=system)
    monkeypatch.setattr(module, "replace_file", original)
    assert module.restore(owner=os.getuid())["state"] == "restored"
    assert all((module.BASE / name).read_bytes() == raw for name, raw in old.items())
    with pytest.raises(FileExistsError):
        module.restore(owner=os.getuid())


@pytest.mark.parametrize("operation", ["prepare", "restore"])
@pytest.mark.parametrize("drift", ["mode", "hardlink"])
def test_operation_lock_drift_rejected_before_mutation(packet, operation, drift):
    module, old, system = packet
    if operation == "restore":
        module.prepare(owner=os.getuid(), system=system)
    baseline = {name: (module.BASE / name).read_bytes() for name in old}
    lock = module.STATE / "operation.lock"
    if drift == "mode":
        lock.chmod(0o644)
    else:
        lock.with_name("alias").hardlink_to(lock)
    with pytest.raises(ValueError):
        if operation == "prepare":
            module.prepare(owner=os.getuid(), system=system)
        else:
            module.restore(owner=os.getuid())
    assert all((module.BASE / name).read_bytes() == raw for name, raw in baseline.items())
    assert not (module.BACKUP / "restore-intent.json").exists()
