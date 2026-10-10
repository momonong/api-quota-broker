"""Actual umask/write/atomic recovery boundaries; no root/provider access."""

import importlib.util
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"


def load(name):
    s = importlib.util.spec_from_file_location(name, DEPLOY / (name + ".py"))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


r = load("recover_e2e_service")
e = load("representative_e2e")


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_real_write_has_exact_mode_despite_umask077(tmp_path, monkeypatch, mode):
    original = e.os.fchown
    monkeypatch.setattr(e.os, "fchown", lambda fd, uid, gid: original(fd, os.getuid(), os.getgid()))
    mask = os.umask(0o077)
    try:
        e.RootOps().write(tmp_path / "owned.json", b"public fixture", mode, 982)
    finally:
        os.umask(mask)
    assert stat.S_IMODE((tmp_path / "owned.json").stat().st_mode) == mode


def fixture(tmp_path, monkeypatch, fault=None):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    configdir = tmp_path / "config"
    configdir.mkdir(mode=0o750)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    originaldir = backups / "original"
    originaldir.mkdir(mode=0o700)
    backup = originaldir / "gateway.original.json"
    backup.write_bytes(b'{"targets":[]}\n')
    backup.chmod(0o600)
    marker = backups / "marker.json"
    config = configdir / "gateway.json"
    template = json.loads((DEPLOY / "representative_e2e.disabled.json").read_bytes())
    (private / "representative_e2e.disabled.json").write_bytes(
        (DEPLOY / "representative_e2e.disabled.json").read_bytes()
    )
    (private / "representative_e2e.disabled.json").chmod(0o600)
    now = datetime.now(UTC)
    candidate = json.loads(json.dumps(template))
    candidate["targets"][0].update(
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        source="human current Groq Free/billing attestation for bounded ASUS probe",
    )
    if fault == "candidate":
        candidate["targets"][0]["model"] = "unrelated"
    config.write_text(json.dumps(candidate, sort_keys=True, separators=(",", ":")))
    config.chmod(0o600)
    failed = {
        "mode": "asus_representative_groq_once",
        "status": "failed",
        "phase": "failed",
        "failure_phase": "temporary_target",
        "reason": "restoration_failed_operator_review_required",
        "post_attempted": False,
        "automatic_retry": False,
        "restored_original_config": False,
        "failure_stop_completed": True,
        "original_config_backup": str(backup),
        "provider_posts_max": 1,
        "preflight": {"empty_authenticated_targets": True, "empty_ledger": True},
        "started_at": (now - timedelta(seconds=1)).isoformat(),
        "completed_at": (now + timedelta(seconds=1)).isoformat(),
    }
    if fault == "posted":
        failed["post_attempted"] = True
    marker.write_text(json.dumps(failed))
    marker.chmod(0o600)
    if fault == "backup":
        backup.write_bytes(b"unrelated backup")
    if fault == "hardlink":
        os.link(config, configdir / "alias")
    db = tmp_path / "untouched.sqlite"
    db.write_bytes(b"unchanged fixture ledger bytes")
    before = {p: p.read_bytes() for p in (marker, backup, db)}
    monkeypatch.setattr(r, "BACKUP", backup)
    monkeypatch.setattr(r, "__file__", str(private / "recover_e2e_service.py"))
    original_chown = e.os.fchown
    monkeypatch.setattr(
        e.os, "fchown", lambda fd, uid, gid: original_chown(fd, os.getuid(), os.getgid())
    )
    monkeypatch.setattr(
        r.os,
        "readlink",
        lambda _: (
            "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
        ),
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/" + r.UNIT)
    unitdata = (DEPLOY / "api-quota-broker.service").read_bytes()

    def read(path, uid, gid, mode, bound):
        if str(path) == "/etc/systemd/system/api-quota-broker.service":
            return unitdata
        if str(path) == "/run/credentials/" + r.UNIT + "/client_token":
            return b"fixture-client-token-" + b"x" * 24
        return e.read_file(path, os.getuid(), os.getgid(), mode, bound)

    helper = SimpleNamespace(
        CONFIG=config,
        BACKUPS=backups,
        MARKER=marker,
        SERVICE=e.SERVICE,
        EMPTY_SHA=e.EMPTY_SHA,
        TEMPLATE_SHA=e.TEMPLATE_SHA,
        digest=e.digest,
        stamp=e.stamp,
        strict_json=e.strict_json,
        read_file=read,
        directory=lambda p, u, g, m: e.directory(p, os.getuid(), os.getgid(), m),
    )
    ops = e.RootOps()
    events = []
    state = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
        "MemorySwapMax": "0",
        "LimitCORE": "0",
    }
    ops.service = lambda _: dict(state)
    ops.database = lambda: {
        "quick_check": "ok",
        "rows": dict.fromkeys(e.TABLES, 1 if fault == "ledger" else 0),
        "matching_attempts": 0,
        "attempt_states": [],
        "charges": [],
    }
    ops.orderflow = lambda: {"active": True, "http_status": 200, "n_restarts": 0}

    def command(argv):
        events.append(argv)
        assert argv == ["/usr/bin/systemctl", "start", e.SERVICE]
        state.update(ActiveState="active", SubState="running")

    ops.command = command

    def ready(count):
        assert (
            count == 0
            and config.read_bytes() == backup.read_bytes()
            and stat.S_IMODE(config.stat().st_mode) == 0o640
        )
        if fault == "ready":
            raise ValueError("fixture-private-error")

    ops.ready = ready
    ops.stop = lambda: events.append("stop")
    ops.http = lambda method, path: (200, {"targets": []})

    class Anonymous:
        def __init__(self, *a, **k):
            pass

        def request(self, method, path):
            assert method == "GET" and path == "/v1/diagnostics"

        def getresponse(self):
            return SimpleNamespace(status=401)

        def close(self):
            pass

    import http.client

    monkeypatch.setattr(http.client, "HTTPConnection", Anonymous)
    return helper, ops, config, before, events


@pytest.mark.parametrize(
    "fault", [None, "candidate", "posted", "backup", "hardlink", "ledger", "ready"]
)
def test_preserving_recovery_with_real_write_and_umask(tmp_path, monkeypatch, fault):
    helper, ops, config, before, events = fixture(tmp_path, monkeypatch, fault)
    mask = os.umask(0o077)
    try:
        result = r.recover(helper, ops)
    finally:
        os.umask(mask)
    assert result["status"] == ("passed" if fault is None else "failed")
    for p, raw in before.items():
        assert p.read_bytes() == raw
    assert "fixture-private" not in json.dumps(result)
    if fault is None:
        assert (
            result["marker_preserved"]
            and result["backup_preserved"]
            and result["ledger_unchanged"]
            and result["broker_active_enabled"]
        )
        assert (
            config.read_bytes() == b'{"targets":[]}\n'
            and stat.S_IMODE(config.stat().st_mode) == 0o640
        )
        assert events == [["/usr/bin/systemctl", "start", e.SERVICE]]
    elif fault == "ready":
        assert (
            config.read_bytes() == b'{"targets":[]}\n'
            and result["failure_stop_completed"]
            and events[-1] == "stop"
        )
    else:
        assert not events and not result["config_changed"]
