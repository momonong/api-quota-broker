"""Actual public filesystem upgrade/rollback; all host tools and uid metadata fake."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "history_install", ROOT / "deploy/asus/install_history_ops.py"
)
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)


class FixtureEntry:
    def __init__(self, root, base, system, config, fail=None):
        self.root, self.base, self.system, self.config = root, base, system, config
        self.fail, self.calls = fail, []

    def root_dir(self, path, mode=None):
        assert path.is_dir() and (mode is None or path.stat().st_mode & 0o777 == mode)

    def read_root(self, path, *, mode, limit=131072, sha=None):
        assert path.name != "ops_doppler.cred", "encrypted credential content must never open"
        assert path.stat().st_mode & 0o777 == mode and path.stat().st_nlink == 1
        raw = path.read_bytes()
        assert len(raw) <= limit
        if sha:
            assert hashlib.sha256(raw).hexdigest() == sha
        return raw

    def write_exclusive(self, path, raw, *, mode=0o600):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if self.fail == "partial" and path.name == "history_audit_protocol.py":
            path.write_bytes(b"PUBLIC_PARTIAL")
            raise OSError("PUBLIC_PRIVATE_ERROR_MUST_NOT_OUTPUT")

    strict_json = staticmethod(json.loads)

    def package(self):
        manifest = json.loads((self.base / "manifest.json").read_bytes())
        for name, sha in manifest["files"].items():
            assert hashlib.sha256((self.base / name).read_bytes()).hexdigest() == sha
        for name, sha in manifest.get("units", {}).items():
            assert hashlib.sha256((self.system / name).read_bytes()).hexdigest() == sha

    def runtime_policy(self):
        return json.loads((self.config / "policy.json").read_bytes())

    def broker_pins(self, sha):
        assert sha == "a" * 64

    def native(self, argv, **_):
        self.calls.append(argv)
        assert (
            "restart" not in argv
            and "reload" not in argv
            and "start" not in argv
            and "stop" not in argv
        )
        if argv[0] == "/usr/bin/chage":
            return b"Account expires : 2026-11-05\n"
        if argv[:2] == ("/usr/bin/systemctl", "show"):
            if argv[2] == "api-quota-broker-ops.socket":
                return b"ActiveState=active\nSubState=listening\nUnitFileState=enabled\n"
            assert argv[2] in ("api-quota-broker.service", "orderflow.service", "ssh.service")
            return b"ActiveState=active\nSubState=running\nMainPID=100\nExecMainStartTimestampMonotonic=12345\nNRestarts=0\n"
        if argv[:2] == ("/usr/bin/systemctl", "list-units"):
            return b""
        if argv[0] == "/usr/bin/systemd-analyze":
            if self.fail == "validation":
                raise i.Blocked("upgrade_validation")
            return b""
        if argv[:2] == ("/usr/bin/systemctl", "daemon-reload"):
            return b""
        if argv[0] == "/usr/bin/python3.14":
            return b'{"operations":["inspect","history_audit"],"restart_enabled":false}'
        raise AssertionError("arbitrary native command")


def fixture(tmp_path, monkeypatch, fail=None):
    base, system, state, config = (
        tmp_path / name for name in ("base", "system", "state", "config")
    )
    for path in (base, system, state, config):
        path.mkdir(mode=0o700)
    mapping = {
        name: (base if path.parent == i.BASE else system) / path.name
        for name, path in i.MAPPING.items()
    }
    for name, value in (
        ("BASE", base),
        ("SYSTEM", system),
        ("STATE", state),
        ("CONFIG", config),
        ("AUDIT", state / "history-audit"),
        ("CLAIM", state / "history-upgrade-r1.claim.json"),
        ("MAPPING", mapping),
    ):
        monkeypatch.setattr(i, name, value)
    deploy = ROOT / "deploy/asus"
    files = {
        base / "ops_entry.py": (deploy / "ops_entry.py").read_bytes(),
        base / "broker_ops_policy.py": (deploy / "broker_ops_policy.py").read_bytes(),
        system / "api-quota-broker-ops@.service": (
            deploy / "api-quota-broker-ops@.service"
        ).read_bytes(),
    }
    manifest = {
        "schema": 1,
        "files": {
            path.name: hashlib.sha256(raw).hexdigest()
            for path, raw in files.items()
            if path.parent == base
        },
    }
    files[base / "manifest.json"] = json.dumps(manifest).encode()
    for path, raw in files.items():
        path.write_bytes(raw)
        path.chmod(0o644)
    (state / "operation.lock").touch(mode=0o600)
    (state / "retained-inspect.claim.json").write_text("PUBLIC_OLD_CLAIM")
    (state / "retained-inspect.claim.json").chmod(0o600)
    policy = {
        "schema": 1,
        "enabled": False,
        "expires_at": "2026-11-05T04:33:31+00:00",
        "ops_uid": 994,
        "ops_gid": 981,
        "peer_uid": 1000,
        "config_sha256": "a" * 64,
    }
    (config / "policy.json").write_text(json.dumps(policy))
    (config / "policy.json").chmod(0o644)
    cipher = config / "ops_doppler.cred"
    cipher.write_bytes(b"PUBLIC_ENCRYPTED_PLACEHOLDER")
    cipher.chmod(0o600)
    entry = FixtureEntry(tmp_path, base, system, config, fail=fail)
    # Only the four fixed public preservation files get a fixture read result.
    original_read = entry.read_root

    def read(path, **kw):
        if not path.is_relative_to(tmp_path):
            assert str(path) in {
                "/usr/local/libexec/api-quota-broker-control",
                "/usr/local/libexec/api-quota-broker-ops-client",
                "/etc/sudoers.d/api-quota-broker-ops",
            }
            return b"PUBLIC_FIXED_CONTRACT"
        if path == system / "api-quota-broker-ops.socket":
            return b"PUBLIC_FIXED_SOCKET"
        return original_read(path, **kw)

    entry.read_root = read
    original_lstat, original_fstat = Path.lstat, os.fstat

    def root_meta(info):
        return SimpleNamespace(
            **{
                key: getattr(info, key)
                for key in (
                    "st_mode",
                    "st_nlink",
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            },
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(Path, "lstat", lambda path: root_meta(original_lstat(path)))
    monkeypatch.setattr(os, "fstat", lambda fd: root_meta(original_fstat(fd)))
    source = {name: (deploy / name).read_bytes() for name in mapping}
    source["broker_ops_policy.py"] = (deploy / "broker_ops_policy.py").read_bytes()
    return i.Native(source, entry), files, cipher, state, base, system


@pytest.mark.parametrize("failure", [None, "validation", "partial"])
def test_real_file_upgrade_and_rollback_preserve_cipher_policy_old_claims(
    tmp_path, monkeypatch, failure
):
    native, originals, cipher, state, base, system = fixture(tmp_path, monkeypatch, fail=failure)
    old_claim = (state / "retained-inspect.claim.json").read_bytes()
    cipher_bytes = cipher.read_bytes()
    policy_bytes = (i.CONFIG / "policy.json").read_bytes()
    value = i.upgrade(native)
    assert (state / "history-upgrade-r1.claim.json").exists()
    assert (
        cipher.read_bytes() == cipher_bytes
        and (i.CONFIG / "policy.json").read_bytes() == policy_bytes
    )
    assert (state / "retained-inspect.claim.json").read_bytes() == old_claim
    assert "PUBLIC_PRIVATE_ERROR" not in json.dumps(value)
    if failure is None:
        assert (
            value["status"] == "passed"
            and json.loads((base / "manifest.json").read_bytes())["schema"] == 2
        )
        assert (system / "api-quota-broker-history-audit@.service").exists()
        assert (state / "history-audit/reader.lock").exists()
        assert value["native_inspect_executed"] is False
    elif failure == "validation":
        assert value["rollback_verified"] is True
        assert all(path.read_bytes() == raw for path, raw in originals.items())
        assert not (system / "api-quota-broker-history-audit@.service").exists()
    else:
        assert value["rollback_verified"] is False and value["automatic_retry"] is False
        assert (base / "history_audit_protocol.py").read_bytes() == b"PUBLIC_PARTIAL"
        assert (base / "ops_entry.py").read_bytes() == originals[base / "ops_entry.py"]


def test_existing_new_audit_directory_is_never_adopted(tmp_path, monkeypatch):
    native, originals, _, state, _, _ = fixture(tmp_path, monkeypatch)
    (state / "history-audit").mkdir(mode=0o700)
    value = i.upgrade(native)
    assert value["status"] == "blocked" and value["rollback_verified"] is None
    assert not (state / "history-upgrade-r1.claim.json").exists()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
