"""ASUS deployment preparation fixtures; no root, SSH, credentials or service changes."""

import argparse
import importlib.util
import json
import os
import shlex
import sqlite3
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_broker import cli

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "asus"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = load_module("asus_install_fixture", DEPLOY / "install.py")
credentials = load_module("asus_credentials_install_fixture", DEPLOY / "import_credentials.py")
swap_guard = load_module("asus_swap_guard_fixture", DEPLOY / "swap_guard.py")

TARGET = {
    "python": "3.14.4",
    "implementation": "CPython",
    "architecture": "x86_64",
    "gil_disabled": False,
    "minimum_glibc": "2.17",
}
PREPARATION = {
    "artifact_count": 10,
    "max_http_requests": 11,
    "max_download_wall_seconds": 480,
    "build_python": "3.12.3",
    "uv_version": "0.9.5",
    "source_date_epoch": 1580601600,
    "target_install": False,
    "credential_reads": 0,
    "provider_calls": 0,
}
SMOKE = {
    "schema_version": 1,
    "status": "passed",
    "mode": "target_smoke",
    "target": TARGET,
    "checks": [
        "dependency_versions",
        "project_cli_import",
        "cffi",
        "fernet",
        "aesgcm",
        "pdf",
        "ssl_ca",
        "sqlite",
        "curl_present",
    ],
    "provider_calls": 0,
    "credential_reads": 0,
}


@pytest.fixture
def layout(tmp_path, monkeypatch):
    value = installer.Layout(tmp_path)
    for directory, mode in (
        (value.base, 0o755),
        (value.base / "releases", 0o755),
        (value.config, 0o750),
        (value.config / "credentials", 0o700),
        (value.state, 0o700),
        (value.backups, 0o700),
    ):
        directory.mkdir(parents=True)
        directory.chmod(mode)
    for name in installer.CREDENTIALS:
        path = value.config / "credentials" / f"{name}.cred"
        path.write_bytes(b"encrypted-fixture-only:" + name.encode())
        path.chmod(0o600)
    policy = value.config / "credentials/doppler-metadata.json"
    policy.write_text(
        json.dumps(
            credentials.metadata_record(
                datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
                human_attested=True,
                now=datetime.now(UTC),
            )
        )
    )
    policy.chmod(0o600)
    config = value.config / "gateway.json"
    config.write_text('{"fixture":"original"}')
    config.chmod(0o640)
    with sqlite3.connect(value.state / "ledger.sqlite3") as db:
        db.execute("CREATE TABLE receipt (state TEXT)")
        db.execute("INSERT INTO receipt VALUES ('unknown')")
    (value.state / "ledger.sqlite3").chmod(0o600)
    monkeypatch.setattr(installer, "stopped_service", lambda: None)
    return value


def receipt_states(db_path):
    with sqlite3.connect(db_path) as db:
        return [row[0] for row in db.execute("SELECT state FROM receipt ORDER BY rowid")]


def release(layout, name):
    path = layout.base / "releases" / name
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    return path


@pytest.fixture
def runtime_case(layout, tmp_path, monkeypatch, request):
    # The local sandbox presents OS-owned / and /tmp as nobody. Model the
    # reviewed target's root-owned ancestors without changing any real mode.
    lstat = Path.lstat

    def target_stat(path, *args, **kwargs):
        info = lstat(path, *args, **kwargs)
        if path in (Path("/"), Path("/tmp")):
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", target_stat)
    source_digest, runtime_digest = "a" * 64, "b" * 64
    candidate = release(layout, "release-" + source_digest)
    (candidate / "deploy/asus").mkdir(parents=True)
    config = candidate / "deploy/asus/gateway.disabled.json"
    config.write_bytes(b'{"targets":[]}\n')
    (candidate / "deploy/asus/api-quota-broker.service").write_bytes(
        (DEPLOY / "api-quota-broker.service").read_bytes()
    )
    (candidate / "runtime/bin").mkdir(parents=True)
    python = candidate / "runtime/bin/python"
    python.write_bytes(b"fixture interpreter; never executed")
    python.chmod(0o755)
    private = tempfile.TemporaryDirectory(prefix="quota-asus-fixture-")
    request.addfinalizer(private.cleanup)
    bundle = Path(private.name) / "bundle"
    (bundle / "tools").mkdir(parents=True)
    uv = bundle / "tools/uv"
    uv.write_bytes(b"fixture uv; never executed")
    uv.chmod(0o755)
    for name in ("requirements.txt", "runtime-manifest.json", "SHA256SUMS"):
        path = bundle / name
        path.write_bytes(b"fixture")
        path.chmod(0o644)
    for directory in (candidate / "runtime", candidate / "runtime/bin", bundle, bundle / "tools"):
        directory.chmod(0o755)
    receipt = {
        "schema_version": 1,
        "status": "verified",
        "policy": "asus-runtime-v1",
        "runtime_manifest_sha256": runtime_digest,
        "source_payload_manifest_sha256": source_digest,
        "project_wheel_sha256": "c" * 64,
        "target": TARGET,
        "preparation": PREPARATION,
        "files": [{"path": "tools/uv", "mode": 0o755}],
    }
    prepared = {
        "schema_version": 1,
        "policy": "asus-native-runtime-v1",
        "status": "prepared",
        "bundle": receipt,
        "native_smoke": SMOKE,
        "files": installer.runtime_inventory(candidate / "runtime", root_uid=os.getuid()),
    }
    prepared_digest = installer.write_receipt(candidate / "runtime-prepared.json", prepared)
    layout.unit.parent.mkdir(parents=True)
    layout.unit.parent.chmod(0o755)
    monkeypatch.setattr(
        installer,
        "validate_staged_source",
        lambda *args, **kwargs: (candidate, {"artifact_id": candidate.name}),
    )
    monkeypatch.setattr(installer, "verify_runtime_bundle", lambda *args: receipt)
    monkeypatch.setattr(installer, "system_python_gate", lambda: None)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = json.dumps(SMOKE).encode() if "--target-smoke" in argv else b""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(installer.subprocess, "run", run)
    return SimpleNamespace(
        layout=layout,
        candidate=candidate,
        bundle=bundle,
        receipt=receipt,
        source_digest=source_digest,
        runtime_digest=runtime_digest,
        prepared_digest=prepared_digest,
        config_digest=installer.file_hash(config),
        calls=calls,
    )


