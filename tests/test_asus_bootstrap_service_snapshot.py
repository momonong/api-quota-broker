"""Exercise the real r15/generated service gates rather than permissive stand-ins."""

import importlib.util
import types
from pathlib import Path

import pytest
from asus_packet_fixtures import normal_review

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = load("history_ops_entry")
boot = load("bootstrap_maintenance")
builder = load("build_maintenance_review")


@pytest.fixture(scope="module")
def portable_normal_review(tmp_path_factory):
    return normal_review(tmp_path_factory.mktemp("normal-public-review"))


@pytest.fixture(autouse=True)
def portable_maintenance_packet(portable_normal_review, monkeypatch):
    monkeypatch.setattr(builder, "NORMAL", portable_normal_review)


RAW = b"ActiveState=active\nSubState=running\nMainPID=123\nExecMainStartTimestampMonotonic=456\nNRestarts=0\n"


def test_r1_legacy_ssh_failure_is_before_native_read(monkeypatch):
    monkeypatch.setattr(old, "native", lambda *_: pytest.fail("must reject before systemctl"))
    with pytest.raises(old.Denied, match="service_denied"):
        old.service_state("ssh.service")


def test_bootstrap_reads_three_exact_units_without_old_gate(monkeypatch):
    commands = []

    def native(argv):
        commands.append(argv)
        return RAW

    monkeypatch.setattr(old, "native", native)
    for unit in ["api-quota-broker.service", "orderflow.service", "ssh.service"]:
        assert boot.service_snapshot(old, unit)["MainPID"] == "123"
    assert [c[1] for c in commands] == ["show"] * 3
    assert [c[2] for c in commands] == [
        "api-quota-broker.service",
        "orderflow.service",
        "ssh.service",
    ]
    with pytest.raises(ValueError):
        boot.service_snapshot(old, "other.service")
    assert len(commands) == 3


@pytest.mark.parametrize(
    "raw",
    [
        RAW.replace(b"running", b"failed"),
        RAW.replace(b"MainPID=123", b"MainPID=0"),
        RAW + b"Unrequested=value\n",
    ],
)
def test_bootstrap_snapshot_rejects_unhealthy_or_unrequested_fields(monkeypatch, raw):
    monkeypatch.setattr(old, "native", lambda *_: raw)
    with pytest.raises(ValueError):
        boot.service_snapshot(old, "ssh.service")


def test_generated_entry_allows_ssh_read_but_keeps_mutation_scoped():
    module = types.ModuleType("generated_snapshot")
    exec(compile(builder.entry_source(), "generated_entry", "exec"), module.__dict__)  # noqa: S102
    commands = []
    module.native = lambda argv: commands.append(argv) or RAW
    assert module.service_state("ssh.service")["MainPID"] == "123"
    assert commands[0][:3] == ("/usr/bin/systemctl", "show", "ssh.service")
    with pytest.raises(module.Denied):
        module.service_state("other.service")
    assert module.SERVICE == "api-quota-broker.service"


