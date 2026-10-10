"""Preserve a failed probe and restore its verified original empty service config.

Default is offline. Apply is root TTY only and never dispatches a provider task.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import resource
import socket
import stat
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HELPER_SHA = "6254c6ec81688cd462281e52da3fec48a2d55f4ecd7155d4a6c73fcc77581cf9"
BACKUP = Path(
    "/var/backups/api-quota-broker/e2e-groq-4319ad90692d4221b910b043bf5da0ce/gateway.original.json"
)
UNIT = "api-quota-broker-e2e-recovery.service"


class RecoveryError(ValueError):
    pass


def require(value):
    if not value:
        raise RecoveryError


def fingerprint(path, raw):
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_size,
        hashlib.sha256(raw).hexdigest(),
    )


def candidate_valid(value, template, failed):
    """Allow exactly v3's attested candidate, or the original empty config."""
    if value == {"targets": []}:
        return True
    target = value["targets"][0]
    verified = datetime.fromisoformat(target["verified_at"])
    expiry = datetime.fromisoformat(target["expires_at"])
    started = datetime.fromisoformat(failed["started_at"])
    completed = datetime.fromisoformat(failed["completed_at"])
    require(
        all(
            t.tzinfo is not None and t.utcoffset() == timedelta(0)
            for t in (verified, expiry, started, completed)
        )
    )
    require(started <= verified <= completed and expiry - verified == timedelta(minutes=5))
    expected = json.loads(json.dumps(template))
    expected["targets"][0].update(
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=target["verified_at"],
        expires_at=target["expires_at"],
        source="human current Groq Free/billing attestation for bounded ASUS probe",
    )
    return value == expected