def activate(case, apply=False):
    return installer.activation(
        case.layout,
        Path("/fixture-archive"),
        case.source_digest,
        case.bundle,
        case.runtime_digest,
        case.prepared_digest,
        case.config_digest,
        service_uid=os.getuid(),
        service_gid=os.getgid(),
        apply=apply,
        root_uid=os.getuid(),
    )


def directives():
    source = (DEPLOY / "api-quota-broker.service").read_text()
    values = {}
    for line in source.replace("\\\n", " ").splitlines():
        if line and not line.startswith(("#", "[")):
            name, value = line.split("=", 1)
            values.setdefault(name, []).append(value.strip())
    return values


def command():
    return shlex.split(directives()["ExecStart"][0])


def test_service_uses_fixed_owner_private_storage_and_runtime_credentials():
    unit = directives()
    assert unit["User"] == unit["Group"] == ["api-quota-broker"]
    assert unit["StateDirectory"] == unit["RuntimeDirectory"] == ["api-quota-broker"]
    assert unit["StateDirectoryMode"] == unit["RuntimeDirectoryMode"] == ["0700"]
    assert unit["UMask"] == ["0077"]
    names = {"digest_key", "client_token", "admin_token", "queue_key", "doppler_service_token"}
    assert unit["LoadCredentialEncrypted"] == [
        f"{name}:/etc/api-quota-broker/credentials/{name}.cred"
        for name in (
            "digest_key",
            "client_token",
            "admin_token",
            "queue_key",
            "doppler_service_token",
        )
    ]
    assert not {"Environment", "EnvironmentFile", "LoadCredential", "SetCredential"} & unit.keys()
    assert {value.split(":", 1)[0] for value in unit["LoadCredentialEncrypted"]} == names
    assert unit["ExecStartPre"] == [
        "/opt/api-quota-broker/current/runtime/bin/python -I -B /opt/api-quota-broker/current/deploy/asus/swap_guard.py",
        "/usr/bin/install -m 0600 %d/queue_key /run/api-quota-broker/queue.key",
    ]
    assert unit["Slice"] == ["system.slice"]
    args = command()
    assert args[:5] == [
        "/opt/api-quota-broker/current/runtime/bin/python",
        "-I",
        "-m",
        "quota_broker.cli",
        "gateway-serve",
    ]
    for flag, value in {
        "--db": "/var/lib/api-quota-broker/ledger.sqlite3",
        "--config": "/etc/api-quota-broker/gateway.json",
        "--port": "18084",
        "--digest-key-file": "%d/digest_key",
        "--client-token-file": "%d/client_token",
        "--admin-token-file": "%d/admin_token",
        "--doppler-token-file": "%d/doppler_service_token",
        "--queue-key-file": "/run/api-quota-broker/queue.key",
    }.items():
        assert args[args.index(flag) + 1] == value


def test_service_bounds_shared_host_resources_and_starts_without_queue_worker():
    unit = directives()
    for name, value in {
        "CPUQuota": "50%",
        "MemoryHigh": "256M",
        "MemoryMax": "384M",
        "MemorySwapMax": "0",
        "TasksMax": "32",
        "LimitNOFILE": "128",
        "KillMode": "control-group",
    }.items():
        assert unit[name] == [value]
    args = command()
    assert args.count("--no-queue-worker") == 1
    for flag, value in {
        "--max-media-input-bytes": "1048576",
        "--max-request-bytes": "2097152",
        "--max-result-bytes": "4194304",
    }.items():
        assert args[args.index(flag) + 1] == value


def test_service_cannot_write_releases_config_home_or_other_services():
    unit = directives()
    assert unit["ProtectSystem"] == ["strict"]
    assert unit["ProtectHome"] == unit["NoNewPrivileges"] == ["yes"]
    assert unit["CapabilityBoundingSet"] == unit["AmbientCapabilities"] == [""]
    assert unit["ReadWritePaths"] == ["/var/lib/api-quota-broker /run/api-quota-broker"]
    assert unit["RestrictAddressFamilies"] == ["AF_UNIX AF_INET AF_INET6"]
    assert "OrderFlow" not in (DEPLOY / "api-quota-broker.service").read_text()


def test_unit_command_is_accepted_by_actual_cli_without_reading_files(monkeypatch):
    class Parsed(Exception):
        pass

    captured = {}
    parse = argparse.ArgumentParser.parse_args

    def capture(parser, *args, **kwargs):
        namespace = parse(parser, *args, **kwargs)
        captured.update(vars(namespace))
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    monkeypatch.setattr(sys, "argv", ["quota-broker", *command()[4:]])
    with pytest.raises(Parsed):
        cli.main()
    assert captured["command"] == "gateway-serve"
    assert captured["no_queue_worker"] is True
    assert captured["max_media_input_bytes"] == 1024**2
    assert captured["max_request_bytes"] == 2 * 1024**2
    assert captured["max_result_bytes"] == 4 * 1024**2


