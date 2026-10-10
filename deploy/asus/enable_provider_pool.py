"""Reviewed ASUS pool operator. Default is a plan; root apply is single use.

No new token or provider metadata GET. Six fixed loopback Gateway tasks, one per
new provider, no retry. Existing Groq evidence and all historical rows/claims stay.
Only the broker current selector/config may change. Failure restores originals;
failure to restore stops the broker and preserves every artifact for diagnosis.
"""

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import re
import resource
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/opt/api-quota-broker")
CURRENT = BASE / "current"
CONFIG = Path("/etc/api-quota-broker/gateway.json")
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
BACKUPS = Path("/var/backups/api-quota-broker")
CLAIM = BACKUPS / "provider-pool-2026-10-04-r1.claim.json"
JOURNAL = BACKUPS / "provider-pool-2026-10-04-r1.json"
UNIT = "api-quota-broker-pool.service"
SERVICE = "api-quota-broker.service"
OLD_RELEASE = "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770"
UNIT_SHA = "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
EMPTY_SHA = "a758f0791313b284d4e230a4d5f162fb002bbb9c0e814277ddc24b07ace36cb2"
OLD_MARKER = BACKUPS / "representative-groq-once.json"
OLD_MARKER_SHA = "0e7bd18cb142a9a793d0fbca9fb54c129b1e17ad76242a6850769138ceee409a"
GROQ_JOURNAL = BACKUPS / "representative-groq-after-recovery-once.json"
HISTORY = (
    OLD_MARKER,
    GROQ_JOURNAL,
    BACKUPS / "representative-groq-after-recovery-once.claim.json",
    BACKUPS / "e2e-groq-4319ad90692d4221b910b043bf5da0ce/gateway.original.json",
    BACKUPS / "service-recovery-e072ded861ea4717898b07d7ed9c12e4/recovery.json",
    BACKUPS / "service-recovery-e072ded861ea4717898b07d7ed9c12e4/gateway.failed.json",
)
TABLES = (
    "gateway_tasks",
    "gateway_attempts",
    "reservations",
    "charges",
    "queue_jobs",
    "queue_attempts",
)
SAFE_STATES = {
    "completed",
    "completed_usage_unknown",
    "unknown",
    "rejected",
    "pre_send_failed",
    "quota_rejected",
    "quota_exhausted",
}


class GateError(ValueError):
    pass


def require(condition, code="operator_gate"):
    if not condition:
        raise GateError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(GateError())
    )


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path, uid=0, gid=0, mode=0o600, bound=131072):
    for parent in (*reversed(path.parents), path.parent):
        require(not parent.is_symlink())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == uid
            and info.st_gid == gid
            and stat.S_IMODE(info.st_mode) == mode
            and info.st_nlink == 1
            and info.st_size <= bound
        )
        raw = stream.read(bound + 1)
        require(len(raw) == info.st_size)
        return raw