def load_helper():
    require(os.geteuid() == 0 and socket.gethostname() == "asus-ubuntu2604-server")
    require(sys.stdin.isatty() and resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))
    require(Path("/proc/self/cgroup").read_bytes().strip() == ("0::/system.slice/" + UNIT).encode())
    group = Path("/sys/fs/cgroup/system.slice") / UNIT
    require((group / "memory.max").read_bytes().strip() == b"134217728")
    require((group / "memory.swap.max").read_bytes().strip() == b"0")
    cpu = (group / "cpu.max").read_bytes().split()
    require(len(cpu) == 2 and int(cpu[0]) * 2 == int(cpu[1]))
    private = Path(__file__).absolute().parent
    for path, directory, mode in (
        (private, True, 0o700),
        (Path(__file__).absolute(), False, 0o600),
        (private / "representative_e2e.py", False, 0o600),
    ):
        for parent in (*reversed(path.parents), path):
            require(not parent.is_symlink())
        info = path.lstat()
        require(info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == mode)
        require(
            stat.S_ISDIR(info.st_mode)
            if directory
            else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        )
    path = private / "representative_e2e.py"
    require(
        path.stat().st_size <= 128 * 1024
        and hashlib.sha256(path.read_bytes()).hexdigest() == HELPER_SHA
    )
    spec = importlib.util.spec_from_file_location("reviewed_service_recovery_ops", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def recover(helper, ops):
    record = {
        "mode": "asus_empty_service_recovery",
        "status": "failed",
        "phase": "source_gate",
        "provider_calls": 0,
        "doppler_secret_gets": 0,
        "automatic_retry": False,
        "started_at": helper.stamp(),
    }
    journal = None
    start_attempted = False
    config_changed = False

    def save():
        temporary = helper.BACKUPS / (".service-recovery-" + uuid.uuid4().hex)
        ops.write(temporary, json.dumps(record, sort_keys=True, allow_nan=False).encode(), 0o600, 0)
        os.replace(temporary, journal)
        fd = os.open(helper.BACKUPS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    try:
        helper.directory(helper.CONFIG.parent, 0, 982, 0o750)
        helper.directory(helper.BACKUPS, 0, 0, 0o700)
        helper.directory(BACKUP.parent, 0, 0, 0o700)
        unit = Path("/etc/systemd/system/api-quota-broker.service")
        require(
            helper.digest(helper.read_file(unit, 0, 0, 0o644, 32768))
            == "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
        )
        require(
            os.readlink("/opt/api-quota-broker/current")
            == "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
        )
        record["phase"] = "failed_probe_receipt"
        marker = helper.read_file(helper.MARKER, 0, 0, 0o600, 16384)
        failed = helper.strict_json(marker)
        require(
            all(
                failed.get(k) == v
                for k, v in {
                    "mode": "asus_representative_groq_once",
                    "status": "failed",
                    "phase": "failed",
                    "failure_phase": "temporary_target",
                    "reason": "restoration_failed_operator_review_required",
                    "post_attempted": False,
                    "automatic_retry": False,
                    "restored_original_config": False,
                    "failure_stop_completed": True,
                    "original_config_backup": str(BACKUP),
                    "preflight": {"empty_authenticated_targets": True, "empty_ledger": True},
                }.items()
            )
        )
        require(failed.get("post_attempted") is False and "result" not in failed)
        require(type(failed.get("provider_posts_max")) is int and failed["provider_posts_max"] == 1)
        marker_stamp = fingerprint(helper.MARKER, marker)
        record["marker_sha256"] = helper.digest(marker)
        record["phase"] = "original_backup"
        original = helper.read_file(BACKUP, 0, 0, 0o600, 8192)
        require(
            helper.digest(original) == helper.EMPTY_SHA
            and helper.strict_json(original) == {"targets": []}
        )
        backup_stamp = fingerprint(BACKUP, original)
        record["phase"] = "current_candidate"
        info = helper.CONFIG.lstat()
        mode = stat.S_IMODE(info.st_mode)
        require(mode in {0o600, 0o640})
        current = helper.read_file(helper.CONFIG, 0, 982, mode, 32768)
        template_raw = helper.read_file(
            Path(__file__).absolute().parent / "representative_e2e.disabled.json",
            0,
            0,
            0o600,
            32768,
        )
        require(helper.digest(template_raw) == helper.TEMPLATE_SHA)
        require(
            candidate_valid(helper.strict_json(current), helper.strict_json(template_raw), failed)
        )
        if helper.strict_json(current) == {"targets": []}:
            require(helper.digest(current) == helper.EMPTY_SHA)
        current_stamp = fingerprint(helper.CONFIG, current)
        record["current_config"] = {
            "mode": mode,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "inode": info.st_ino,
            "sha256": helper.digest(current),
        }
        record["phase"] = "stopped_service_and_ledger"
        before = ops.service(helper.SERVICE)
        require(
            before["ActiveState"] == "inactive"
            and before["SubState"] == "dead"
            and before["UnitFileState"] == "enabled"
        )
        ledger = ops.database()
        require(not any(ledger["rows"].values()))
        record["ledger_before"] = ledger
        orderflow = ops.orderflow()
        record["phase"] = "client_credential"
        require(os.environ.get("CREDENTIALS_DIRECTORY") == "/run/credentials/" + UNIT)
        raw = helper.read_file(
            Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token", 0, 0, 0o400, 256
        )
        ops.client = raw.decode("ascii").strip()
        require(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", ops.client))
        record["phase"] = "preserve_current"
        evidence = helper.BACKUPS / ("service-recovery-" + uuid.uuid4().hex)
        evidence.mkdir(mode=0o700)
        ops.write(evidence / "gateway.failed.json", current, 0o600, 0)
        journal = evidence / "recovery.json"
        ops.write(journal, b"{}", 0o600, 0)
        record["journal"] = str(journal)
        record["preserved_candidate"] = str(evidence / "gateway.failed.json")
        save()
        record["phase"] = "restore_original_config"
        temporary = helper.CONFIG.parent / (".service-recovery-config-" + uuid.uuid4().hex)
        ops.write(temporary, original, 0o640, 982)
        require(helper.read_file(temporary, 0, 982, 0o640, 8192) == original)
        require(
            fingerprint(helper.CONFIG, helper.read_file(helper.CONFIG, 0, 982, mode, 32768))
            == current_stamp
        )
        require(
            fingerprint(helper.MARKER, helper.read_file(helper.MARKER, 0, 0, 0o600, 16384))
            == marker_stamp
        )
        require(fingerprint(BACKUP, helper.read_file(BACKUP, 0, 0, 0o600, 8192)) == backup_stamp)
        os.replace(temporary, helper.CONFIG)
        config_changed = True
        fd = os.open(helper.CONFIG.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        require(helper.read_file(helper.CONFIG, 0, 982, 0o640, 8192) == original)
        record["original_config_restored"] = True
        record["phase"] = "start_empty_service"
        start_attempted = True
        save()
        ops.command(["/usr/bin/systemctl", "start", helper.SERVICE])
        ops.ready(0)
        record["phase"] = "acceptance"
        service = ops.service(helper.SERVICE)
        require(
            service["ActiveState"] == "active"
            and service["SubState"] == "running"
            and service["UnitFileState"] == "enabled"
            and service["MemorySwapMax"] == "0"
            and service["LimitCORE"] == "0"
        )
        status, data = ops.http("GET", "/v1/diagnostics")
        require(status == 200 and data.get("targets") == [])
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=5)
        try:
            connection.request("GET", "/v1/diagnostics")
            require(connection.getresponse().status == 401)
        finally:
            connection.close()
        require(ops.database() == ledger and ops.orderflow() == orderflow)
        require(
            fingerprint(helper.MARKER, helper.read_file(helper.MARKER, 0, 0, 0o600, 16384))
            == marker_stamp
        )
        require(fingerprint(BACKUP, helper.read_file(BACKUP, 0, 0, 0o600, 8192)) == backup_stamp)
        record.update(
            status="passed",
            phase="complete",
            broker_active_enabled=True,
            empty_authenticated_targets=True,
            anonymous_status=401,
            marker_preserved=True,
            backup_preserved=True,
            ledger_unchanged=True,
            orderflow_unchanged=True,
        )
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - no credential/raw error text
        record.update(
            failure_gate=record["phase"], phase="failed", reason="service_recovery_gate_failed"
        )
        if start_attempted:
            try:
                ops.stop()
                record["failure_stop_completed"] = True
            except Exception:  # noqa: BLE001 - fixed outcome only
                record["failure_stop_completed"] = False
    finally:
        record["config_changed"] = config_changed
        record["completed_at"] = helper.stamp()
        if journal is not None:
            try:
                save()
            except Exception:  # noqa: BLE001 - preserve service outcome separately
                record["recovery_journal_save_failed"] = True
    return record


def main():
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args()
        if not args.apply:
            result = {
                "mode": "service_recovery_plan",
                "restore_original_empty_config": True,
                "preserve_marker_backup_ledger": True,
                "actual_provider_calls": 0,
                "actual_credential_reads": 0,
                "actual_service_changes": 0,
            }
        else:
            os.umask(0o077)
            helper = load_helper()
            result = recover(helper, helper.RootOps())
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed outer gate only
        result = {
            "status": "failed",
            "failure_gate": "root_environment_or_helper_integrity",
            "provider_calls": 0,
        }
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