def test_preflight_reads_only_metadata_and_preserves_first_install(layout, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("metadata preflight read file contents")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    report = installer.inspect_layout(
        layout, root_uid=os.getuid(), service_uid=os.getuid(), service_gid=os.getgid()
    )
    assert report == {"current_release": None, "missing": []}
    assert not (layout.base / "current").exists()


@pytest.mark.parametrize("name", ["gateway.json", "credentials/queue_key.cred"])
def test_preflight_rejects_symlink_instead_of_fixing_it(layout, tmp_path, name):
    target = layout.config / name
    old = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(old)
    target.symlink_to(outside)
    with pytest.raises(installer.DeploymentError, match="symlink"):
        installer.inspect_layout(layout, root_uid=os.getuid(), service_uid=os.getuid())
    assert target.is_symlink() and outside.read_bytes() == old


def test_preflight_rejects_world_readable_credential_and_unsafe_sqlite_sidecar(layout):
    key = layout.config / "credentials" / "queue_key.cred"
    key.chmod(0o644)
    with pytest.raises(installer.DeploymentError, match="unsafe"):
        installer.inspect_layout(layout, root_uid=os.getuid(), service_uid=os.getuid())
    assert stat.S_IMODE(key.stat().st_mode) == 0o644
    key.chmod(0o600)
    sidecar = layout.state / "ledger.sqlite3-wal"
    sidecar.symlink_to("missing")
    with pytest.raises(installer.DeploymentError, match="symlink"):
        installer.inspect_layout(layout, root_uid=os.getuid(), service_uid=os.getuid())


@pytest.mark.parametrize("selector", ["../outside", "/tmp/outside", "releases/../outside"])
def test_current_selector_cannot_escape_release_directory(layout, selector):
    (layout.base / "current").symlink_to(selector)
    with pytest.raises(installer.DeploymentError, match="outside"):
        installer.current_release(layout, root_uid=os.getuid())


def test_copy_hash_refuse_symlink_fifo_hardlink_and_existing_destination(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"fixture")
    destination = tmp_path / "existing"
    destination.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        installer.copy_exclusive(source, destination)
    assert destination.read_bytes() == b"preserve"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    symlink = tmp_path / "link"
    symlink.symlink_to(source)
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    for path in (fifo, symlink, hardlink):
        with pytest.raises(installer.DeploymentError):
            installer.file_hash(path)
        with pytest.raises(installer.DeploymentError):
            installer.copy_exclusive(path, tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()


def test_backup_preserves_unknown_and_keys_without_plaintext_decryption(layout):
    release(layout, "old")
    (layout.base / "current").symlink_to("releases/old")
    snapshot = installer.backup(layout, root_uid=os.getuid())
    receipt = installer.backup_receipt(layout, snapshot, root_uid=os.getuid())
    assert receipt["previous_release"] == "old"
    assert receipt_states(snapshot / "ledger.sqlite3") == ["unknown"]
    assert receipt_states(layout.state / "ledger.sqlite3") == ["unknown"]
    for name in installer.CREDENTIALS:
        assert (snapshot / f"{name}.cred").read_bytes() == (
            layout.config / "credentials" / f"{name}.cred"
        ).read_bytes()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshot.iterdir())


def test_backup_reads_uncheckpointed_wal_without_restoring_or_changing_state(layout):
    original = sqlite3.connect(layout.state / "ledger.sqlite3")
    try:
        assert original.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        original.execute("INSERT INTO receipt VALUES ('dispatching')")
        original.commit()
        assert (layout.state / "ledger.sqlite3-wal").stat().st_size > 0
        snapshot = installer.backup(layout, root_uid=os.getuid())
        assert receipt_states(snapshot / "ledger.sqlite3") == ["unknown", "dispatching"]
        assert receipt_states(layout.state / "ledger.sqlite3") == ["unknown", "dispatching"]
    finally:
        original.close()


def test_backup_requires_stopped_service_and_retains_existing_backups_on_failure(
    layout, monkeypatch
):
    def active():
        raise installer.DeploymentError("broker must be stopped")

    existing = layout.backups / ("a" * 32)
    existing.mkdir(mode=0o700)
    monkeypatch.setattr(installer, "stopped_service", active)
    with pytest.raises(installer.DeploymentError, match="stopped"):
        installer.backup(layout, root_uid=os.getuid())
    assert list(layout.backups.iterdir()) == [existing]


def test_apply_gate_requires_root_tty_and_exact_reviewed_host(monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(installer.socket, "gethostname", lambda: installer.HOST)
    with pytest.raises(installer.DeploymentError, match="root TTY"):
        installer.operator_gate()
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False)
    with pytest.raises(installer.DeploymentError, match="root TTY"):
        installer.operator_gate()
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(installer.socket, "gethostname", lambda: "unreviewed-host")
    with pytest.raises(installer.DeploymentError, match="ASUS"):
        installer.operator_gate()


def test_rollback_changes_only_release_config_preserving_new_ledger_and_keys(layout):
    release(layout, "old")
    release(layout, "new")
    selector = layout.base / "current"
    selector.symlink_to("releases/old")
    snapshot = installer.backup(layout, root_uid=os.getuid())
    selector.unlink()
    selector.symlink_to("releases/new")
    (layout.config / "gateway.json").write_text('{"fixture":"new"}')
    with sqlite3.connect(layout.state / "ledger.sqlite3") as db:
        db.execute("INSERT INTO receipt VALUES ('completed')")
    key_hashes = {
        name: installer.file_hash(layout.config / "credentials" / f"{name}.cred")
        for name in installer.CREDENTIALS
    }
    result = installer.rollback(layout, snapshot, root_uid=os.getuid())
    assert result["rollback_release"] == "old" and result["activation"] == "stopped"
    assert os.readlink(selector) == "releases/old"
    assert json.loads((layout.config / "gateway.json").read_text()) == {"fixture": "original"}
    assert receipt_states(layout.state / "ledger.sqlite3") == ["unknown", "completed"]
    assert all(
        installer.file_hash(layout.config / "credentials" / f"{name}.cred") == digest
        for name, digest in key_hashes.items()
    )
    assert receipt_states(Path(result["before_rollback_backup"]) / "ledger.sqlite3") == [
        "unknown",
        "completed",
    ]


def test_first_install_rollback_leaves_broker_stopped_and_disabled(layout, monkeypatch):
    snapshot = installer.backup(layout, root_uid=os.getuid())
    release(layout, "new")
    (layout.base / "current").symlink_to("releases/new")
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    result = installer.rollback(layout, snapshot, root_uid=os.getuid())
    assert result["rollback_release"] is None and result["activation"] == "stopped"
    assert not (layout.base / "current").exists()
    assert calls == [["systemctl", "disable", "api-quota-broker.service"]]
    assert receipt_states(layout.state / "ledger.sqlite3") == ["unknown"]


@pytest.mark.parametrize("change", ["hash", "credential"])
def test_rollback_hash_failure_or_key_rotation_cannot_mutate_current_state(layout, change):
    release(layout, "old")
    release(layout, "new")
    selector = layout.base / "current"
    selector.symlink_to("releases/old")
    snapshot = installer.backup(layout, root_uid=os.getuid())
    selector.unlink()
    selector.symlink_to("releases/new")
    if change == "hash":
        (snapshot / "gateway.json").write_text("tampered")
    else:
        (layout.config / "credentials" / "queue_key.cred").write_bytes(b"rotated-encrypted-fixture")
    before = installer.file_hash(layout.state / "ledger.sqlite3")
    with pytest.raises(installer.DeploymentError):
        installer.rollback(layout, snapshot, root_uid=os.getuid())
    assert os.readlink(selector) == "releases/new"
    assert installer.file_hash(layout.state / "ledger.sqlite3") == before


def test_real_artifact_verifier_and_extractor_stage_exclusively_without_selecting(
    layout, tmp_path, monkeypatch
):
    builder = load_module(
        "asus_release_fixture_install", DEPLOY.parents[1] / "scripts" / "build_asus_release.py"
    )
    source = tmp_path / "source-fixture"
    source.mkdir()
    for relative in builder._paths():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture payload\n")
    (source / "pyproject.toml").write_text('[project]\nname="api-quota-broker"\nversion="0.1.0"\n')
    (source / "deploy/asus/api-quota-broker.service").write_bytes(
        (DEPLOY / "api-quota-broker.service").read_bytes()
    )
    monkeypatch.setattr(
        builder,
        "read_provenance",
        lambda *args: {
            "head": "a" * 40,
            "working_tree_dirty": False,
            "payload_dirty": False,
            "included_changes": [],
            "description": builder.DESCRIPTION,
        },
    )
    artifact = builder.build_release(source)
    archive = tmp_path / "fixture.tar"
    archive.write_bytes(artifact.archive)
    digest = artifact.manifest["payload_manifest_sha256"]
    assert installer.verify_artifact(archive, digest) == artifact.manifest
    with pytest.raises(installer.DeploymentError, match="verification"):
        installer.verify_artifact(archive, "0" * 64)
    result = installer.stage_release(layout, archive, digest, root_uid=os.getuid())
    staged = layout.base / "releases" / result["staged_release"]
    assert (staged / "deploy/asus/api-quota-broker.service").read_bytes() == (
        DEPLOY / "api-quota-broker.service"
    ).read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o755
    assert not (layout.base / "current").exists()
    assert not (staged / "runtime").exists()
    assert (
        installer.validate_staged_source(layout, archive, digest, root_uid=os.getuid())[0] == staged
    )
    with pytest.raises(installer.DeploymentError, match="already exists"):
        installer.stage_release(layout, archive, digest, root_uid=os.getuid())
    (staged / "src/quota_broker/cli.py").write_bytes(b"modified after staging")
    with pytest.raises(installer.DeploymentError, match="payload hash mismatch"):
        installer.validate_staged_source(layout, archive, digest, root_uid=os.getuid())


@pytest.mark.parametrize(
    "violation", ["member", "same_uid", "primary_gid", "supplementary", "root"]
)
def test_dedicated_service_identity_rejects_shared_or_privileged_accounts(monkeypatch, violation):
    user = SimpleNamespace(
        pw_name="api-quota-broker",
        pw_uid=424,
        pw_gid=424,
        pw_dir="/var/lib/api-quota-broker",
        pw_shell="/usr/sbin/nologin",
    )
    group = SimpleNamespace(gr_gid=424, gr_mem=[])
    users = [user]
    groups = [424]
    if violation == "member":
        group.gr_mem = ["another-user"]
    elif violation == "same_uid":
        users.append(SimpleNamespace(pw_name="another-user", pw_uid=424, pw_gid=425))
    elif violation == "primary_gid":
        users.append(SimpleNamespace(pw_name="another-user", pw_uid=425, pw_gid=424))
    elif violation == "supplementary":
        groups.append(425)
    else:
        user.pw_uid = 0
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(installer.pwd, "getpwall", lambda: users)
    monkeypatch.setattr(installer.grp, "getgrnam", lambda name: group)
    monkeypatch.setattr(installer.os, "getgrouplist", lambda *args: groups)
    with pytest.raises(installer.DeploymentError):
        installer.service_owner()


def test_existing_group_without_user_rejects_foreign_members_before_provision(monkeypatch):
    def missing(name):
        raise KeyError

    monkeypatch.setattr(installer.pwd, "getpwnam", missing)
    monkeypatch.setattr(installer.pwd, "getpwall", list)
    monkeypatch.setattr(
        installer.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=424, gr_mem=["foreign"])
    )
    with pytest.raises(installer.DeploymentError, match="dedicated"):
        installer.service_owner()