def write_exclusive(path, raw, *, uid=0, gid=0, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchown(stream.fileno(), uid, gid)
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def service_can_read(path, expected):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(982)
            os.setuid(995)
            raw = read_regular(path, 0, 982, 0o640)
            os._exit(0 if digest(raw) == expected else 1)
        except BaseException:  # noqa: BLE001 - child must fail closed without exception output.
            os._exit(1)
    _, status = os.waitpid(child, 0)
    require(os.waitstatus_to_exitcode(status) == 0)


def atomic_file(path, raw, *, service_read=False):
    temporary = path.parent / (".pool-" + uuid.uuid4().hex)
    write_exclusive(
        temporary, raw, gid=982 if service_read else 0, mode=0o640 if service_read else 0o600
    )
    if service_read:
        service_can_read(temporary, digest(raw))
    os.replace(temporary, path)
    sync_directory(path.parent)


def task_key(provider):
    return "asus-pool-2026-10-04-r1-" + provider + "-a1"


def project_result(provider, model, result):
    require(
        isinstance(result, dict)
        and result.get("provider") in (provider, None)
        and result.get("model") in (model, None)
        and result.get("request_key") == task_key(provider)
    )
    diagnostics = result.get("diagnostics")
    # Gateway diagnostics live on the attempt, not the top-level response.
    attempts = result.get("attempts")
    if isinstance(attempts, list) and len(attempts) == 1:
        diagnostics = attempts[0].get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    reported = diagnostics.get("provider_reported_model")
    metadata_state = diagnostics.get("provider_model_status")
    recognized = metadata_state == "reported" and isinstance(reported, str) and len(reported) <= 256
    # Revalidate against the fixed requested identity / numeric alias versions.
    compatible = recognized and (
        reported == model
        or provider == "openrouter"
        and reported == model.removesuffix(":free")
        or provider in {"google", "mistral"}
        and bool(
            re.fullmatch(re.escape(model.removesuffix("-latest")) + r"(?:-\d{1,8}){1,4}", reported)
        )
        or provider == "mistral"
        and model == "ministral-3b-latest"
        and bool(re.fullmatch(r"ministral-3-3b-\d{4}(?:-\d{2})?", reported))
    )
    state = result.get("state")
    answer = result.get("answer")
    complete = (
        state in {"completed", "completed_usage_unknown"}
        and result.get("http_status") == 200
        and isinstance(answer, str)
        and bool(answer.strip())
    )
    if provider in {"nvidia", "mistral", "openrouter"}:
        complete &= (
            result.get("finish_reason") == "stop" and result.get("response_truncated") is False
        )
    if provider == "google":
        complete &= diagnostics.get("provider_finish_reason") == "STOP"
    if provider == "ocrspace":
        complete &= isinstance(answer, str) and answer.strip() == "OK"
    complete &= compatible or metadata_state in {"missing", "not_reported_by_contract"}
    safe = {
        "provider": provider,
        "requested_model": model,
        "provider_reported_model": reported if compatible else None,
        "provider_model_basis": "provider_response" if compatible else "unknown",
        "model_compatible": bool(compatible) if recognized else None,
        "state": state if isinstance(state, str) and state in SAFE_STATES else "unclassified",
        "full_answer_verified": bool(complete),
        "provider_posts_max": 1,
        "automatic_retry": False,
        "request_key": task_key(provider),
        "provider_model_status": metadata_state
        if isinstance(metadata_state, str)
        and metadata_state
        in {
            "reported",
            "missing",
            "not_reported_by_contract",
            "invalid",
            "redacted",
            "unrecognized",
            "ambiguous_json",
            "not_success",
            "unknown",
        }
        else "unknown",
    }
    for field, choices in (
        (
            "error_code",
            {
                "provider_http_error",
                "provider_authentication_error",
                "provider_quota_rejected",
                "provider_response_invalid",
                "provider_timeout",
                "provider_transport_error",
                "ledger_report_failed",
                "unavailable",
                "deadline_expired",
                "credential_missing",
                "credential_read_failed",
                "internal_error",
                "mistral_non_json_error",
                "mistral_non_object_error",
                "mistral_rate_limit_error",
                "mistral_error_type_unclassified",
                "mistral_nested_error_unclassified",
                "mistral_error_envelope_unclassified",
                "google_not_found",
                "google_model_not_found",
                "groq_model_blocked_org",
                "groq_model_blocked_project",
                "groq_edge_browser_signature_blocked",
            },
        ),
        ("finish_reason", {"stop", "length", "content_filter", "tool_calls"}),
    ):
        value = result.get(field)
        safe[field] = value if isinstance(value, str) and value in choices else None
    safe["response_truncated"] = (
        result.get("response_truncated") if type(result.get("response_truncated")) is bool else None
    )
    safe["provider_finish_reason"] = (
        diagnostics.get("provider_finish_reason")
        if diagnostics.get("provider_finish_reason")
        in ("STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER")
        else None
    )
    safe["ledger_state"] = (
        result.get("ledger_state")
        if result.get("ledger_state")
        in (
            "completed",
            "failed",
            "unknown",
            "dispatched",
            "reserved",
            "quota_rejected",
            "cancelled",
            "expired",
        )
        else None
    )
    for field, maximum in (
        ("http_status", 599),
        ("reported_input_tokens", 131072),
        ("reported_output_tokens", 512),
        ("latency_ms", 150000),
    ):
        value = result.get(field)
        safe[field] = value if type(value) is int and 0 <= value <= maximum else None
    safe["ledger_basis"] = (
        result.get("ledger_basis")
        if isinstance(result.get("ledger_basis"), str)
        and result.get("ledger_basis")
        in {"settled_provider_usage", "held_estimate", "rejected_zero_usage", "unsent"}
        else "unknown"
    )
    return safe


class Ops:
    def __init__(self, private, policy, planner, verifier):
        self.private, self.policy, self.planner, self.verifier = private, policy, planner, verifier
        self.changed = False
        self.client = None
        self.phase = "preflight"
        self.claimed = False
        self.journal_sha = None
        self.expected_current = OLD_RELEASE
        self.expected_config_sha = EMPTY_SHA

    def command(self, argv):
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=150,
            check=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout

    def service(self, name):
        raw = self.command(
            [
                "/usr/bin/systemctl",
                "show",
                name,
                "--property=ActiveState,SubState,UnitFileState,MainPID,ExecMainStartTimestampMonotonic,NRestarts,MemoryHigh,MemoryMax,MemorySwapMax,LimitCORE,User,Group,CPUQuotaPerSecUSec",
            ]
        )
        return dict(line.split("=", 1) for line in raw.decode().splitlines())

    def orderflow(self):
        service = self.service("orderflow.service")
        require(service["ActiveState"] == "active")
        connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=5)
        try:
            connection.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            require(connection.getresponse().status == 200)
        finally:
            connection.close()
        return {
            key: service[key] for key in ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts")
        }

    def connect(self):
        for parent in DB.parents:
            require(not parent.is_symlink(), "ledger_path")
        info = DB.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 995
            and info.st_gid == 982
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "ledger_metadata",
        )
        return sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=5)

    def history_rows(self, *, initial=False):
        with self.connect() as con:
            require(con.execute("PRAGMA quick_check").fetchone() == ("ok",))
            if initial:
                self.highwater = {
                    table: con.execute("SELECT coalesce(max(rowid),0) FROM " + table).fetchone()[0]
                    for table in TABLES
                }
            result = {}
            for table in TABLES:
                rows = con.execute(
                    "SELECT rowid,* FROM " + table + " WHERE rowid<=? ORDER BY rowid",
                    (self.highwater[table],),
                ).fetchall()
                encoded = [
                    [value.hex() if isinstance(value, bytes) else value for value in row]
                    for row in rows
                ]
                result[table] = {"rows": len(rows), "sha256": digest(canonical(encoded))}
            return result

    def files(self):
        return {str(path): digest(read_regular(path)) for path in HISTORY}

    def runtime_snapshot(self, runtime, *, dependencies_only=False):
        result = {}
        for path in sorted(runtime.rglob("*")):
            name = str(path.relative_to(runtime))
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if dependencies_only and name.startswith("lib/python3.14/site-packages/quota_broker/"):
                continue
            info = path.lstat()
            require(
                info.st_uid == info.st_gid == 0
                and (path.is_symlink() or not stat.S_IMODE(info.st_mode) & 0o022),
                "runtime_ownership",
            )
            if path.is_symlink():
                result[name] = {"link": str(path.readlink())}
            elif path.is_file():
                require(
                    info.st_nlink == 1 and not info.st_mode & (stat.S_ISUID | stat.S_ISGID),
                    "runtime_file",
                )
                result[name] = {
                    "sha256": digest(path.read_bytes()),
                    "mode": stat.S_IMODE(info.st_mode),
                }
            else:
                require(path.is_dir(), "runtime_entry")
        return result

    def preflight(self):
        require(
            os.geteuid() == 0
            and socket.gethostname() == "asus-ubuntu2604-server"
            and sys.stdin.isatty()
        )
        require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))
        require(
            Path("/proc/self/cgroup").read_bytes().strip()
            == b"0::/system.slice/api-quota-broker-pool.service"
        )
        group = Path("/sys/fs/cgroup/system.slice") / UNIT
        require((group / "memory.max").read_bytes().strip() == b"402653184")
        require((group / "memory.swap.max").read_bytes().strip() == b"0")
        cpu = (group / "cpu.max").read_bytes().split()
        require(len(cpu) == 2 and int(cpu[0]) * 2 == int(cpu[1]))
        require(
            not CLAIM.exists()
            and not CLAIM.is_symlink()
            and not JOURNAL.exists()
            and not JOURNAL.is_symlink()
        )
        require(str(CURRENT.readlink()) == OLD_RELEASE)
        require(digest(read_regular(Path("/etc/systemd/system") / SERVICE, mode=0o644)) == UNIT_SHA)
        self.original = read_regular(CONFIG, gid=982, mode=0o640)
        require(digest(self.original) == EMPTY_SHA)
        self.history_files = self.files()
        require(self.history_files[str(OLD_MARKER)] == OLD_MARKER_SHA)
        groq = strict_json(read_regular(GROQ_JOURNAL))
        require(
            groq.get("status") == "passed"
            and groq.get("restored_original_config") is True
            and groq.get("result", {}).get("state") == "completed"
        )
        self.history = self.history_rows(initial=True)
        require(
            self.history["gateway_tasks"]["rows"] == self.history["gateway_attempts"]["rows"] == 1
        )
        require(self.history["reservations"]["rows"] == 1 and self.history["charges"]["rows"] == 3)
        require(self.history["queue_jobs"]["rows"] == self.history["queue_attempts"]["rows"] == 0)
        meta = strict_json(read_regular(CONFIG.parent / "credentials/doppler-metadata.json"))
        require(
            all(
                meta.get(key) == value
                for key, value in {
                    "project": "api-quota-broker",
                    "config": "dev",
                    "access": "read",
                    "credential_policy": "host",
                    "human_dashboard_attested": True,
                }.items()
            )
        )
        expiry = datetime.fromisoformat(meta["expires_at"])
        require(expiry.tzinfo is not None and expiry > datetime.now(UTC) + timedelta(minutes=15))
        self.expiry = expiry.isoformat()
        self.prior_orderflow = self.orderflow()
        self.old_runtime_snapshot = self.runtime_snapshot(BASE / OLD_RELEASE / "runtime")
        self.check_service()
        require(os.environ.get("CREDENTIALS_DIRECTORY") == "/run/credentials/" + UNIT)
        raw = read_regular(
            Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token", mode=0o400, bound=257
        )
        self.client = raw.decode().strip()
        require(bool(re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", self.client)))
        require(self.http("GET", "/v1/diagnostics")[1]["targets"] == [])

    def check_service(self):
        service = self.service(SERVICE)
        require(
            all(
                service.get(key) == value
                for key, value in {
                    "ActiveState": "active",
                    "SubState": "running",
                    "UnitFileState": "enabled",
                    "MemoryHigh": "268435456",
                    "MemoryMax": "402653184",
                    "MemorySwapMax": "0",
                    "LimitCORE": "0",
                    "User": "api-quota-broker",
                    "Group": "api-quota-broker",
                    "CPUQuotaPerSecUSec": "500ms",
                }.items()
            )
        )
        listeners = (
            self.command(["/usr/bin/ss", "-H", "-ltn", "sport = :18084"]).decode().splitlines()
        )
        require(
            len(listeners) == 1 and listeners[0].split()[3] == "127.0.0.1:18084", "broker_listener"
        )
        connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=5)
        try:
            connection.request("GET", "/v1/diagnostics")
            require(connection.getresponse().status == 401, "anonymous_admission")
        finally:
            connection.close()

    def http(self, method, path, body=None):
        require(
            method == "GET"
            and body is None
            and path in {"/v1/diagnostics", "/v1/usage"}
            or method == "GET"
            and body is None
            and path
            in {
                "/v1/tasks/" + task_key(provider)
                for provider in self.planner.PROVIDERS
                if provider != "groq"
            }
            or method == "POST"
            and path == "/v1/tasks"
            and body in self.tasks().values()
        )
        limit = 150 if method == "POST" else 10
        deadline = time.monotonic() + limit
        connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=limit)
        try:
            connection.request(
                method,
                path,
                body=canonical(body) if body else None,
                headers={
                    "Authorization": "Bearer " + self.client,
                    "Content-Type": "application/json",
                },
            )
            wire = connection.sock
            response = connection.getresponse()
            raw = bytearray()
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                if wire is not None:
                    wire.settimeout(remaining)
                chunk = response.read1(min(16384, 262145 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                require(len(raw) <= 262144)
            require(response.length in (None, 0))
            return response.status, strict_json(raw)
        finally:
            connection.close()

    def tasks(self):
        result = {}
        for provider, (model, _, output) in self.planner.PROVIDERS.items():
            if provider == "groq":
                continue
            content = "Reply with exactly READY."
            if provider == "ocrspace":
                content = self.policy["synthetic_ocr_png"]
                require(len(base64.b64decode(content, validate=True)) == 129)
            result[provider] = {
                "request_key": task_key(provider),
                "provider": provider,
                "model": model,
                "capability": "ocr" if provider == "ocrspace" else "text_generation",
                "input": content,
                "max_output_tokens": output,
                "max_attempts": 1,
            }
        return result

    def claim(self, record):
        self.backup = BACKUPS / ("provider-pool-" + uuid.uuid4().hex)
        self.backup.mkdir(mode=0o700)
        sync_directory(BACKUPS)
        record["backup"] = str(self.backup)
        write_exclusive(CLAIM, canonical(record))
        self.claimed = True
        self.save(record)
        write_exclusive(self.backup / "gateway.original.json", self.original)
        destination = self.backup / "ledger.original.sqlite3"
        write_exclusive(destination, b"")
        with self.connect() as source, sqlite3.connect(destination) as copied:
            source.backup(copied)
            require(copied.execute("PRAGMA quick_check").fetchone() == ("ok",))
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        sync_directory(self.backup)
        require(self.history_rows() == self.history)

    def save(self, record):
        raw = canonical(record)
        if self.journal_sha is None:
            write_exclusive(JOURNAL, raw)
        else:
            require(digest(read_regular(JOURNAL)) == self.journal_sha, "journal_changed")
            atomic_file(JOURNAL, raw)
        self.journal_sha = digest(raw)

    def stage(self):
        archive = read_regular(self.private / "source.tar", bound=134217728)
        require(digest(archive) == self.policy["source_archive_sha256"])
        verified = self.verifier.verify_archive(archive, self.policy["payload_manifest_sha256"])
        destination = BASE / "releases" / ("release-" + self.policy["payload_manifest_sha256"])
        require(not destination.exists() and not destination.is_symlink())
        self.verifier.extract_archive(archive, self.policy["payload_manifest_sha256"], destination)
        self.new_release = destination
        old_runtime = BASE / OLD_RELEASE / "runtime"
        links = {
            str(path.relative_to(old_runtime)): str(path.readlink())
            for path in old_runtime.rglob("*")
            if path.is_symlink()
        }
        require(
            links
            == {
                "lib64": "lib",
                "bin/python3": "python",
                "bin/python": "/usr/bin/python3.14",
                "bin/python3.14": "python",
            }
        )
        shutil.copytree(
            old_runtime,
            destination / "runtime",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        require(
            self.runtime_snapshot(old_runtime, dependencies_only=True)
            == self.runtime_snapshot(destination / "runtime", dependencies_only=True),
            "dependency_copy_changed",
        )
        package = destination / "runtime/lib/python3.14/site-packages/quota_broker"
        require(package.is_dir() and not package.is_symlink())
        for row in verified["release"]["files"]:
            name = row["path"]
            if name.startswith("src/quota_broker/"):
                path = destination / name
                require(digest(path.read_bytes()) == row["sha256"])
                shutil.copyfile(path, package / path.name)
                os.chmod(package / path.name, 0o644)
        # Keep old distribution/wheel receipts as historical bytes; declare the
        # source overlay separately rather than pretending it is an intact wheel.
        write_exclusive(
            destination / "pool-runtime-overlay.json",
            canonical(
                {
                    "basis": "existing_native_dependencies_source_overlay",
                    "original_release": OLD_RELEASE,
                    "payload_manifest_sha256": self.policy["payload_manifest_sha256"],
                }
            ),
            mode=0o644,
        )
        for directory in [
            destination,
            *(p for p in destination.rglob("*") if p.is_dir() and not p.is_symlink()),
        ]:
            os.chmod(directory, 0o755)
        script = "import json,sys; import quota_broker.gateway_providers as p; import quota_broker.config as c; assert sys.version_info[:2]==(3,14); r=p.safe_response_diagnostics('google',200,{},json.dumps({'modelVersion':'gemini-3.5-flash-lite'}).encode(),requested_model='gemini-3.5-flash-lite'); assert r['provider_reported_model']=='gemini-3.5-flash-lite'; assert c.cloudflare_neuron_upper_bound(4096,256)==15; print('offline_runtime_passed')"
        require(
            self.command(
                [str(destination / "runtime/bin/python"), "-I", "-B", "-c", script]
            ).strip()
            == b"offline_runtime_passed"
        )
        # The root operator runs isolated system Python; imports in the pinned
        # planner must resolve to the verified candidate source after staging.
        sys.path.insert(0, str(destination / "src"))

    def switch(self, config, *, original=False):
        require(
            str(CURRENT.readlink()) == self.expected_current
            and digest(read_regular(CONFIG, gid=982, mode=0o640)) == self.expected_config_sha,
            "selector_or_config_changed",
        )
        self.changed = True
        raw = self.original if original else canonical(config)
        atomic_file(CONFIG, raw, service_read=True)
        self.expected_config_sha = digest(raw)
        target = OLD_RELEASE if original else str(self.new_release.relative_to(BASE))
        temporary = BASE / (".pool-current-" + uuid.uuid4().hex)
        os.symlink(target, temporary)
        os.replace(temporary, CURRENT)
        self.expected_current = target
        sync_directory(BASE)
        self.command(["/usr/bin/systemctl", "restart", SERVICE])
        deadline = time.monotonic() + 20
        while True:
            try:
                self.check_service()
                status, diagnostics = self.http("GET", "/v1/diagnostics")
                require(status == 200 and isinstance(diagnostics.get("targets"), list))
                require(
                    {t["target_id"] for t in diagnostics["targets"]}
                    == {t["id"] for t in config["targets"]}
                )
                break
            except (OSError, GateError, http.client.HTTPException):
                require(time.monotonic() < deadline, "broker_readiness")
                time.sleep(0.25)  # Local readiness only; never repeats a provider request.

    def preservation(self):
        require(self.files() == self.history_files and self.history_rows() == self.history)
        require(
            self.runtime_snapshot(BASE / OLD_RELEASE / "runtime") == self.old_runtime_snapshot,
            "old_runtime_changed",
        )
        require(self.orderflow() == self.prior_orderflow)
        return {
            "history_files_preserved": True,
            "historical_rows_preserved": True,
            "orderflow_unchanged": True,
        }

    def ledger_probe(self, provider, result):
        key = task_key(provider)
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            task = con.execute("SELECT * FROM gateway_tasks WHERE request_key=?", (key,)).fetchone()
            require(task is not None, "task_missing")
            # A pre-send refusal may have no selected provider or reservation.
            require(
                task["provider"] in (provider, None)
                and task["model"] in (self.planner.PROVIDERS[provider][0], None),
                "ledger_identity",
            )
            rows = con.execute(
                "SELECT * FROM gateway_attempts WHERE request_key=? ORDER BY attempt_no", (key,)
            ).fetchall()
            require(len(rows) <= 1, "attempt_budget")
            reservations, charges = [], []
            for row in rows:
                require(
                    row["provider"] == provider
                    and row["model"] == self.planner.PROVIDERS[provider][0],
                    "ledger_identity",
                )
                reservation = con.execute(
                    "SELECT * FROM reservations WHERE id=?", (row["reservation_id"],)
                ).fetchone()
                require(reservation is not None, "reservation_missing")
                reservations.append(dict(reservation))
                values = con.execute(
                    "SELECT * FROM charges WHERE reservation_id=? ORDER BY bucket",
                    (row["reservation_id"],),
                ).fetchall()
                require(len(values) == (4 if provider == "cloudflare" else 3), "charge_dimensions")
                for charge in values:
                    require(
                        charge["bucket"].startswith(provider + ":asus-dev-key:"), "charge_scope"
                    )
                    if reservation["state"] == "completed":
                        require(
                            charge["amount"]
                            == (
                                1
                                if charge["metric"] == "requests"
                                else row["reported_input_tokens"]
                                if charge["metric"] == "input_tokens"
                                else row["reported_neurons"]
                            ),
                            "charge_settlement",
                        )
                    if reservation["state"] == "quota_rejected":
                        require(charge["amount"] == 0, "quota_rejection_charges")
                charges.extend(dict(value) for value in values)
                require(
                    row["http_status"] == result["http_status"]
                    and row["reported_input_tokens"] == result["reported_input_tokens"]
                    and row["reported_output_tokens"] == result["reported_output_tokens"],
                    "reported_usage_mismatch",
                )
                if result["full_answer_verified"]:
                    require(
                        len(rows) == 1 and row["state"] in ("completed", "completed_usage_unknown"),
                        "completion_ledger",
                    )
                    require(
                        reservation["state"]
                        == ("completed" if row["state"] == "completed" else "unknown"),
                        "completion_hold",
                    )
                    require(
                        con.execute(
                            "SELECT count(*) FROM execution_completion WHERE reservation_id=?",
                            (row["reservation_id"],),
                        ).fetchone()[0]
                        == 1,
                        "completion_mark",
                    )
            if result["full_answer_verified"]:
                require(bool(rows), "completion_attempt_missing")
            projection = {
                "task": dict(task),
                "attempts": [dict(row) for row in rows],
                "reservations": reservations,
                "charges": charges,
            }
            return {
                "sha256": digest(canonical(projection)),
                "attempts": len(rows),
                "reservations": len(reservations),
                "charges": len(charges),
            }

    def observe(self, provider, response=None):
        status, saved = self.http("GET", "/v1/tasks/" + task_key(provider))
        if status == 404 and response is None:
            # Caller cannot prove non-dispatch. Durable intent remains consumed;
            # this provider cannot be enabled or replayed, even without a row.
            return {
                "provider": provider,
                "requested_model": self.planner.PROVIDERS[provider][0],
                "request_key": task_key(provider),
                "state": "unknown",
                "full_answer_verified": False,
                "blocker": "loopback_delivery_unknown",
                "provider_posts_max": 1,
                "automatic_retry": False,
                "ledger_projection": None,
            }
        require(status == 200, "status_unavailable")
        projected = project_result(provider, self.planner.PROVIDERS[provider][0], saved)
        if response is not None:
            immediate = project_result(provider, self.planner.PROVIDERS[provider][0], response)
            require(
                {k: v for k, v in immediate.items() if k != "full_answer_verified"}
                == {k: v for k, v in projected.items() if k != "full_answer_verified"},
                "status_response_mismatch",
            )
            projected["full_answer_verified"] = immediate["full_answer_verified"]
        projected["ledger_projection"] = self.ledger_probe(provider, projected)
        status = projected["http_status"]
        projected["blocker"] = (
            None
            if projected["full_answer_verified"]
            else "rate_limit"
            if status == 429
            else "authentication"
            if status == 401
            else "access_denied"
            if status == 403
            else "payment_required"
            if status == 402
            else "model_or_route_missing"
            if status == 404
            else "provider_or_transport_unknown"
            if projected["state"] == "unknown"
            else "response_not_accepted"
        )
        return projected

    def verify_restart(self, results):
        for result in results:
            if result.get("ledger_projection") is None:
                continue
            require(
                self.ledger_probe(result["provider"], result) == result["ledger_projection"],
                "new_ledger_changed",
            )
            status, saved = self.http("GET", "/v1/tasks/" + result["request_key"])
            require(status == 200, "restart_status")
            projected = project_result(result["provider"], result["requested_model"], saved)
            require(
                all(
                    projected[key] == value
                    for key, value in result.items()
                    if key in projected and key != "full_answer_verified"
                ),
                "restart_projection",
            )
        with self.connect() as con:
            allowed = {result["request_key"] for result in results}
            for table in ("gateway_tasks", "gateway_attempts"):
                keys = {
                    row[0]
                    for row in con.execute(
                        "SELECT request_key FROM " + table + " WHERE rowid>?",
                        (self.highwater[table],),
                    )
                }
                require(keys <= allowed, "foreign_execution")
            require(
                con.execute("SELECT count(*) FROM queue_jobs").fetchone()[0] == 0
                and con.execute("SELECT count(*) FROM queue_attempts").fetchone()[0] == 0,
                "queue_changed",
            )


def run(ops):
    record = {
        "mode": "asus_provider_pool_r1",
        "status": "running",
        "phase": "preflight",
        "started_at": datetime.now(UTC).isoformat(),
        "provider_posts_max": 6,
        "new_token_creation": 0,
        "authenticated_metadata_gets": 0,
        "automatic_retry": False,
        "posts_intended": [],
        "results": [],
        "claim": str(CLAIM),
        "journal": str(JOURNAL),
    }
    try:
        ops.preflight()
        record["phase"] = "backup"
        ops.claim(record)
        record["phase"] = "stage_runtime"
        ops.save(record)
        ops.stage()
        candidates = set(ops.planner.PROVIDERS) - {"groq"}
        record["phase"] = "probe_readiness"
        ops.save(record)
        ops.switch(
            ops.planner.qualified_config(candidates, ops.expiry, ops.policy["free_attested_at"])
        )
        accepted = {"groq"}
        for provider, task in ops.tasks().items():
            record["phase"] = "probe_" + provider
            ops.preservation()
            record["posts_intended"].append(provider)
            ops.save(record)  # durable file+directory before the only loopback POST
            try:
                status, result = ops.http("POST", "/v1/tasks", task)
            except (OSError, http.client.HTTPException):
                status, result = None, None
            # A non-200 loopback response or transport uncertainty is never a
            # retry trigger. Query only the original key and preserve its ledger.
            projected = ops.observe(provider, result if status == 200 else None)
            if projected["full_answer_verified"]:
                accepted.add(provider)
            record["results"].append(projected)
            ops.save(record)
        record["phase"] = "activate_pool"
        ops.save(record)
        normal = ops.planner.qualified_config(
            accepted, ops.expiry, ops.policy["free_attested_at"], normal=True
        )
        ops.switch(normal)
        status, usage = ops.http("GET", "/v1/usage")
        require(status == 200 and isinstance(usage, list))
        ops.verify_restart(record["results"])
        record.update(ops.preservation())
        record.update(
            status="passed",
            phase="complete",
            enabled_providers=sorted(accepted),
            completed_at=datetime.now(UTC).isoformat(),
            restart_verified=True,
            purpose="personal_development_prototyping",
        )
        ops.save(record)
    except Exception:  # noqa: BLE001 - fixed receipt; any fault requires restoration.
        record["status"] = "failed"
        record["failure_phase"] = record["phase"]
        if ops.changed:
            try:
                ops.switch({"targets": []}, original=True)
                record["original_restored"] = True
                record.update(ops.preservation())
            except Exception:  # noqa: BLE001 - restoration must handle any failure.
                record["original_restored"] = False
                try:
                    ops.command(["/usr/bin/systemctl", "stop", SERVICE])
                    record["broker_stopped"] = True
                except Exception:  # noqa: BLE001 - restoration must handle any failure.
                    record["broker_stop_failed"] = True
        if ops.claimed:
            try:
                ops.save(record)
            except Exception:  # noqa: BLE001 - restoration must handle any failure.
                record["receipt_save_failed"] = True
    return record


def load_pinned(private, name, expected):
    require(digest(read_regular(private / name, bound=1048576)) == expected)
    spec = importlib.util.spec_from_file_location("pool_" + name.replace(".", "_"), private / name)
    require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "provider_calls": 0,
                    "new_token_creation": 0,
                    "service_changes": 0,
                    "initial_posts_max": 6,
                    "groq_once_replay": False,
                    "purpose": "personal_development_prototyping",
                },
                sort_keys=True,
            )
        )
        return
    try:
        private = Path(__file__).absolute().parent
        info = private.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and info.st_gid == 0
            and stat.S_IMODE(info.st_mode) == 0o700
        )
        policy = strict_json(read_regular(private / "policy.json"))
        planner = load_pinned(private, "provider_pool_plan.py", policy["planner_sha256"])
        verifier = load_pinned(private, "build_asus_release.py", policy["verifier_sha256"])
        result = run(Ops(private, policy, planner, verifier))
    except Exception:  # noqa: BLE001 - bootstrap outputs only a fixed safe code.
        result = {
            "mode": "asus_provider_pool_r1",
            "status": "failed",
            "phase": "bootstrap",
            "provider_calls": 0,
        }
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result.get("status") == "passed" else 1)


if __name__ == "__main__":
    main()
