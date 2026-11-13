"""Real release files, CAS and receipts; systemd/HTTP replaced only at host boundary."""

import base64
import hashlib
import importlib.util
import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def helper():
    path = ROOT / "deploy/hp_quota_maintenance.py"
    assert path.exists(), "HP dedicated publisher must be implemented"
    spec = importlib.util.spec_from_file_location("hp_quota_maintenance_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(module, operation=None, release="new", current="old", previous=None):
    files = []
    for path in module.ALLOWED:
        raw = b"pass\n" if path.endswith(".py") else b"{}" if path.endswith(".json") else b"fixture"
        files.append(
            {
                "path": path,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content_base64": base64.b64encode(raw).decode(),
            }
        )
    return {
        "schema": "hp-quota-release-v1",
        "operation_id": operation or str(uuid.uuid4()),
        "release_id": release,
        "expected_current": current,
        "expected_manifest_sha256": previous or "a" * 64,
        "files": files,
    }


def test_client_package_accepted_without_importing_payload(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from hp_quota_client import build
    from hp_quota_client import validate as client_validate

    metadata = tmp_path / "metadata"
    metadata.mkdir()
    for name in (
        "accounts.json",
        "gateway.json",
        "provider-observations.json",
        "registry.json",
        "reviewed.json",
    ):
        (metadata / name).write_text("{}")
    candidate = build(metadata, "new", "old", "a" * 64, str(uuid.uuid4()))
    h = helper()
    manifest, files = h.validate(candidate)
    assert len(files) == 51
    assert h.digest(h.canonical(manifest)) == client_validate(candidate)["manifest_sha256"]


@pytest.mark.parametrize(
    "mutation",
    ["traversal", "duplicate", "size_bool", "digest", "extra", "missing", "syntax", "launcher"],
)
def test_untrusted_package_rejected_before_claim(mutation):
    h = helper()
    data = package(h)
    if mutation == "traversal":
        data["files"][0]["path"] = "../../etc/sudoers"
    if mutation == "duplicate":
        data["files"].append(data["files"][0])
    if mutation == "size_bool":
        data["files"][0]["size"] = True
    if mutation == "digest":
        data["files"][0]["sha256"] = "b" * 64
    if mutation == "extra":
        data["shell"] = "anything"
    if mutation == "missing":
        data["files"].pop()
    if mutation == "launcher":
        data["files"][0]["path"] = "launcher.py"
    if mutation == "syntax":
        raw = b"def invalid("
        row = data["files"][0]
        row.update(
            size=len(raw), sha256=h.digest(raw), content_base64=base64.b64encode(raw).decode()
        )
    with pytest.raises(h.Refused):
        h.validate(data)


@pytest.fixture
def host(tmp_path, monkeypatch):
    h = helper()
    root, state = tmp_path / "app", tmp_path / "state"
    root.mkdir(mode=0o750)
    (root / "releases").mkdir(mode=0o750)
    state.mkdir(mode=0o700)
    (state / "history").mkdir(mode=0o700)
    (tmp_path / "lock").touch(mode=0o600)
    monkeypatch.setattr(h, "OWNER", os.getuid())
    monkeypatch.setattr(h, "GROUP", os.getgid())
    operator = h.Operator(root, state, tmp_path / "lock")
    old = package(h, release="old", current="bootstrap")
    manifest, files = h.validate(old)
    operator.stage("old", manifest, files)
    (root / "current").symlink_to("releases/old")
    old_sha = h.digest(h.canonical(manifest))
    monkeypatch.setattr(h, "verify_service", lambda *_: None)
    monkeypatch.setattr(h, "restart_service", lambda: None)
    return h, operator, package(h, previous=old_sha)


def request(action, payload=None, operation=None):
    data = {"schema": "hp-quota-maintenance-v1", "action": action}
    if payload is not None:
        data["payload"] = payload
    if operation is not None:
        data["operation_id"] = operation
    return data


def test_publish_readback_and_replay_are_distinct(host):
    h, operator, data = host
    assert operator.handle(request("validate", data))["status"] == "validated"
    assert not list(operator.state.glob("*.json"))
    result = operator.handle(request("publish", data))
    assert result["status"] == "complete"
    assert operator.handle(request("status", operation=data["operation_id"]))["current"] == "new"
    assert (operator.root / "current").readlink() == Path("releases/new")
    with pytest.raises(h.Refused):
        operator.handle(request("publish", data))


def test_baseline_drift_does_not_claim(host):
    h, operator, data = host
    data["expected_manifest_sha256"] = "b" * 64
    with pytest.raises(h.Refused):
        operator.handle(request("publish", data))
    assert not list(operator.state.glob("*.json"))
    assert (operator.root / "current").readlink() == Path("releases/old")


def test_health_failure_restores_previous_and_keeps_receipt(host, monkeypatch):
    h, operator, data = host

    def check(manifest):
        if (operator.root / "current").readlink() == Path("releases/new"):
            raise h.Refused("fixture_unhealthy")

    monkeypatch.setattr(h, "verify_service", check)
    result = operator.handle(request("publish", data))
    assert result["status"] == "rolled_back"
    assert operator.current()[0] == "old"
    assert (operator.root / "releases/new").is_dir()
    assert (
        operator.handle(request("status", operation=data["operation_id"]))["status"]
        == "rolled_back"
    )


def test_crash_preserves_original_operation_and_blocks_new_uuid(host, monkeypatch):
    h, operator, data = host

    def crash():
        raise KeyboardInterrupt()

    monkeypatch.setattr(h, "restart_service", crash)
    with pytest.raises(KeyboardInterrupt):
        operator.handle(request("publish", data))
    another = {**data, "operation_id": str(uuid.uuid4()), "release_id": "another"}
    with pytest.raises(h.Refused, match="unresolved"):
        operator.handle(request("publish", another))
    assert (
        operator.handle(request("status", operation=data["operation_id"]))["status"] == "restarting"
    )
    monkeypatch.setattr(h, "restart_service", lambda: None)
    assert (
        operator.handle(request("rollback", operation=data["operation_id"]))["status"]
        == "rolled_back"
    )
    assert operator.current()[0] == "old"


def test_release_symlink_and_same_size_content_drift_are_rejected(host):
    h, operator, data = host
    asset = operator.root / "releases/old/app/quota_broker/dashboard_assets/index.html"
    original = asset.read_bytes()
    asset.write_bytes(b"X" * len(original))
    with pytest.raises(h.Refused):
        operator.handle(request("publish", data))
    asset.unlink()
    asset.symlink_to("/etc/passwd")
    result = operator.handle(request("status"))
    assert result["status"] == "unverified" and result["current"] is None


def test_foreign_current_never_rolled_back(host, monkeypatch):
    h, operator, data = host
    original = operator.switch

    def switched(release, operation):
        original(release, operation)
        if release == "new":
            (operator.root / "current").unlink()
            (operator.root / "current").symlink_to("releases/foreign")
            raise h.Refused("external_change")

    monkeypatch.setattr(operator, "switch", switched)
    result = operator.handle(request("publish", data))
    assert result["status"] == "unknown"
    assert (operator.root / "current").readlink() == Path("releases/foreign")


def test_wire_rejects_unknown_commands_duplicate_keys_and_extra_fields(host):
    h, operator, _data = host
    with pytest.raises(h.Refused):
        h.parse(b'{"action":"status","action":"publish"}')
    for raw in [request("shell"), {**request("status"), "command": "whoami"}, request("publish")]:
        with pytest.raises(h.Refused):
            operator.handle(raw)


def test_old_complete_rollback_after_new_release_preserves_receipts(host):
    h, operator, first = host
    operator.handle(request("publish", first))
    second = package(h, release="next", current="new", previous=operator.current()[1])
    operator.handle(request("publish", second))
    with pytest.raises(h.Refused, match="foreign_current"):
        operator.handle(request("rollback", operation=first["operation_id"]))
    assert operator.receipt(first["operation_id"])["status"] == "complete"
    third = package(h, release="third", current="next", previous=operator.current()[1])
    assert operator.handle(request("publish", third))["status"] == "complete"


def test_same_operation_recovers_interrupted_switch_and_receipt_write(host):
    _h, operator, data = host
    operator.handle(request("publish", data))
    operation = data["operation_id"]
    (operator.root / (".current-" + operation)).symlink_to("releases/old")
    pending = operator.state / (operation + ".pending")
    pending.write_bytes(b'{"interrupted":')
    pending.chmod(0o600)
    assert operator.handle(request("rollback", operation=operation))["status"] == "rolled_back"
    assert operator.current()[0] == "old"
    assert not pending.exists()
    assert any(p.read_bytes() == b'{"interrupted":' for p in (operator.state / "history").iterdir())


def test_status_separates_original_receipt_from_current_release(host):
    h, operator, first = host
    operator.handle(request("publish", first))
    first_sha = operator.current()[1]
    second = package(h, release="next", current="new", previous=first_sha)
    operator.handle(request("publish", second))
    proof = operator.handle(request("status", operation=first["operation_id"]))
    assert proof["current"] == "next"
    assert proof["release_id"] == "new" and proof["release_manifest_sha256"] == first_sha


def test_service_group_can_read_release_under_root_private_umask(host):
    import stat

    _h, operator, data = host
    previous = os.umask(0o077)
    try:
        operator.handle(request("publish", data))
    finally:
        os.umask(previous)
    release = operator.root / "releases/new"
    assert stat.S_IMODE(release.stat().st_mode) == 0o750
    assert stat.S_IMODE((release / "app/quota_broker").stat().st_mode) == 0o750
    assert stat.S_IMODE((release / "metadata/accounts.json").stat().st_mode) == 0o640


def test_client_and_helper_publish_and_lost_reply_reconcile(host, tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import hp_quota_client as client

    h, operator, data = host
    state = tmp_path / "client"

    def transport(req):
        result = operator.handle(req)
        if req["action"] == "publish":
            raise TimeoutError("synthetic lost reply")
        return result

    with pytest.raises(TimeoutError):
        client.publish(data, state, transport)
    assert operator.receipt(data["operation_id"])["status"] == "complete"
    result = client.reconcile(data["operation_id"], state, operator.handle)
    assert result["status"] == "complete"
    second = package(h, release="next", current="new", previous=operator.current()[1])
    assert client.publish(second, state, operator.handle)["active"] is True


def test_shared_server_lock_blocks_other_management_client(host):
    import fcntl

    _h, operator, data = host
    fd = os.open(operator.lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            operator.handle(request("publish", data))
        assert not list(operator.state.glob("*.json"))
    finally:
        os.close(fd)


def test_validation_never_executes_python_payload(tmp_path):
    h = helper()
    data = package(h)
    marker = tmp_path / "must-not-exist"
    raw = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n".encode()
    row = next(row for row in data["files"] if row["path"].endswith("/__init__.py"))
    row.update(size=len(raw), sha256=h.digest(raw), content_base64=base64.b64encode(raw).decode())
    h.validate(data)
    assert not marker.exists()


def test_fixed_launcher_rejects_root_before_application_import(monkeypatch):
    import runpy

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="service_identity_required"):
        runpy.run_path(str(ROOT / "deploy/hp_quota_launcher.py"), run_name="__main__")


@pytest.mark.parametrize("failure", [None, "private_exposed", "asset_drift", "wrong_process"])
def test_host_readiness_checks_runtime_assets_and_anonymous_boundary(
    tmp_path, monkeypatch, failure
):
    h = helper()
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    argv = [
        "/usr/bin/python3",
        "-I",
        "-B",
        "/usr/local/libexec/momonong-quota-launcher.py",
        "dashboard-serve",
        "--reviewed-config",
        "/opt/momonong-quota/current/metadata/reviewed.json",
        "--accounts-file",
        "/opt/momonong-quota/current/metadata/accounts.json",
        "--provider-metadata-file",
        "/opt/momonong-quota/current/metadata/provider-observations.json",
        "--dashboard-password-file",
        "/etc/api-quota-dashboard/browser-auth.json",
        "--public-base-url",
        "https://momonong.me/quota/",
        "--port",
        "18586",
    ]
    if failure == "wrong_process":
        argv[-1] = "18587"
    (proc / "cmdline").write_bytes(b"\0".join(x.encode() for x in argv) + b"\0")
    original_path = h.Path
    monkeypatch.setattr(
        h, "Path", lambda value: tmp_path / "proc" if value == "/proc" else original_path(value)
    )
    monkeypatch.setattr(
        h, "command", lambda *a, **k: b"ActiveState=active\nSubState=running\nMainPID=123\n"
    )
    clock = iter([0, 30])
    monkeypatch.setattr(h.time, "monotonic", lambda: next(clock))
    raw = b"fixed asset"
    sha = h.digest(raw)
    assets = [
        {"path": "app/quota_broker/dashboard_assets/" + name, "sha256": sha}
        for name in ("index.html", "dashboard.js", "dashboard.css")
    ]

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 18586

        def request(self, method, path, headers):
            assert method == "GET" and headers == {"Host": "momonong.me"}
            self.path = path

        def getresponse(self):
            self.status = (
                200
                if self.path in ("/quota/", "/quota/dashboard.js", "/quota/dashboard.css")
                else 401
                if self.path in ("/quota/api/snapshot", "/quota/api/usage")
                else 404
            )
            if failure == "private_exposed" and self.path == "/quota/api/snapshot":
                self.status = 200
            return self

        def read(self, _):
            return b"changed" if failure == "asset_drift" else raw

        def getheader(self, *_):
            return "no-store"

        def close(self):
            pass

    monkeypatch.setattr(h, "HTTPConnection", Connection)
    if failure:
        with pytest.raises(h.Refused, match="readiness_failed"):
            h.verify_service(assets)
    else:
        h.verify_service(assets)


def test_launcher_copies_pinned_metadata_to_private_service_owned_files(
    host, tmp_path, monkeypatch
):
    import stat

    _h, operator, _data = host
    path = ROOT / "deploy/hp_quota_launcher.py"
    spec = importlib.util.spec_from_file_location("quota_launcher", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "ROOT_OWNER", os.getuid())
    destination = tmp_path / "private"
    destination.mkdir(mode=0o700)
    launcher.private_metadata(operator.root / "releases/old", destination)
    for name in launcher.METADATA:
        info = (destination / name).stat()
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        assert (destination / name).read_bytes() == (
            operator.root / "releases/old/metadata" / name
        ).read_bytes()
    target = operator.root / "releases/old/metadata/accounts.json"
    target.write_bytes(b"[]")
    with pytest.raises(ValueError, match="metadata_digest"):
        launcher.private_metadata(operator.root / "releases/old", tmp_path / "other")