def test_provision_default_plan_never_creates_accounts_directories_or_credentials(
    layout, monkeypatch
):
    monkeypatch.setattr(installer, "service_owner", lambda: (os.getuid(), os.getgid()))
    before = sorted(str(path) for path in layout.root.rglob("*"))
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *args, **kwargs: pytest.fail("plan executed a process")
    )
    result = installer.provision(layout, root_uid=os.getuid())
    assert result["phase"] == "provision-plan"
    assert sorted(str(path) for path in layout.root.rglob("*")) == before


def test_runtime_inventory_accepts_only_fixed_python_aliases_and_rejects_write_access(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib").mkdir()
    for directory in (runtime, runtime / "bin", runtime / "lib"):
        directory.chmod(0o755)
    (runtime / "bin/python").symlink_to("/usr/bin/python3.14")
    (runtime / "bin/python3").symlink_to("python")
    (runtime / "bin/python3.14").symlink_to("python")
    (runtime / "lib64").symlink_to("lib")
    assert len(installer.runtime_inventory(runtime, root_uid=os.getuid())) == 7
    extra = runtime / "extra"
    extra.symlink_to("/tmp/foreign")
    with pytest.raises(installer.DeploymentError, match="unapproved symlink"):
        installer.runtime_inventory(runtime, root_uid=os.getuid())
    extra.unlink()
    extra.write_bytes(b"fixture")
    extra.chmod(0o666)
    with pytest.raises(installer.DeploymentError, match="writable"):
        installer.runtime_inventory(runtime, root_uid=os.getuid())


def test_prepared_runtime_pin_files_and_native_gate_must_match(runtime_case):
    case = runtime_case
    installer.validate_native_runtime(
        case.candidate, case.receipt, case.prepared_digest, root_uid=os.getuid()
    )
    assert case.calls[0][0][1:3] == ["-I", "-B"]
    assert case.calls[0][1]["env"] == installer.CLEAN_ENV
    assert case.calls[0][1]["stderr"] is installer.subprocess.DEVNULL
    (case.candidate / "runtime/bin/python").write_bytes(b"changed")
    with pytest.raises(installer.DeploymentError, match="files or native gate changed"):
        installer.validate_native_runtime(
            case.candidate, case.receipt, case.prepared_digest, root_uid=os.getuid()
        )
    assert len(case.calls) == 1


def test_runtime_prepare_plan_is_offline_and_never_executes_uv(runtime_case, monkeypatch):
    case = runtime_case
    runtime = case.candidate / "runtime"
    (runtime / "bin/python").unlink()
    (runtime / "bin").rmdir()
    runtime.rmdir()
    (case.candidate / "runtime-prepared.json").unlink()
    result = installer.prepare_native_runtime(case.candidate, case.bundle, case.receipt)
    assert result["phase"] == "runtime-plan" and case.calls == []
    commands = result["commands"]
    assert commands[0] == [str(case.bundle / "tools/uv"), "--version"]
    assert "/usr/bin/python3.14" in commands[1] and "--no-python-downloads" in commands[1]
    for argv in commands[1:]:
        assert {"--offline", "--no-config", "--no-cache"} <= set(argv)
    assert {
        "--no-index",
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--no-deps",
    } <= set(commands[2])
    # uv0.9.5 rejects combining --no-build and --only-binary. The explicit
    # :all: policy still forbids source packages under offline/hash admission.
    assert "--no-build" not in commands[2]
    assert commands[2][commands[2].index("--only-binary") + 1] == ":all:"
    assert commands[2][commands[2].index("--find-links") + 1] == str(case.bundle / "wheelhouse")
    assert commands[2][commands[2].index("-r") + 1] == str(case.bundle / "requirements.txt")


@pytest.mark.parametrize("fault", [None, "uv_version", "native_smoke"])
def test_runtime_prepare_uses_only_verified_offline_tool_and_keeps_failure_unselected(
    runtime_case, monkeypatch, fault
):
    case = runtime_case
    runtime = case.candidate / "runtime"
    (runtime / "bin/python").unlink()
    (runtime / "bin").rmdir()
    runtime.rmdir()
    (case.candidate / "runtime-prepared.json").unlink()
    calls = []

    def fixture_command(argv, **kwargs):
        calls.append((argv, kwargs))
        if "--version" in argv:
            return SimpleNamespace(
                returncode=0, stdout=b"uv 0.9.4" if fault == "uv_version" else b"uv 0.9.5"
            )
        if "venv" in argv:
            (runtime / "bin").mkdir(parents=True)
            for directory in (runtime, runtime / "bin"):
                directory.chmod(0o755)
            python = runtime / "bin/python"
            python.write_bytes(b"fixture executable; not launched")
            python.chmod(0o755)
        if "pip" in argv:
            (runtime / ".lock").touch()
            (runtime / ".lock").chmod(0o777)
            assert "--no-build" not in argv
            assert argv[argv.index("--only-binary") + 1] == ":all:"
            assert {"--offline", "--no-index", "--require-hashes", "--no-deps"} <= set(argv)
        if "--target-smoke" in argv:
            smoke = {**SMOKE, "status": "failed"} if fault == "native_smoke" else SMOKE
            return SimpleNamespace(returncode=0, stdout=json.dumps(smoke).encode())
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(installer.subprocess, "run", fixture_command)
    if fault:
        with pytest.raises(installer.DeploymentError):
            installer.prepare_native_runtime(
                case.candidate, case.bundle, case.receipt, apply=True, root_uid=os.getuid()
            )
        assert not (case.candidate / "runtime-prepared.json").exists()
        if fault == "uv_version":
            assert len(calls) == 1 and not runtime.exists()
    else:
        result = installer.prepare_native_runtime(
            case.candidate, case.bundle, case.receipt, apply=True, root_uid=os.getuid()
        )
        prepared = case.candidate / "runtime-prepared.json"
        assert result["prepared_runtime_sha256"] == installer.file_hash(prepared)
        assert json.loads(prepared.read_text())["bundle"] == case.receipt
        assert stat.S_IMODE(prepared.stat().st_mode) == 0o600
    assert all(kwargs["env"] == installer.CLEAN_ENV for _, kwargs in calls)
    assert all(kwargs["stderr"] is installer.subprocess.DEVNULL for _, kwargs in calls)
    assert all(
        kwargs["umask"] == 0o022 for argv, kwargs in calls if "venv" in argv or "pip" in argv
    )
    assert not (case.layout.base / "current").exists() and not case.layout.unit.exists()


def test_uv_lock_sealing_preserves_inode_and_keeps_other_write_gate(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    lock = runtime / ".lock"
    lock.touch()
    lock.chmod(0o777)
    before = lock.stat()
    with pytest.raises(installer.DeploymentError, match="writable") as caught:
        installer.runtime_inventory(runtime, root_uid=os.getuid())
    assert installer.safe_error(caught.value) == {
        "reason": "runtime_writable_metadata",
        "operation": "runtime_inventory",
    }
    installer.normalize_uv_lock(runtime, root_uid=os.getuid())
    assert stat.S_IMODE(lock.stat().st_mode) == 0o777
    report = installer.normalize_uv_lock(runtime, root_uid=os.getuid(), apply=True)
    after = lock.stat()
    assert report["inode_preserved"] and after.st_ino == before.st_ino
    assert after.st_uid == before.st_uid and after.st_size == 0
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert len(installer.runtime_inventory(runtime, root_uid=os.getuid())) == 2
    extra = runtime / "other"
    extra.touch(mode=0o666)
    extra.chmod(0o666)
    with pytest.raises(installer.DeploymentError, match="writable"):
        installer.runtime_inventory(runtime, root_uid=os.getuid())


@pytest.mark.parametrize(
    "fault", ["missing", "content", "symlink", "hardlink", "mode", "owner", "busy"]
)
def test_uv_lock_rejects_untrusted_or_active_file_without_mutation(tmp_path, fault):
    import fcntl

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    lock = runtime / ".lock"
    if fault != "missing":
        lock.touch()
        lock.chmod(0o777)
    owner = os.getuid()
    descriptor = None
    if fault == "content":
        lock.write_bytes(b"unsafe")
    elif fault == "symlink":
        lock.unlink()
        lock.symlink_to(tmp_path / "foreign")
    elif fault == "hardlink":
        os.link(lock, tmp_path / "alias")
    elif fault == "mode":
        lock.chmod(0o666)
    elif fault == "owner":
        owner += 10000
    elif fault == "busy":
        descriptor = os.open(lock, os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = lock.lstat() if fault != "missing" else None
    try:
        with pytest.raises(installer.DeploymentError):
            installer.normalize_uv_lock(runtime, root_uid=owner, apply=True)
        if before:
            assert lock.lstat() == before
    finally:
        if descriptor is not None:
            os.close(descriptor)


def test_runtime_bundle_cli_receipt_binds_both_pins_and_cleans_environment(
    runtime_case, monkeypatch
):
    case = runtime_case
    calls = []

    def response(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(case.receipt).encode())

    monkeypatch.setattr(installer.subprocess, "run", response)
    # Exercise the unpatched public function from a fresh module instance.
    real = load_module("asus_install_verify_fixture", DEPLOY / "install.py")
    assert (
        real.verify_runtime_bundle(
            case.candidate, case.bundle, case.runtime_digest, case.source_digest
        )
        == case.receipt
    )
    argv, kwargs = calls[0]
    assert argv[1:3] == ["-I", "-B"]
    assert argv[argv.index("--manifest-sha256") + 1] == case.runtime_digest
    assert argv[argv.index("--source-manifest-sha256") + 1] == case.source_digest
    assert kwargs["env"] == installer.CLEAN_ENV
    with pytest.raises(real.DeploymentError, match="pinned source and bundle"):
        real.verify_runtime_bundle(case.candidate, case.bundle, case.runtime_digest, "0" * 64)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"value":NaN}', '{"value":Infinity}'])
def test_deployment_receipt_json_rejects_duplicate_and_nonfinite_fields(raw):
    with pytest.raises(installer.DeploymentError):
        installer.strict_json(raw)


def test_runtime_inventory_rejects_root_only_packages_unreadable_by_service(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    with pytest.raises(installer.DeploymentError, match="unreadable by service"):
        installer.runtime_inventory(runtime, root_uid=os.getuid())


def test_activation_default_plan_does_not_select_write_or_reload(runtime_case):
    case = runtime_case
    before = installer.file_hash(case.layout.config / "gateway.json")
    result = activate(case)
    assert result["phase"] == "activation-plan"
    assert not (case.layout.base / "current").exists()
    assert not case.layout.unit.exists()
    assert installer.file_hash(case.layout.config / "gateway.json") == before
    assert all("--target-smoke" in argv for argv, _ in case.calls)


@pytest.mark.parametrize(
    "failure",
    ["source", "bundle", "prepared_pin", "native", "credential", "enabled_config", "config_pin"],
)
def test_activation_failed_gate_never_changes_config_unit_or_selector(
    runtime_case, monkeypatch, failure
):
    case = runtime_case

    def fail(*args, **kwargs):
        raise installer.DeploymentError("fixture gate failure")

    if failure == "source":
        monkeypatch.setattr(installer, "validate_staged_source", fail)
    elif failure == "bundle":
        monkeypatch.setattr(installer, "verify_runtime_bundle", fail)
    elif failure == "prepared_pin":
        case.prepared_digest = "0" * 64
    elif failure == "native":
        monkeypatch.setattr(installer, "native_smoke", fail)
    elif failure == "credential":
        (case.layout.config / "credentials/queue_key.cred").unlink()
    elif failure == "enabled_config":
        config = case.candidate / "deploy/asus/gateway.disabled.json"
        config.write_bytes(b'{"targets":[{"enabled":true}]}')
        case.config_digest = installer.file_hash(config)
    else:
        case.config_digest = "0" * 64
    before = installer.file_hash(case.layout.config / "gateway.json")
    with pytest.raises(installer.DeploymentError):
        activate(case, apply=True)
    assert not (case.layout.base / "current").exists() and not case.layout.unit.exists()
    assert installer.file_hash(case.layout.config / "gateway.json") == before
    assert list(case.layout.backups.iterdir()) == []


def test_first_activation_receipt_and_rollback_restore_absence_without_replaying_ledger(
    runtime_case,
):
    case = runtime_case
    (case.layout.config / "gateway.json").unlink()
    result = activate(case, apply=True)
    snapshot = Path(result["before_activation_backup"])
    receipt = installer.backup_receipt(case.layout, snapshot, root_uid=os.getuid())
    assert receipt["previous_release"] is None
    assert receipt["previous_config"] is False and receipt["previous_unit"] is False
    assert os.readlink(case.layout.base / "current") == "releases/" + case.candidate.name
    assert (case.layout.config / "gateway.json").read_bytes() == b'{"targets":[]}\n'
    assert stat.S_IMODE(case.layout.unit.stat().st_mode) == 0o644
    assert stat.S_IMODE((case.layout.config / "gateway.json").stat().st_mode) == 0o640
    assert [argv for argv, _ in case.calls if argv[0] == "systemctl"] == [
        ["systemctl", "daemon-reload"]
    ]
    journal = json.loads((snapshot / "activation.json").read_text())
    assert journal["status"] == "activated-stopped"
    installer.rollback(case.layout, snapshot, root_uid=os.getuid())
    assert not case.layout.unit.exists() and not (case.layout.config / "gateway.json").exists()
    assert not (case.layout.base / "current").exists()
    assert receipt_states(case.layout.state / "ledger.sqlite3") == ["unknown"]
    assert all("start" not in argv and "enable" not in argv for argv, _ in case.calls)


def test_upgrade_rollback_restores_matched_unit_and_preserves_new_state(runtime_case):
    case = runtime_case
    release(case.layout, "old")
    (case.layout.base / "current").symlink_to("releases/old")
    case.layout.unit.write_bytes(b"fixture previous matching unit")
    case.layout.unit.chmod(0o644)
    result = activate(case, apply=True)
    with sqlite3.connect(case.layout.state / "ledger.sqlite3") as db:
        db.execute("INSERT INTO receipt VALUES ('completed')")
    installer.rollback(case.layout, Path(result["before_activation_backup"]), root_uid=os.getuid())
    assert case.layout.unit.read_bytes() == b"fixture previous matching unit"
    assert os.readlink(case.layout.base / "current") == "releases/old"
    assert receipt_states(case.layout.state / "ledger.sqlite3") == ["unknown", "completed"]


def test_activation_partial_failure_retains_reviewable_receipt_and_stays_stopped(
    runtime_case, monkeypatch
):
    case = runtime_case
    replace = installer.replace_from_snapshot

    def failure(source, destination, *args):
        if destination == case.layout.unit:
            raise OSError("fixture filesystem failure")
        return replace(source, destination, *args)

    monkeypatch.setattr(installer, "replace_from_snapshot", failure)
    with pytest.raises(OSError):
        activate(case, apply=True)
    snapshot = next(case.layout.backups.iterdir())
    journal = json.loads((snapshot / "activation.json").read_text())
    assert journal["status"] == "partial-failure; operator review required"
    assert journal["completed_steps"] == ["config", "failure"]
    assert not (case.layout.base / "current").exists()
    assert all(argv[0] != "systemctl" for argv, _ in case.calls)


@pytest.mark.parametrize("newline", [b"", b"\n"])
def test_swap_guard_accepts_only_same_unified_service_with_canonical_zero(monkeypatch, newline):
    calls = []

    def read(path, bound):
        calls.append((path, bound))
        return swap_guard.EXPECTED + newline if path == swap_guard.CGROUP else b"0" + newline

    monkeypatch.setattr(swap_guard, "_read", read)
    swap_guard.verify()
    assert calls == [
        ("/proc/self/cgroup", 4096),
        ("/sys/fs/cgroup/system.slice/api-quota-broker.service/memory.swap.max", 32),
    ]


@pytest.mark.parametrize(
    "cgroup",
    [
        b"",
        b"1:memory:/system.slice/api-quota-broker.service\n",
        b"0::/system.slice/another.service\n",
        b"0::/system.slice/api-quota-broker.service/child\n",
        b"0::/system.slice/api-quota-broker.service-other\n",
        b"0::/\n",
        b"0::/system.slice/../api-quota-broker.service\n",
        swap_guard.EXPECTED + b"\n" + swap_guard.EXPECTED + b"\n",
        swap_guard.EXPECTED + b"\n1:memory:/other\n",
        swap_guard.EXPECTED + b"\r\n",
        swap_guard.EXPECTED + b"\n\n",
    ],
)
def test_swap_guard_rejects_v1_hybrid_other_or_ambiguous_membership_before_swap_read(
    monkeypatch, cgroup
):
    calls = []

    def read(path, bound):
        calls.append(path)
        return cgroup

    monkeypatch.setattr(swap_guard, "_read", read)
    with pytest.raises(swap_guard.GuardError):
        swap_guard.verify()
    assert calls == [swap_guard.CGROUP]


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"max\n",
        b"1\n",
        b"4096\n",
        b"-1\n",
        b"00\n",
        b" 0\n",
        b"0 \n",
        b"0\r\n",
        b"0\n0\n",
        b"0\x00\n",
    ],
)
def test_swap_guard_rejects_unbounded_nonzero_and_malformed_kernel_limit(monkeypatch, value):
    monkeypatch.setattr(
        swap_guard,
        "_read",
        lambda path, bound: swap_guard.EXPECTED if path == swap_guard.CGROUP else value,
    )
    with pytest.raises(swap_guard.GuardError):
        swap_guard.verify()


def test_swap_guard_bounded_reader_handles_regular_zero_size_metadata_partial_reads_and_closes_fd(
    monkeypatch,
):
    flags_seen = []
    closed = []
    chunks = iter((b"0", b"\n", b""))
    monkeypatch.setattr(swap_guard.os, "open", lambda path, flags: flags_seen.append(flags) or 42)
    monkeypatch.setattr(
        swap_guard.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_size=0),
    )
    sizes = []
    monkeypatch.setattr(
        swap_guard.os, "read", lambda descriptor, size: sizes.append(size) or next(chunks)
    )
    monkeypatch.setattr(swap_guard.os, "close", closed.append)
    assert swap_guard._read(swap_guard.SWAP, 32) == b"0\n"
    assert flags_seen == [os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK]
    assert sizes == [33, 32, 31] and closed == [42]