@pytest.mark.parametrize("changed_after_install", [False, True])
def test_full_preclaim_and_postinstall_flow_preserves_originals(
    tmp_path, monkeypatch, changed_after_install
):
    import hashlib
    import importlib.machinery
    import json
    import os
    import stat

    real_path = Path

    def mapped(value):
        value = str(value)
        return tmp_path / value.lstrip("/") if value.startswith("/") else real_path(value)

    monkeypatch.setattr(boot, "Path", mapped)
    for name, path in [
        ("BASE", "/usr/local/lib/api-quota-broker-ops"),
        ("STATE", "/var/lib/api-quota-broker-ops"),
        ("SYSTEM", "/etc/systemd/system"),
    ]:
        target = mapped(path)
        target.mkdir(parents=True)
        monkeypatch.setattr(boot, name, target)
    monkeypatch.setattr(boot, "CLIENT", mapped("/usr/local/libexec/api-quota-broker-ops-client"))
    monkeypatch.setattr(boot, "CLAIM", boot.STATE / "maintenance-bootstrap-r1.claim.json")
    monkeypatch.setattr(boot, "RESULT", boot.STATE / "maintenance-bootstrap-r1.result.json")
    monkeypatch.setattr(boot, "BACKUP", boot.STATE / "maintenance-bootstrap-r1.backup")
    files = builder.packet()
    old_names = {
        "ops_entry.py",
        "broker_ops_policy.py",
        "history_audit_protocol.py",
        "history_audit_reader.py",
        "ops_history_projection.py",
    }
    old_units = {"api-quota-broker-ops@.service", "api-quota-broker-history-audit@.service"}
    legacy = b"fixture legacy entry"
    for name in old_names:
        (boot.BASE / name).write_bytes(legacy if name == "ops_entry.py" else b"old module")
    for name in old_units:
        (boot.SYSTEM / name).write_bytes(b"old unit")
    manifest = boot.BASE / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"files": dict.fromkeys(old_names, "old"), "units": dict.fromkeys(old_units, "old")}
        )
    )
    for path in [
        boot.CLIENT,
        mapped("/etc/sudoers.d/api-quota-broker-ops"),
        mapped("/etc/api-quota-broker-ops/policy.json"),
        mapped("/usr/local/libexec/api-quota-broker-control"),
        boot.SYSTEM / "api-quota-broker-ops.socket",
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    (boot.STATE / "operation.lock").write_bytes(b"")
    hash_bytes = lambda raw: hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(boot, "EXPECTED_MANIFEST", hash_bytes(manifest.read_bytes()))
    monkeypatch.setattr(boot, "EXPECTED_POLICY", hash_bytes(b"original"))
    monkeypatch.setattr(
        boot,
        "sha",
        lambda raw: (
            "ab54fbba16232c173e7838510f1f7c387dcf2e6d40bb20eea01d377f31bd2a88"
            if raw == legacy
            else hash_bytes(raw)
        ),
    )
    original_lstat = real_path.lstat

    def lstat(path, *args, **kwargs):
        if path == boot.BASE / "ops_entry.py":
            return types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644, st_nlink=1)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(real_path, "lstat", lstat)
    events = []

    def native(argv, **kwargs):
        events.append(argv)
        if argv[1] == "show":
            result = RAW
            if changed_after_install and boot.CLAIM.exists() and argv[2] == "ssh.service":
                result = RAW.replace(b"MainPID=123", b"MainPID=999")
            return result
        return b"active\n" if argv[1] == "is-active" else b""

    def exclusive(path, raw, mode=0o600, **kwargs):
        if path == boot.CLAIM:
            events.append(("claim",))
        with path.open("xb") as stream:
            stream.write(raw)
        os.chmod(path, mode)

    module = types.ModuleType("legacy_fixture")
    module.PROPS = old.PROPS
    module.native = native
    module.prevent_process_dumps = lambda: None
    module.package = lambda: None
    module.runtime_policy = lambda: {"config_sha256": "a" * 64}
    module.broker_pins = lambda _: None
    module.root_dir = lambda path, **kwargs: None
    module.read_root = lambda path, **kwargs: path.read_bytes()
    module.write_exclusive = exclusive
    module.service_state = lambda *_: pytest.fail(
        "bootstrap must not invoke legacy narrow service_state"
    )

    class Loader:
        def create_module(self, spec):
            return None

        def exec_module(self, target):
            target.__dict__.update(module.__dict__)

    monkeypatch.setattr(
        boot.importlib.util,
        "spec_from_file_location",
        lambda name, path: importlib.machinery.ModuleSpec(name, Loader()),
    )
    result = boot.install(files)
    shows = [event for event in events if len(event) > 1 and event[1] == "show"]
    assert [item[2] for item in shows] == [
        "api-quota-broker.service",
        "orderflow.service",
        "ssh.service",
    ] * 2
    claim_index = events.index(("claim",))
    assert sum(len(event) > 1 and event[1] == "show" for event in events[:claim_index]) == 3
    assert boot.CLAIM.exists() and boot.RESULT.exists() and boot.BACKUP.is_dir()
    if changed_after_install:
        assert result["status"] == "blocked" and result["stage"] == "verify"
        assert result["rollback_verified"] is True
        assert (boot.BASE / "ops_entry.py").read_bytes() == legacy
    else:
        assert result["status"] == "passed" and result["stage"] == "complete"
        assert (boot.BASE / "ops_entry.py").read_bytes() == files["ops_entry.py"]
    assert mapped("/etc/sudoers.d/api-quota-broker-ops").read_bytes() == b"original"
