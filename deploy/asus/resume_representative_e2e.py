"""Continue the unused ASUS single POST after verified empty-service recovery.

Default is offline. Preserve all old evidence; fixed exclusive claim prevents
replay. Apply requires the reviewed root TTY unit and existing encrypted client.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

HELPER_SHA = "6254c6ec81688cd462281e52da3fec48a2d55f4ecd7155d4a6c73fcc77581cf9"
OLD_SHA = "0e7bd18cb142a9a793d0fbca9fb54c129b1e17ad76242a6850769138ceee409a"
UNIT = "api-quota-broker-e2e-resume.service"
KEY = "asus-v01-groq-e2e-after-recovery-2026-10-04-once"
BACKUPS = Path("/var/backups/api-quota-broker")
OLD_MARKER = BACKUPS / "representative-groq-once.json"
ORIGINAL = BACKUPS / "e2e-groq-4319ad90692d4221b910b043bf5da0ce/gateway.original.json"
RECOVERY = BACKUPS / "service-recovery-e072ded861ea4717898b07d7ed9c12e4/recovery.json"
CLAIM = BACKUPS / "representative-groq-after-recovery-once.claim.json"
RECEIPT = BACKUPS / "representative-groq-after-recovery-once.json"
RELEASE = "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
UNIT_SHA = "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
CANDIDATE_SHA = "bceef22c74aac230197406969d45bf8fe443c2130df2102270b7057a1a5ae45c"
TABLES = (
    "gateway_tasks",
    "gateway_attempts",
    "reservations",
    "charges",
    "queue_jobs",
    "queue_attempts",
)
EMPTY_LEDGER = {
    "quick_check": "ok",
    "rows": dict.fromkeys(TABLES, 0),
    "matching_attempts": 0,
    "attempt_states": [],
    "charges": [],
}
EXPECTED_RECOVERY = {
    "mode": "asus_empty_service_recovery",
    "status": "passed",
    "phase": "complete",
    "provider_calls": 0,
    "doppler_secret_gets": 0,
    "automatic_retry": False,
    "started_at": "2026-10-04T02:18:50.672903+00:00",
    "completed_at": "2026-10-04T02:18:54.057214+00:00",
    "marker_sha256": OLD_SHA,
    "current_config": {
        "mode": 384,
        "uid": 0,
        "gid": 982,
        "inode": 2098918,
        "sha256": CANDIDATE_SHA,
    },
    "ledger_before": EMPTY_LEDGER,
    "journal": str(RECOVERY),
    "preserved_candidate": str(RECOVERY.parent / "gateway.failed.json"),
    "original_config_restored": True,
    "broker_active_enabled": True,
    "empty_authenticated_targets": True,
    "anonymous_status": 401,
    "marker_preserved": True,
    "backup_preserved": True,
    "ledger_unchanged": True,
    "orderflow_unchanged": True,
    "config_changed": True,
}


class ResumeError(ValueError):
    pass


PREFLIGHT_GATES = {
    "preserved_history",
    "exclusive_resume_absent",
    "installed_source",
    "original_empty_config",
    "disabled_template",
    "doppler_metadata",
    "empty_service",
    "client_credential",
    "authenticated_empty_targets",
}


def require(value):
    if not value:
        raise ResumeError


def same_json(value, expected):
    # Python equality alone accepts True == 1; receipt types are part of the gate.
    return json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(
        expected, sort_keys=True, allow_nan=False
    )


def fingerprint(path, raw):
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_size,
        hashlib.sha256(raw).hexdigest(),
    )


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def service_can_read(path, expected_sha):
    """Actually open/read as the service UID with only its primary group."""
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(982)
            os.setuid(995)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode))
                require(
                    (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink)
                    == (0, 982, 0o640, 1)
                )
                require(0 < info.st_size <= 32768)
                raw = stream.read(32769)
                require(
                    len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == expected_sha
                )
            os._exit(0)
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - child emits only exit status
            os._exit(1)
    _, status = os.waitpid(child, 0)
    require(status == 0)


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
        0 < path.stat().st_size <= 128 * 1024
        and hashlib.sha256(path.read_bytes()).hexdigest() == HELPER_SHA
    )
    spec = importlib.util.spec_from_file_location("reviewed_resume_ops", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def build_ops(helper):
    # Private module instance: reuse fixed HTTP/ledger/projection contracts with
    # the new task identity. Its legacy preflight/claim/save/run are never used.
    helper.KEY = KEY
    helper.TASK = {**helper.TASK, "request_key": KEY}

    class ResumeOps(helper.RootOps):
        def __init__(self):
            super().__init__()
            self.history = {}
            self.receipt_stamp = None

        def service(self, name):
            raw = self.command(
                [
                    "/usr/bin/systemctl",
                    "show",
                    name,
                    "--property=ActiveState,SubState,UnitFileState,NRestarts,User,Group,MemoryHigh,MemoryMax,MemorySwapMax,LimitCORE,MainPID,ExecMainStartTimestampMonotonic",
                ]
            )
            return dict(row.split("=", 1) for row in raw.decode().splitlines())

        def orderflow(self):
            result = super().orderflow()
            service = self.service("orderflow.service")
            require(
                service["SubState"] == "running"
                and service["MainPID"] == "172058"
                and service["ExecMainStartTimestampMonotonic"] == "515137840877"
                and service["NRestarts"] == "0"
            )
            return {**result, "pid": 172058, "start_monotonic": 515137840877}

        def history_gate(self, initial=False):
            for directory in (BACKUPS, ORIGINAL.parent, RECOVERY.parent):
                helper.directory(directory, 0, 0, 0o700)
            files = (
                (OLD_MARKER, OLD_SHA),
                (ORIGINAL, helper.EMPTY_SHA),
                (RECOVERY, None),
                (RECOVERY.parent / "gateway.failed.json", CANDIDATE_SHA),
            )
            observed = {}
            for path, sha in files:
                raw = helper.read_file(path, 0, 0, 0o600, 32768)
                if sha is not None:
                    require(helper.digest(raw) == sha)
                if path == OLD_MARKER:
                    value = helper.strict_json(raw)
                    require(
                        value.get("post_attempted") is False
                        and "result" not in value
                        and value.get("original_config_backup") == str(ORIGINAL)
                    )
                elif path == RECOVERY:
                    require(same_json(helper.strict_json(raw), EXPECTED_RECOVERY))
                elif path == ORIGINAL:
                    require(raw == b'{"targets":[]}\n')
                observed[str(path)] = fingerprint(path, raw)
            if initial:
                self.history = observed
            else:
                require(observed == self.history)
            return observed

        def preflight(self):
            self.gate = "preserved_history"
            self.history_gate(initial=True)
            self.gate = "exclusive_resume_absent"
            for path in (CLAIM, RECEIPT):
                helper.real_path(path)
                require(not path.exists() and not path.is_symlink())
            self.gate = "installed_source"
            require(os.readlink("/opt/api-quota-broker/current") == RELEASE)
            require(
                helper.digest(
                    helper.read_file(
                        Path("/etc/systemd/system/api-quota-broker.service"), 0, 0, 0o644, 32768
                    )
                )
                == UNIT_SHA
            )
            self.gate = "original_empty_config"
            helper.directory(helper.CONFIG.parent, 0, 982, 0o750)
            raw = helper.read_file(helper.CONFIG, 0, 982, 0o640, 8192)
            require(helper.digest(raw) == helper.EMPTY_SHA and raw == b'{"targets":[]}\n')
            self.original = raw
            self.config_stamp = fingerprint(helper.CONFIG, raw)
            self.gate = "disabled_template"
            raw = helper.read_file(
                Path(__file__).absolute().parent / "representative_e2e.disabled.json",
                0,
                0,
                0o600,
                32768,
            )
            require(helper.digest(raw) == helper.TEMPLATE_SHA)
            self.template = helper.strict_json(raw)
            self.gate = "doppler_metadata"
            metadata = helper.strict_json(
                helper.read_file(
                    helper.CONFIG.parent / "credentials/doppler-metadata.json", 0, 0, 0o600, 8192
                )
            )
            require(
                all(
                    same_json(metadata.get(k), v)
                    for k, v in {
                        "project": "api-quota-broker",
                        "config": "dev",
                        "access": "read",
                        "credential_policy": "host",
                        "human_dashboard_attested": True,
                    }.items()
                )
            )
            expiry = datetime.fromisoformat(metadata["expires_at"])
            require(expiry.tzinfo is not None and expiry > datetime.now(UTC) + timedelta(minutes=5))
            self.gate = "empty_service"
            service = self.service(helper.SERVICE)
            require(
                all(
                    service.get(k) == v
                    for k, v in {
                        "ActiveState": "active",
                        "SubState": "running",
                        "UnitFileState": "enabled",
                        "User": "api-quota-broker",
                        "Group": "api-quota-broker",
                        "NRestarts": "0",
                        "MemoryHigh": "268435456",
                        "MemoryMax": "402653184",
                        "MemorySwapMax": "0",
                        "LimitCORE": "0",
                    }.items()
                )
            )
            self.before_orderflow = self.orderflow()
            require(self.database() == EMPTY_LEDGER)
            self.gate = "client_credential"
            require(os.environ.get("CREDENTIALS_DIRECTORY") == "/run/credentials/" + UNIT)
            raw = helper.read_file(
                Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token", 0, 0, 0o400, 256
            )
            self.client = raw.decode("ascii").strip()
            require(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.client))
            self.gate = "authenticated_empty_targets"
            require(not self.targets() and self.database() == EMPTY_LEDGER)
            service_can_read(helper.CONFIG, helper.EMPTY_SHA)
            self.history_gate()
            return {
                "empty_authenticated_targets": True,
                "empty_ledger": True,
                "old_post_attempted": False,
                "recovery_passed": True,
                "history": self.history,
            }

        def claim(self, record):
            self.history_gate()
            require(
                fingerprint(helper.CONFIG, helper.read_file(helper.CONFIG, 0, 982, 0o640, 8192))
                == self.config_stamp
            )
            # The immutable claim remains even if receipt creation/save fails.
            payload = {
                "mode": "asus_recovered_groq_once_claim",
                "request_key": KEY,
                "history": self.history,
                "provider_posts_max": 1,
                "automatic_retry": False,
            }
            self.write(CLAIM, json.dumps(payload, sort_keys=True).encode(), 0o600, 0)
            self.claimed = True
            sync_directory(BACKUPS)
            self.claim_stamp = fingerprint(CLAIM, helper.read_file(CLAIM, 0, 0, 0o600, 32768))
            self.write(RECEIPT, b"{}", 0o600, 0)
            sync_directory(BACKUPS)
            self.receipt_stamp = fingerprint(RECEIPT, b"{}")
            record["original_config_backup"] = str(ORIGINAL)
            record["claim"] = str(CLAIM)
            record["history"] = self.history
            self.save(record)

        def save(self, record):
            require(self.receipt_stamp is not None)
            current = helper.read_file(RECEIPT, 0, 0, 0o600, 65536)
            require(fingerprint(RECEIPT, current) == self.receipt_stamp)
            temporary = BACKUPS / (".groq-resume-receipt-" + uuid.uuid4().hex)
            raw = json.dumps(record, sort_keys=True, allow_nan=False).encode()
            self.write(temporary, raw, 0o600, 0)
            require(
                fingerprint(RECEIPT, helper.read_file(RECEIPT, 0, 0, 0o600, 65536))
                == self.receipt_stamp
            )
            os.replace(temporary, RECEIPT)
            self.receipt_stamp = fingerprint(RECEIPT, raw)
            sync_directory(BACKUPS)

        def replace_config(self, raw):
            current = helper.read_file(helper.CONFIG, 0, 982, 0o640, 32768)
            require(fingerprint(helper.CONFIG, current) == self.config_stamp)
            temporary = helper.CONFIG.parent / (".groq-resume-config-" + uuid.uuid4().hex)
            self.write(temporary, raw, 0o640, 982)
            require(helper.read_file(temporary, 0, 982, 0o640, 32768) == raw)
            service_can_read(temporary, helper.digest(raw))
            require(
                fingerprint(helper.CONFIG, helper.read_file(helper.CONFIG, 0, 982, 0o640, 32768))
                == self.config_stamp
            )
            os.replace(temporary, helper.CONFIG)
            self.config_stamp = fingerprint(helper.CONFIG, raw)
            sync_directory(helper.CONFIG.parent)
            require(helper.read_file(helper.CONFIG, 0, 982, 0o640, 32768) == raw)

        def post(self):
            self.history_gate()
            require(
                fingerprint(CLAIM, helper.read_file(CLAIM, 0, 0, 0o600, 32768)) == self.claim_stamp
            )
            require(
                fingerprint(helper.CONFIG, helper.read_file(helper.CONFIG, 0, 982, 0o640, 32768))
                == self.config_stamp
            )
            receipt = helper.strict_json(helper.read_file(RECEIPT, 0, 0, 0o600, 65536))
            require(
                receipt.get("post_attempted") is True
                and receipt.get("phase") == "post"
                and receipt.get("history") == json.loads(json.dumps(self.history))
            )
            require(self.database() == EMPTY_LEDGER and self.orderflow() == self.before_orderflow)
            require(
                datetime.fromisoformat(self.template["targets"][0]["expires_at"])
                > datetime.now(UTC) + timedelta(seconds=45)
            )
            return super().post()

        def restore(self):
            # Preserve an administrator's foreign config; only our exact inode
            # and bytes, or the original unchanged config, can be replaced.
            raw = helper.read_file(helper.CONFIG, 0, 982, 0o640, 32768)
            require(fingerprint(helper.CONFIG, raw) == self.config_stamp)
            require(helper.digest(raw) in {helper.EMPTY_SHA, self.temporary_sha})
            original = helper.read_file(ORIGINAL, 0, 0, 0o600, 8192)
            require(
                helper.digest(original) == helper.EMPTY_SHA
                and fingerprint(ORIGINAL, original) == self.history[str(ORIGINAL)]
            )
            self.replace_config(original)
            self.command(["/usr/bin/systemctl", "restart", helper.SERVICE])
            self.ready(0)
            service = self.service(helper.SERVICE)
            require(
                service["ActiveState"] == "active"
                and service["SubState"] == "running"
                and service["UnitFileState"] == "enabled"
                and service["MemorySwapMax"] == "0"
                and service["LimitCORE"] == "0"
            )
            require(self.orderflow() == self.before_orderflow)
            self.history_gate()
            require(
                fingerprint(CLAIM, helper.read_file(CLAIM, 0, 0, 0o600, 32768)) == self.claim_stamp
            )
            return {
                "empty_targets": True,
                "active_enabled": True,
                "orderflow_unchanged": True,
                "history_preserved": True,
                "claim_preserved": True,
                "ledger": self.database(),
            }

    return ResumeOps


def run(helper, ops):
    record = {
        "mode": "asus_recovered_representative_groq_once",
        "status": "failed",
        "phase": "preflight",
        "post_attempted": False,
        "provider_posts_max": 1,
        "automatic_retry": False,
        "started_at": helper.stamp(),
        "free_confirmation_reused": True,
    }
    completed = False
    try:
        record["preflight"] = ops.preflight()
        record["phase"] = "claim"
        ops.claim(record)
        record["phase"] = "temporary_target"
        ops.save(record)
        ops.enable()
        record["phase"] = "post"
        record["post_attempted"] = True
        ops.save(record)
        safe = helper.projected(ops.post())
        record["result"] = safe
        record["phase"] = "ledger_verify"
        ops.save(record)
        metadata = ops.status()
        ledger = ops.database()
        record["ledger_before_restore"] = ledger
        completed = (
            safe["full_answer_verified"]
            and metadata["state"] == metadata["ledger_state"] == "completed"
            and all(
                metadata[k] == safe[k]
                for k in (
                    "reported_input_tokens",
                    "reported_output_tokens",
                    "finish_reason",
                    "response_truncated",
                    "ledger_basis",
                )
            )
            and ledger["matching_attempts"] == 1
            and ledger["attempt_states"] == ["completed"]
            and ledger["rows"] == dict(zip(TABLES, (1, 1, 1, 3, 0, 0), strict=True))
            and sorted((c["metric"], c["amount"]) for c in ledger["charges"])
            == sorted(
                [("requests", 1), ("requests", 1), ("input_tokens", safe["reported_input_tokens"])]
            )
        )
        if not completed:
            record["reason"] = "full_answer_or_ledger_not_verified"
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - no raw/secret error text
        record["failure_phase"] = record["phase"]
        if record["phase"] == "preflight":
            record["failure_gate"] = ops.gate if ops.gate in PREFLIGHT_GATES else "unclassified"
        record["reason"] = "resume_gate_failed"
    finally:
        if ops.changed:
            try:
                # On a POST/status error still compare ledger before/after
                # restore, without clearing holds or changing the verdict.
                try:
                    ledger_before = ops.database()
                except Exception:  # noqa: BLE001 - ledger error must not prevent config restoration
                    ledger_before = None
                record["restoration"] = ops.restore()
                require(record["restoration"]["ledger"] == ledger_before)
                if "ledger_before_restore" in record:
                    require(record["ledger_before_restore"] == ledger_before)
                record["restored_original_config"] = True
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - preserve evidence, stop safely
                completed = False
                record["restored_original_config"] = False
                record["reason"] = "restoration_failed_operator_review_required"
                try:
                    ops.stop()
                    record["failure_stop_completed"] = True
                except Exception:  # noqa: BLE001 - fixed outcome only
                    record["failure_stop_completed"] = False
        record.update(
            status="passed" if completed else "failed",
            phase="complete" if completed else "failed",
            completed_at=helper.stamp(),
        )
        if ops.claimed:
            try:
                ops.save(record)
                record["journal"] = str(RECEIPT)
            except Exception:  # noqa: BLE001 - no retry of provider or receipt
                record.update(
                    status="failed", reason="receipt_save_failed_operator_review_required"
                )
    return record


def main():
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--apply", action="store_true")
        args = parser.parse_args()
        if not args.apply:
            result = {
                "mode": "asus_recovered_e2e_plan",
                "provider": "groq",
                "model": "openai/gpt-oss-20b",
                "request_key": KEY,
                "provider_posts_max": 1,
                "doppler_secret_gets_max": 1,
                "new_tokens": 0,
                "provider_models_gets": 0,
                "retries": 0,
                "max_output_tokens": 512,
                "seconds": 30,
                "preserve_history": True,
                "actual_provider_calls": 0,
                "actual_credential_reads": 0,
                "actual_service_changes": 0,
            }
        else:
            os.umask(0o077)
            helper = load_helper()
            result = run(helper, build_ops(helper)())
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed environment gate only
        result = {
            "status": "failed",
            "reason": "root_environment_or_helper_integrity",
            "provider_calls": 0,
        }
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