@pytest.mark.parametrize("fault", ["oversize", "fifo", "read_error"])
def test_swap_guard_reader_rejects_oversize_wrong_type_and_io_errors_closing_fd(monkeypatch, fault):
    closed = []
    monkeypatch.setattr(swap_guard.os, "open", lambda *args: 42)
    monkeypatch.setattr(swap_guard.os, "close", closed.append)
    monkeypatch.setattr(
        swap_guard.os,
        "fstat",
        lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFIFO if fault == "fifo" else stat.S_IFREG
        ),
    )

    def read(descriptor, bound):
        if fault == "read_error":
            raise OSError("fixture kernel message must never leak")
        return b"x" * bound

    monkeypatch.setattr(swap_guard.os, "read", read)
    with pytest.raises((swap_guard.GuardError, OSError)):
        swap_guard._read(swap_guard.SWAP, 32)
    assert closed == [42]


@pytest.mark.parametrize("failure", ["missing", "permission", "metadata", "argument"])
def test_swap_guard_exit_failure_is_fixed_content_free_and_success_is_silent(
    monkeypatch, capsys, failure
):
    monkeypatch.setattr(sys, "argv", ["swap_guard.py"])
    if failure == "argument":
        monkeypatch.setattr(sys, "argv", ["swap_guard.py", "/fixture-foreign-cgroup"])
        monkeypatch.setattr(swap_guard, "verify", lambda: pytest.fail("unexpected metadata read"))
    else:

        def fail():
            if failure == "missing":
                raise FileNotFoundError("fixture raw metadata")
            if failure == "permission":
                raise PermissionError("fixture private kernel message")
            raise swap_guard.GuardError

        monkeypatch.setattr(swap_guard, "verify", fail)
    assert swap_guard.main() == 1
    assert capsys.readouterr() == ("", "broker swap guard failed\n")
    monkeypatch.setattr(sys, "argv", ["swap_guard.py"])
    monkeypatch.setattr(swap_guard, "verify", lambda: None)
    assert swap_guard.main() == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("fault", ["missing", "expired", "scope", "bool"])
