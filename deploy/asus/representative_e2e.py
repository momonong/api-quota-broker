"""One reviewed ASUS Gateway E2E; default is an offline, zero-effect plan.

Live requires root trueTTY, a current Free/billing attestation, encrypted client
credential, empty config/ledger, independent script/template hashes and approval.
Never prints answer/raw response/secrets. A permanent receipt prohibits replay.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import resource
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SERVICE = "api-quota-broker.service"
SELF_UNIT = "api-quota-broker-e2e.service"
CONFIG = Path("/etc/api-quota-broker/gateway.json")
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
BACKUPS = Path("/var/backups/api-quota-broker")
MARKER = BACKUPS / "representative-groq-once.json"
EMPTY_SHA = "a758f0791313b284d4e230a4d5f162fb002bbb9c0e814277ddc24b07ace36cb2"
TEMPLATE_SHA = "019bc66bc21424ed5372622c6c58e63a2ba674a9a519fdf1526409fc1e897e56"
MODEL = "openai/gpt-oss-20b"
KEY = "asus-v01-groq-e2e-2026-10-04-once"
TASK = {
    "request_key": KEY,
    "provider": "groq",
    "model": MODEL,
    "capability": "text_generation",
    "input": "Reply with exactly READY.",
    "max_output_tokens": 512,
    "max_attempts": 1,
}
STATES = {
    "completed",
    "completed_usage_unknown",
    "unknown",
    "pre_send_failed",
    "quota_rejected",
    "rejected",
    "dispatched",
    "reserved",
    "released",
    "expired",
    "failed",
}
ERRORS = {
    "secret_unavailable",
    "provider_http_error",
    "timeout_before_headers",
    "timeout_response_body",
    "credential_format_rejected",
    "provider_response_invalid",
    "unavailable",
    "quota_exhausted",
    "invalid_request",
    "internal_error",
}
TABLES = (
    "gateway_tasks",
    "gateway_attempts",
    "reservations",
    "charges",
    "queue_jobs",
    "queue_attempts",
)


PREFLIGHT_GATES = {
    "template_hash",
    "once_marker_absent",
    "true_tty",
    "bootstrap_directory",
    "root_uid",
    "empty_ledger",
    "swap_max",
    "client_credential_metadata",
    "credential_directory_environment",
    "release_selector",
    "installed_unit_hash",
    "installed_unit_file",
    "helper_file",
    "broker_service",
    "config_directory",
    "doppler_metadata_file",
    "host_identity",
    "self_cgroup",
    "template_file",
    "orderflow",
    "empty_authenticated_targets",
    "client_credential_read",
    "cpu_quota",
    "doppler_metadata_expiry",
    "core_limits",
    "metadata_diagnosis_complete",
    "empty_config_file",
    "doppler_metadata_scope",
    "client_credential_format",
    "memory_max",
    "backups_directory",
    "empty_config_hash",
}


class GateError(ValueError):
    pass


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise GateError
            value[key] = item
        return value

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(GateError())
    )


def real_path(path):
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise GateError


def read_file(path, uid, gid, mode, bound):
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink)
            != (uid, gid, mode, 1)
            or not 0 < info.st_size <= bound
        ):
            raise GateError
        raw = stream.read(bound + 1)
        if len(raw) != info.st_size:
            raise GateError
        return raw


def directory(path, uid, gid, mode):
    real_path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
        uid,
        gid,
        mode,
    ):
        raise GateError


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp():
    return datetime.now(UTC).isoformat()


def projected(result):
    """Only fixed enums, bounded numbers and booleans cross the secret boundary."""
    if not isinstance(result, dict):
        raise GateError
    if any(
        result.get(k) != v
        for k, v in {"provider": "groq", "model": MODEL, "request_key": KEY}.items()
    ):
        raise GateError
    safe = {"provider": "groq", "model": MODEL, "request_key": KEY}
    state = result.get("state")
    safe["state"] = state if state in STATES else "unclassified"
    code = result.get("error_code")
    safe["error_code"] = code if code in ERRORS else "unclassified" if code else None
    for key, upper in (
        ("http_status", 599),
        ("reported_input_tokens", 1024),
        ("reported_output_tokens", 512),
        ("latency_ms", 120000),
    ):
        value = result.get(key)
        safe[key] = value if type(value) is int and 0 <= value <= upper else None
    safe["finish_reason"] = (
        result.get("finish_reason")
        if result.get("finish_reason") in {"stop", "length", "content_filter", "tool_calls"}
        else None
    )
    safe["response_truncated"] = (
        result.get("response_truncated") if type(result.get("response_truncated")) is bool else None
    )
    safe["ledger_state"] = (
        result.get("ledger_state") if result.get("ledger_state") in STATES else "unclassified"
    )
    safe["ledger_basis"] = (
        result.get("ledger_basis")
        if result.get("ledger_basis")
        in {"settled_provider_usage", "held_estimate", "rejected_zero_usage", "unsent"}
        else "unclassified"
    )
    answer = result.get("answer")
    safe["visible_answer_present"] = isinstance(answer, str) and bool(answer.strip())
    safe["ready_exact_match"] = isinstance(answer, str) and answer.strip() == "READY"
    safe["full_answer_verified"] = (
        safe["state"] == "completed"
        and safe["http_status"] == 200
        and safe["finish_reason"] == "stop"
        and safe["response_truncated"] is False
        and safe["visible_answer_present"]
        and safe["ledger_state"] == "completed"
        and safe["ledger_basis"] == "settled_provider_usage"
        and all(safe[k] is not None for k in ("reported_input_tokens", "reported_output_tokens"))
    )
    return safe


class RootOps:
    def __init__(self):
        self.changed = False
        self.original = None
        self.temporary_sha = None
        self.client = None
        self.claimed = False
        self.gate = "root_uid"
        self.observations = {}

    def command(self, argv):
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout

    def service(self, name):
        raw = self.command(
            [
                "/usr/bin/systemctl",
                "show",
                name,
                "--property=ActiveState,SubState,UnitFileState,NRestarts,User,Group,MemorySwapMax,LimitCORE",
            ]
        )
        return dict(row.split("=", 1) for row in raw.decode().splitlines())

    def http(self, method, path, body=None):
        if not (
            method == "GET"
            and path in {"/v1/diagnostics", "/v1/tasks/" + KEY}
            and body is None
            or method == "POST"
            and path == "/v1/tasks"
            and body == TASK
        ):
            raise GateError
        limit = 45 if method == "POST" else 5
        deadline = time.monotonic() + limit
        connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=limit)
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body).encode() if body else None,
                headers={
                    "Authorization": "Bearer " + self.client,
                    "Content-Type": "application/json",
                },
            )
            wire = connection.sock
            response = connection.getresponse()
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateError
                # A complete response may close the last socket reference inside read1.
                # Test framing completion before touching that saved socket again.
                if response.isclosed():
                    if response.length not in (None, 0):
                        raise GateError
                    break
                if wire is not None:
                    wire.settimeout(remaining)
                chunk = response.read1(min(16384, 262145 - size))
                if not chunk:
                    if response.length not in (None, 0):
                        raise GateError
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 262144:
                    raise GateError
            return response.status, strict_json(b"".join(chunks))
        finally:
            connection.close()

    def targets(self):
        status, value = self.http("GET", "/v1/diagnostics")
        if (
            status != 200
            or not isinstance(value, dict)
            or not isinstance(value.get("targets"), list)
        ):
            raise GateError
        return value["targets"]

    def database(self):
        real_path(DB)
        info = DB.lstat()
        if not stat.S_ISREG(info.st_mode) or (
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            info.st_nlink,
        ) != (995, 982, 0o600, 1):
            raise GateError
        with sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as con:
            if con.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise GateError
            counts = {
                name: con.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in TABLES
            }
            rows = con.execute(
                "SELECT provider,state,http_status,reported_input_tokens,reported_output_tokens FROM gateway_attempts WHERE request_key=?",
                (KEY,),
            ).fetchall()
            charges = con.execute(
                "SELECT c.metric,c.amount FROM charges c JOIN gateway_attempts a ON a.reservation_id=c.reservation_id WHERE a.request_key=? ORDER BY c.bucket",
                (KEY,),
            ).fetchall()
        if any(row[0] != "groq" or row[1] not in STATES for row in rows):
            raise GateError
        if any(
            metric not in {"requests", "input_tokens"}
            or type(amount) is not int
            or not 0 <= amount <= 1024
            for metric, amount in charges
        ):
            raise GateError
        return {
            "quick_check": "ok",
            "rows": counts,
            "matching_attempts": len(rows),
            "attempt_states": [r[1] for r in rows],
            "charges": [{"metric": metric, "amount": amount} for metric, amount in charges],
        }

    def orderflow(self):
        value = self.service("orderflow.service")
        if value["ActiveState"] != "active" or not value["NRestarts"].isdigit():
            raise GateError
        connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=5)
        try:
            connection.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            if connection.getresponse().status != 200:
                raise GateError
        finally:
            connection.close()
        return {"active": True, "n_restarts": int(value["NRestarts"]), "http_status": 200}

    def observe_file(self, label, path):
        info = path.lstat()
        self.observations[label] = {
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
            "nlink": info.st_nlink,
            "size": info.st_size,
            "regular": stat.S_ISREG(info.st_mode),
            "directory": stat.S_ISDIR(info.st_mode),
            "symlink": stat.S_ISLNK(info.st_mode),
        }

    def diagnostic_snapshot(self):
        """Independent nonsecret state checks, even if an earlier environment gate failed."""
        snapshot = {}
        if os.geteuid() != 0 or socket.gethostname() != "asus-ubuntu2604-server":
            return {"root_host_verified": False}
        try:
            original = read_file(CONFIG, 0, 982, 0o640, 8192)
            snapshot["original_empty_config_hash_matches"] = digest(original) == EMPTY_SHA
        except Exception:  # noqa: BLE001 - fixed metadata only
            snapshot["config_metadata_check_failed"] = True
        try:
            real_path(MARKER)
            snapshot["once_marker_absent"] = not MARKER.exists() and not MARKER.is_symlink()
        except Exception:  # noqa: BLE001 - no marker contents are read
            snapshot["marker_metadata_check_failed"] = True
        try:
            snapshot["ledger"] = self.database()
        except Exception:  # noqa: BLE001 - no free error text
            snapshot["ledger_metadata_check_failed"] = True
        return snapshot

    def preflight(self, *, diagnosis=False):
        self.gate = "root_uid"
        if os.geteuid() != 0:
            raise GateError
        self.gate = "host_identity"
        if socket.gethostname() != "asus-ubuntu2604-server":
            raise GateError
        self.gate = "true_tty"
        if not sys.stdin.isatty():
            raise GateError
        self.gate = "core_limits"
        self.observations["core_limits"] = list(resource.getrlimit(resource.RLIMIT_CORE))
        if self.observations["core_limits"] != [0, 0]:
            raise GateError
        self.gate = "self_cgroup"
        if (
            Path("/proc/self/cgroup").read_bytes().strip()
            != b"0::/system.slice/api-quota-broker-e2e.service"
        ):
            raise GateError
        group = Path("/sys/fs/cgroup/system.slice") / SELF_UNIT
        self.gate = "memory_max"
        memory = (group / "memory.max").read_bytes().strip()
        self.observations["memory_max_matches"] = memory == b"134217728"
        if memory != b"134217728":
            raise GateError
        self.gate = "swap_max"
        if (group / "memory.swap.max").read_bytes().strip() != b"0":
            raise GateError
        self.gate = "cpu_quota"
        cpu = (group / "cpu.max").read_bytes().split()
        if len(cpu) != 2 or int(cpu[0]) * 2 != int(cpu[1]):
            raise GateError
        private = Path(__file__).absolute().parent
        self.gate = "bootstrap_directory"
        self.observe_file("bootstrap_directory", private)
        directory(private, 0, 0, 0o700)
        self.gate = "helper_file"
        self.observe_file("helper_file", Path(__file__).absolute())
        read_file(Path(__file__).absolute(), 0, 0, 0o600, 128 * 1024)
        self.gate = "template_file"
        self.observe_file("template_file", private / "representative_e2e.disabled.json")
        raw = read_file(private / "representative_e2e.disabled.json", 0, 0, 0o600, 32768)
        self.gate = "template_hash"
        if digest(raw) != TEMPLATE_SHA:
            raise GateError
        self.template = strict_json(raw)
        self.gate = "release_selector"
        if (
            os.readlink("/opt/api-quota-broker/current")
            != "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
        ):
            raise GateError
        self.gate = "installed_unit_file"
        unit = Path("/etc/systemd/system/api-quota-broker.service")
        self.observe_file("installed_unit_file", unit)
        raw = read_file(unit, 0, 0, 0o644, 32768)
        self.gate = "installed_unit_hash"
        if digest(raw) != "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060":
            raise GateError
        self.gate = "config_directory"
        self.observe_file("config_directory", CONFIG.parent)
        directory(CONFIG.parent, 0, 982, 0o750)
        self.gate = "backups_directory"
        self.observe_file("backups_directory", BACKUPS)
        directory(BACKUPS, 0, 0, 0o700)
        self.gate = "once_marker_absent"
        if MARKER.exists() or MARKER.is_symlink():
            raise GateError
        self.gate = "empty_config_file"
        self.observe_file("empty_config_file", CONFIG)
        self.original = read_file(CONFIG, 0, 982, 0o640, 8192)
        self.gate = "empty_config_hash"
        if digest(self.original) != EMPTY_SHA or strict_json(self.original) != {"targets": []}:
            raise GateError
        self.gate = "doppler_metadata_file"
        path = CONFIG.parent / "credentials/doppler-metadata.json"
        self.observe_file("doppler_metadata_file", path)
        metadata = strict_json(read_file(path, 0, 0, 0o600, 8192))
        self.gate = "doppler_metadata_scope"
        if any(
            metadata.get(k) != v
            for k, v in {
                "project": "api-quota-broker",
                "config": "dev",
                "access": "read",
                "credential_policy": "host",
                "human_dashboard_attested": True,
            }.items()
        ):
            raise GateError
        self.gate = "doppler_metadata_expiry"
        expiry = datetime.fromisoformat(metadata["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC) + timedelta(minutes=5):
            raise GateError
        self.gate = "broker_service"
        service = self.service(SERVICE)
        if any(
            service.get(k) != v
            for k, v in {
                "ActiveState": "active",
                "SubState": "running",
                "UnitFileState": "enabled",
                "User": "api-quota-broker",
                "Group": "api-quota-broker",
                "NRestarts": "0",
                "MemorySwapMax": "0",
                "LimitCORE": "0",
            }.items()
        ):
            raise GateError
        self.gate = "credential_directory_environment"
        if (
            os.environ.get("CREDENTIALS_DIRECTORY")
            != "/run/credentials/api-quota-broker-e2e.service"
        ):
            raise GateError
        credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token"
        self.gate = "client_credential_metadata"
        real_path(credential)
        self.observe_file("client_credential_metadata", credential)
        info = credential.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink)
            != (0, 0, 0o400, 1)
            or not 0 < info.st_size <= 256
        ):
            raise GateError
        if diagnosis:
            self.gate = "empty_ledger"
            ledger = self.database()
            if any(ledger["rows"].values()):
                raise GateError
            self.gate = "orderflow"
            self.before_orderflow = self.orderflow()
            self.gate = "metadata_diagnosis_complete"
            return {
                "empty_config_hash_verified": True,
                "once_marker_absent": True,
                "empty_ledger": True,
                "orderflow_unchanged": True,
                "actual_credential_reads": 0,
                "actual_provider_calls": 0,
                "actual_service_changes": 0,
                "observations": self.observations,
            }
        self.gate = "client_credential_read"
        raw = read_file(credential, 0, 0, 0o400, 256)
        self.client = raw.decode("ascii").strip()
        self.gate = "client_credential_format"
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.client):
            raise GateError
        self.gate = "empty_authenticated_targets"
        if self.targets():
            raise GateError
        self.gate = "empty_ledger"
        if any(self.database()["rows"].values()):
            raise GateError
        self.gate = "orderflow"
        self.before_orderflow = self.orderflow()
        return {"empty_authenticated_targets": True, "empty_ledger": True}

    def attest(self):
        if (
            input(
                "Confirm current Groq Free plan, billing disabled, one POST and temporary restart/restore (type ATTEST_FREE): "
            )
            != "ATTEST_FREE"
        ):
            raise GateError

    def write(self, path, raw, mode, gid):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def save(self, record):
        temporary = BACKUPS / (".e2e-receipt-" + uuid.uuid4().hex)
        self.write(
            temporary,
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
            0o600,
            0,
        )
        os.replace(temporary, MARKER)
        fd = os.open(BACKUPS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def claim(self, record):
        self.backup = BACKUPS / ("e2e-groq-" + uuid.uuid4().hex)
        self.backup.mkdir(mode=0o700)
        self.write(self.backup / "gateway.original.json", self.original, 0o600, 0)
        self.write(MARKER, json.dumps(record, sort_keys=True).encode(), 0o600, 0)
        self.claimed = True
        record["original_config_backup"] = str(self.backup / "gateway.original.json")
        self.save(record)

    def replace_config(self, raw):
        temporary = CONFIG.parent / (".e2e-config-" + uuid.uuid4().hex)
        self.write(temporary, raw, 0o640, 982)
        os.replace(temporary, CONFIG)
        fd = os.open(CONFIG.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def ready(self, expected):
        deadline = time.monotonic() + 20
        while True:
            try:
                targets = self.targets()
                if (
                    expected == 0
                    and not targets
                    or expected == 1
                    and len(targets) == 1
                    and targets[0].get("target_id") == "asus-groq-e2e-once"
                    and targets[0].get("provider") == "groq"
                    and targets[0].get("model") == MODEL
                    and targets[0].get("state") in {"ready", "unknown"}
                ):
                    return
            except (OSError, GateError, http.client.HTTPException):
                pass
            if time.monotonic() >= deadline:
                raise GateError
            time.sleep(0.25)  # Bounded local readiness only, never provider retry.

    def enable(self):
        if digest(read_file(CONFIG, 0, 982, 0o640, 8192)) != EMPTY_SHA:
            raise GateError
        target = self.template["targets"][0]
        now = datetime.now(UTC)
        target.update(
            enabled=True,
            free_eligible=True,
            billing_enabled=False,
            verified_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=5)).isoformat(),
            source="human current Groq Free/billing attestation for bounded ASUS probe",
        )
        raw = json.dumps(self.template, sort_keys=True, separators=(",", ":")).encode()
        self.temporary_sha = digest(raw)
        self.changed = True  # Before replace; failure still attempts safe restoration.
        self.replace_config(raw)
        self.command(["/usr/bin/systemctl", "restart", SERVICE])
        self.ready(1)

    def post(self):
        status, result = self.http("POST", "/v1/tasks", TASK)
        if status != 200:
            raise GateError
        return result

    def status(self):
        status, result = self.http("GET", "/v1/tasks/" + KEY)
        if status != 200:
            raise GateError
        return projected(result)

    def restore(self):
        current = digest(read_file(CONFIG, 0, 982, 0o640, 32768))
        if current not in {EMPTY_SHA, self.temporary_sha}:
            raise GateError  # Do not overwrite another administrator's config.
        original = read_file(self.backup / "gateway.original.json", 0, 0, 0o600, 8192)
        if digest(original) != EMPTY_SHA:
            raise GateError
        self.replace_config(original)
        self.command(["/usr/bin/systemctl", "restart", SERVICE])
        self.ready(0)
        service = self.service(SERVICE)
        if (
            service.get("ActiveState") != "active"
            or service.get("UnitFileState") != "enabled"
            or self.orderflow() != self.before_orderflow
        ):
            raise GateError
        return {
            "empty_targets": True,
            "active_enabled": True,
            "orderflow_unchanged": True,
            "ledger": self.database(),
        }

    def stop(self):
        self.command(["/usr/bin/systemctl", "stop", SERVICE])


def run(ops):
    record = {
        "mode": "asus_representative_groq_once",
        "status": "failed",
        "phase": "preflight",
        "post_attempted": False,
        "provider_posts_max": 1,
        "automatic_retry": False,
        "started_at": stamp(),
    }
    claimed, completed = False, False
    try:
        record["preflight"] = ops.preflight()
        record["phase"] = "free_attestation"
        ops.attest()
        record["phase"] = "claim"
        ops.claim(record)
        claimed = True
        record["phase"] = "temporary_target"
        ops.save(record)
        ops.enable()
        record["phase"] = "post"
        record["post_attempted"] = True
        ops.save(record)
        safe = projected(ops.post())
        record["result"] = safe
        record["phase"] = "ledger_verify"
        ops.save(record)
        metadata = ops.status()
        ledger = ops.database()
        record["ledger_before_restore"] = ledger
        completed = (
            safe["full_answer_verified"]
            and metadata["state"] == "completed"
            and metadata["ledger_state"] == "completed"
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
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed metadata only at secret boundary
        record["failure_phase"] = record["phase"]
        if record["phase"] == "preflight":
            gate = getattr(ops, "gate", None)
            record["failure_gate"] = gate if gate in PREFLIGHT_GATES else "unclassified"
        record["reason"] = "bounded_e2e_gate_failed"
    finally:
        if ops.changed:
            try:
                record["restoration"] = ops.restore()
                if record.get("ledger_before_restore") != record["restoration"]["ledger"]:
                    completed = False
                record["restored_original_config"] = True
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - restoration must not leak errors
                completed = False
                record["restored_original_config"] = False
                record["reason"] = "restoration_failed_operator_review_required"
                try:
                    ops.stop()
                    record["failure_stop_completed"] = True
                except Exception:  # noqa: BLE001 - fixed stop outcome only
                    record["failure_stop_completed"] = False
        record.update(
            status="passed" if completed else "failed",
            phase="complete" if completed else "failed",
            completed_at=stamp(),
        )
        if claimed or ops.claimed:
            try:
                ops.save(record)
                record["journal"] = str(MARKER)
            except Exception:  # noqa: BLE001 - fixed receipt failure only
                record.update(
                    status="failed", reason="receipt_save_failed_operator_review_required"
                )
    return record


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GateError


def main():
    try:
        parser = Parser(description=__doc__)
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--apply", action="store_true")
        modes.add_argument("--diagnose", action="store_true")
        args = parser.parse_args()
        if args.diagnose:
            os.umask(0o077)
            ops = RootOps()
            try:
                result = {
                    "mode": "asus_e2e_metadata_diagnosis",
                    "status": "passed",
                    "metadata": ops.preflight(diagnosis=True),
                }
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed code and metadata only
                result = {
                    "mode": "asus_e2e_metadata_diagnosis",
                    "status": "failed",
                    "failure_gate": ops.gate if ops.gate in PREFLIGHT_GATES else "unclassified",
                    "observations": ops.observations,
                    "actual_credential_reads": 0,
                    "actual_provider_calls": 0,
                    "actual_service_changes": 0,
                }
            result["state_snapshot"] = ops.diagnostic_snapshot()
        elif not args.apply:
            result = {
                "mode": "asus_e2e_plan",
                "provider": "groq",
                "model": MODEL,
                "new_provider_posts_max": 1,
                "doppler_secret_gets_max": 1,
                "provider_models_gets": 0,
                "retries": 0,
                "max_output_tokens": 512,
                "seconds": 30,
                "temporary_config_then_restore": True,
                "free_attestation_required": True,
                "actual_credential_reads": 0,
                "actual_provider_calls": 0,
                "actual_service_changes": 0,
            }
        else:
            os.umask(0o077)
            result = run(RootOps())
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fixed metadata only at secret boundary
        result = {"status": "failed", "reason": "invalid_options_or_gate"}
    print(json.dumps(result, sort_keys=True), flush=True)
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