def test_activation_requires_unexpired_approved_host_policy(runtime_case, fault):
    case = runtime_case
    path = case.layout.config / "credentials/doppler-metadata.json"
    if fault == "missing":
        path.unlink()
    else:
        value = json.loads(path.read_text())
        if fault == "expired":
            value["created_at"] = "2020-01-01T00:00:00Z"
            value["expires_at"] = "2020-01-02T00:00:00Z"
        elif fault == "scope":
            value["access"] = "write"
        else:
            value["remote_scope_verified"] = 0
        path.write_text(json.dumps(value))
    with pytest.raises(installer.DeploymentError):
        activate(case, apply=True)
    assert not (case.layout.base / "current").exists()
    assert not list(case.layout.backups.iterdir())


def test_backup_preserves_metadata_and_rollback_rejects_changed_policy(layout):
    path = layout.config / "credentials/doppler-metadata.json"
    raw = path.read_bytes()
    snapshot = installer.backup(layout, root_uid=os.getuid())
    assert (snapshot / path.name).read_bytes() == raw
    receipt = installer.backup_receipt(layout, snapshot, root_uid=os.getuid())
    assert receipt["files"][path.name] == installer.file_hash(path)
    record = json.loads(raw)
    record["token_name"] = "unreviewed-rotation"
    path.write_text(json.dumps(record))
    with pytest.raises(installer.DeploymentError, match="credential policy identity changed"):
        installer.rollback(layout, snapshot, root_uid=os.getuid())
    assert json.loads(path.read_text())["token_name"] == "unreviewed-rotation"


def test_account_commands_execute_absolute_tools_with_restricted_path(tmp_path, monkeypatch):
    # A real executable absent from PATH models the ASUS /usr/sbin boundary.
    import shutil

    for operation in ("groupadd", "useradd"):
        tool = tmp_path / operation
        marker = tmp_path / (operation + ".called")
        tool.write_text('#!/bin/sh\nprintf "executed" > "$1"\n')
        tool.chmod(0o755)
        monkeypatch.setitem(installer.ACCOUNT_TOOLS, operation, str(tool))
        assert shutil.which(operation, path=installer.CLEAN_ENV["PATH"]) is None
        installer.account_command(operation, [str(marker)])
        assert marker.read_text() == "executed"


@pytest.mark.parametrize("failure", ["missing", "exit", "timeout"])
def test_account_failure_reports_only_safe_operation_and_status(monkeypatch, failure):
    import subprocess

    errors = {
        "missing": FileNotFoundError(2, "PRIVATE path and token"),
        "exit": subprocess.CalledProcessError(
            9, ["PRIVATE argv"], output=b"PRIVATE output", stderr=b"PRIVATE stderr"
        ),
        "timeout": subprocess.TimeoutExpired("PRIVATE argv", 10, stderr=b"PRIVATE stderr"),
    }

    def run(*args, **kwargs):
        assert args[0][0] == "/usr/sbin/groupadd"
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin"
        raise errors[failure]

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(installer.DeploymentError) as caught:
        installer.account_command("groupadd", ["--system", "api-quota-broker"])
    details = installer.safe_error(caught.value)
    assert details["operation"] == "groupadd"
    assert (
        details["reason"]
        == {
            "missing": "account_tool_unavailable",
            "exit": "account_tool_exit",
            "timeout": "account_tool_timeout",
        }[failure]
    )
    assert details.get("errno") == (2 if failure == "missing" else None)
    assert details.get("exit_code") == (9 if failure == "exit" else None)
    assert "PRIVATE" not in json.dumps(details)


def test_missing_second_account_tool_stops_before_account_mutation(tmp_path, monkeypatch):
    tool = tmp_path / "groupadd"
    tool.write_text("fixture binary")
    tool.chmod(0o755)
    monkeypatch.setitem(installer.ACCOUNT_TOOLS, "groupadd", str(tool))
    monkeypatch.setitem(installer.ACCOUNT_TOOLS, "useradd", str(tmp_path / "absent"))
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *args, **kwargs: pytest.fail("mutated accounts")
    )
    with pytest.raises(installer.DeploymentError) as caught:
        installer.account_tools_gate(root_uid=os.getuid())
    assert installer.safe_error(caught.value) == {
        "reason": "account_tool_unavailable",
        "operation": "useradd",
        "errno": 2,
    }


def test_safe_error_rejects_secret_or_invalid_diagnostic_fields():
    error = installer.DeploymentError(
        "PRIVATE message",
        reason="PRIVATE reason",
        operation="PRIVATE operation",
        errno=True,
        exit_code=99999,
    )
    assert installer.safe_error(error) == {"reason": "gate_failed"}
