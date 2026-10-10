"""Bounded local deployment acceptance; never dispatches provider requests.

The default CLI is a plan. Observer execution requires an ASUS root transient
service with swap and core dumps disabled. Initial acceptance additionally makes
local maintenance probes only after proving the execution ledger is empty.
"""

import argparse
import grp
import http.client
import json
import os
import platform
import pwd
import re
import resource
import selectors
import sqlite3
import stat
import subprocess
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICE = "api-quota-broker.service"
CGROUP = "/system.slice/api-quota-broker.service"
SELF_GROUPS = {
    "/system.slice/api-quota-broker-install.service",
    "/system.slice/api-quota-broker-acceptance.service",
}
CREDENTIALS = ("digest_key", "client_token", "admin_token", "queue_key", "doppler_service_token")
CONFIG = Path("/etc/api-quota-broker/gateway.json")
CREDS = CONFIG.parent / "credentials"
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
QUEUE_KEY = Path("/run/api-quota-broker/queue.key")
BACKUPS = Path("/var/backups/api-quota-broker")
BODY_BOUND = 262144
HTTP_SECONDS = 5
TABLES = (
    "reservations",
    "charges",
    "gateway_tasks",
    "gateway_attempts",
    "queue_jobs",
    "queue_attempts",
)
STATES = {
    "reserved",
    "dispatched",
    "unknown",
    "completed",
    "completed_usage_unknown",
    "expired",
    "cancelled",
    "rejected",
    "failed",
    "pre_send_failed",
    "quota_rejected",
    "quota_exhausted",
    "queued",
    "waiting",
    "running",
    "pending",
    "released",
    "preparing",
}
PROPERTIES = (
    "User",
    "Group",
    "MainPID",
    "ActiveState",
    "SubState",
    "NRestarts",
    "ExecMainStartTimestampMonotonic",
    "MemoryHigh",
    "MemoryMax",
    "MemorySwapMax",
    "CPUQuotaPerSecUSec",
    "TasksMax",
    "LimitNOFILE",
    "Nice",
    "NoNewPrivileges",
    "LimitCORE",
)
RESOURCES = {
    "MemoryHigh": "268435456",
    "MemoryMax": "402653184",
    "MemorySwapMax": "0",
    "CPUQuotaPerSecUSec": "500ms",
    "TasksMax": "32",
    "LimitNOFILE": "128",
    "Nice": "10",
    "NoNewPrivileges": "yes",
    "LimitCORE": "0",
}
EXPLAIN = {
    "request_key": "asus-acceptance-explain",
    "capability": "text_generation",
    "input": "deployment acceptance",
    "max_output_tokens": 1,
}
ADMIN_PATH = "/v1/admin/targets/not-present/health/reset"
GET_PATHS = {"/v1/diagnostics", "/v1/catalog", "/v1/usage", "/v1/tasks", "/v1/queue"}
POST_PATHS = {ADMIN_PATH, "/v1/routes/explain"}
CATALOG_FIELDS = {
    "target_id",
    "provider",
    "model",
    "author",
    "host",
    "endpoint",
    "origin",
    "capabilities",
    "context_tokens",
    "max_output_tokens",
    "free_kind",
    "use_restrictions",
    "source",
    "source_verified_at",
    "account_source",
    "account_verified_at",
    "account_expires_at",
    "free_eligible",
    "billing_enabled",
    "available",
    "quota_basis",
    "quotas",
    "local_safety_caps",
    "provider_quota_facts",
    "capacity",
    "concurrency_limit",
    "shared_concurrency_scope",
    "shared_concurrency_limit",
    "configured_available",
    "admission_state",
    "health",
    "quota_observations",
    "features",
}
DIAGNOSIS_FIELDS = {
    "target_id",
    "provider",
    "model",
    "capability",
    "state",
    "reasons",
    "credentials",
    "cooldown_until",
    "health",
    "quota_observations",
    "ranking_factors",
    "temporary",
    "next_retry_at",
    "capacity",
    "effective_capacity_kind",
}


class AcceptanceError(ValueError):
    def __init__(self, phase: str, reason: str):
        super().__init__("deployment acceptance failed")
        self.phase, self.reason = phase, reason


def require(condition: bool, phase: str, reason: str) -> None:
    if not condition:
        raise AcceptanceError(phase, reason)


def _directory(path: Path) -> int:
    parts = path.absolute().parts
    require(".." not in parts, "filesystem", "unsafe_path")
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open(path: Path) -> int:
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise AcceptanceError("filesystem", "file_type")
    return fd


def _read(path: Path, bound: int) -> bytes:
    fd = _open(path)
    try:
        raw = bytearray()
        while True:
            chunk = os.read(fd, min(65536, bound + 1 - len(raw)))
            if not chunk:
                return bytes(raw)
            raw.extend(chunk)
            require(len(raw) <= bound, "filesystem", "metadata_bound")
    finally:
        os.close(fd)


def _metadata(
    path: Path, uid: int, gid: int, mode: int, *, directory: bool = False, size: int | None = None
) -> None:
    fd = _directory(path) if directory else _open(path)
    try:
        info = os.fstat(fd)
        require(
            info.st_uid == uid and info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode,
            "filesystem",
            "ownership_or_mode",
        )
        require(directory or info.st_nlink == 1, "filesystem", "file_links")
        require(size is None or info.st_size == size, "filesystem", "file_size")
    finally:
        os.close(fd)


def _command(command: list[str], *, bound: int = 16384) -> bytes:
    deadline = time.monotonic() + 10
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "SYSTEMD_COLORS": "0"},
    ) as child:
        assert child.stdout is not None
        data = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, "command", "deadline")
                    for key, _ in selector.select(min(0.25, remaining)):
                        chunk = os.read(key.fd, 4096)
                        if chunk:
                            data.extend(chunk)
                            require(len(data) <= bound, "command", "output_bound")
                        else:
                            selector.unregister(key.fileobj)
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
            require(child.returncode == 0, "command", "failed")
            return bytes(data)
        except subprocess.TimeoutExpired:
            raise AcceptanceError("command", "deadline") from None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        require(key not in result, "http", "duplicate_json_field")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(AcceptanceError("http", "invalid_json")),
    )


def _systemd(service: str, properties: tuple[str, ...]) -> dict[str, str]:
    require(service in {SERVICE, "orderflow.service"}, "systemd", "unit_not_allowed")
    raw = _command(["/usr/bin/systemctl", "show", service, "--property=" + ",".join(properties)])
    fields: dict[str, str] = {}
    for line in raw.decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        require(
            bool(separator) and key in properties and key not in fields,
            "systemd",
            "invalid_metadata",
        )
        fields[key] = value
    require(set(fields) == set(properties), "systemd", "missing_metadata")
    return fields


def _cgroup(pid: int) -> str:
    raw = _read(Path(f"/proc/{pid}/cgroup"), 4096)
    require(raw.count(b"\n") <= 1, "cgroup", "membership")
    value = raw.decode("ascii").rstrip("\n")
    require(
        value.startswith("0::/system.slice/")
        and re.fullmatch(r"0::/system\.slice/[A-Za-z0-9_.@-]+\.service", value) is not None,
        "cgroup",
        "membership",
    )
    return value[3:]


def _swap(group: str) -> None:
    for name in ("memory.swap.max", "memory.swap.current"):
        require(
            _read(Path("/sys/fs/cgroup" + group) / name, 32).strip() == b"0",
            "cgroup",
            "swap_not_zero",
        )


def _execution_gate() -> None:
    require(
        os.geteuid() == 0 and platform.node() == "asus-ubuntu2604-server",
        "precondition",
        "root_asus_required",
    )
    require(
        resource.getrlimit(resource.RLIMIT_CORE) == (0, 0), "precondition", "core_dumps_enabled"
    )
    group = _cgroup(os.getpid())
    require(group in SELF_GROUPS, "precondition", "observer_cgroup")
    _swap(group)


def _service() -> tuple[dict[str, Any], dict[str, str], int, int]:
    owner = pwd.getpwnam("api-quota-broker")
    gid = grp.getgrnam("api-quota-broker").gr_gid
    require(owner.pw_uid > 0 and owner.pw_gid == gid and gid > 0, "service", "service_identity")
    data = _systemd(SERVICE, PROPERTIES)
    require(data["User"] == data["Group"] == "api-quota-broker", "service", "service_identity")
    require(
        data["ActiveState"] == "active" and data["SubState"] == "running", "service", "not_running"
    )
    require(
        all(data[key] == value for key, value in RESOURCES.items()), "service", "resource_policy"
    )
    require(
        data["MainPID"].isdigit() and 0 < int(data["MainPID"]) < 2**31, "service", "invalid_pid"
    )
    require(
        data["NRestarts"].isdigit() and data["ExecMainStartTimestampMonotonic"].isdigit(),
        "service",
        "invalid_metadata",
    )
    pid = int(data["MainPID"])
    require(_cgroup(pid) == CGROUP, "service", "process_cgroup")
    status = _read(Path(f"/proc/{pid}/status"), 16384).decode("ascii")
    for key, expected in (("Uid", owner.pw_uid), ("Gid", gid)):
        values = re.findall(
            r"^" + key + r":\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status, re.MULTILINE
        )
        require(
            len(values) == 1 and all(int(v) == expected for v in values[0]),
            "service",
            "process_identity",
        )
    limits = _read(Path(f"/proc/{pid}/limits"), 8192).decode("ascii")
    require(
        re.search(r"^Max core file size\s+0\s+0\s+bytes\s*$", limits, re.MULTILINE) is not None,
        "service",
        "process_core_limit",
    )
    _swap(CGROUP)
    require(
        _command(["/usr/bin/stat", "-f", "-c", "%T", "/run"]).strip() == b"tmpfs",
        "filesystem",
        "run_not_tmpfs",
    )
    _metadata(QUEUE_KEY.parent, owner.pw_uid, gid, 0o700, directory=True)
    _metadata(QUEUE_KEY, owner.pw_uid, gid, 0o600, size=32)
    _metadata(CREDS, 0, 0, 0o700, directory=True)
    for name in CREDENTIALS:
        _metadata(CREDS / f"{name}.cred", 0, 0, 0o600)
    summary = {
        "active": True,
        "main_pid": pid,
        "n_restarts": int(data["NRestarts"]),
        "start_monotonic_usec": int(data["ExecMainStartTimestampMonotonic"]),
        "uid": owner.pw_uid,
        "gid": gid,
        "memory_high_bytes": 268435456,
        "memory_max_bytes": 402653184,
        "swap_max": 0,
        "swap_current": 0,
        "tasks_max": 32,
        "queue_key_metadata": "verified",
        "encrypted_credential_metadata_count": 5,
        "run_tmpfs": True,
        "core_limit": 0,
    }
    return summary, data, owner.pw_uid, gid


def _database(uid: int, gid: int) -> dict[str, Any]:
    _metadata(DB.parent, uid, gid, 0o700, directory=True)
    _metadata(DB, uid, gid, 0o600)
    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        deadline = time.monotonic() + 5
        require(
            connection.execute("PRAGMA quick_check").fetchall() == [("ok",)],
            "sqlite",
            "quick_check",
        )
        counts = {
            name: connection.execute("SELECT count(*) FROM " + name).fetchone()[0]
            for name in TABLES
        }
        states = {}
        for table in ("reservations", "gateway_tasks", "queue_jobs"):
            rows = connection.execute(
                "SELECT state,count(*) FROM " + table + " GROUP BY state"
            ).fetchall()
            require(
                all(state in STATES and type(count) is int and count >= 0 for state, count in rows),
                "sqlite",
                "unknown_state",
            )
            states[table] = dict(rows)
    return {"quick_check": "ok", "rows": counts, "states": states}


def _decrypt(name: str) -> str:
    require(name in {"client_token", "admin_token"}, "credentials", "credential_not_allowed")
    raw = _command(
        ["/usr/bin/systemd-creds", "decrypt", "--name=" + name, str(CREDS / f"{name}.cred"), "-"],
        bound=4096,
    )
    token = raw.decode("ascii")
    require(
        32 <= len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token),
        "credentials",
        "invalid_token",
    )
    return token


def _token_metadata() -> dict[str, Any]:
    path = CREDS / "doppler-metadata.json"
    _metadata(path, 0, 0, 0o600)
    record = _json(_read(path, 8192))
    require(isinstance(record, dict), "credential_metadata", "invalid_schema")
    times = []
    for name in ("created_at", "expires_at"):
        value = record.get(name)
        require(
            isinstance(value, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is not None,
            "credential_metadata",
            "invalid_timestamp",
        )
        try:
            times.append(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC))
        except ValueError:
            raise AcceptanceError("credential_metadata", "invalid_timestamp") from None
    created, expires = times
    expected = {
        "schema_version": 1,
        "credential_policy": "host",
        "approval_user_message_id": "01a0ffaa-84ae-7030-b193-a004ca233d8e",
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "token_name": "asus-api-quota-broker",
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "created_at_source": "local_import_clock",
        "human_dashboard_attested": True,
        "remote_scope_verified": False,
        "evidence": "human_dashboard_attestation_only",
        "local_keys_expire": False,
    }
    require(
        record == expected
        and type(record["schema_version"]) is int
        and record["human_dashboard_attested"] is True
        and record["remote_scope_verified"] is False
        and record["local_keys_expire"] is False,
        "credential_metadata",
        "invalid_scope_or_evidence",
    )
    require(
        created <= datetime.now(UTC) < expires
        and timedelta(0) < expires - created <= timedelta(days=30),
        "credential_metadata",
        "expired_or_invalid_lifetime",
    )
    return {
        "credential_policy": "host",
        "project": "api-quota-broker",
        "config": "dev",
        "access": "read",
        "expires_at": record["expires_at"],
        "unexpired": True,
        "human_dashboard_attested": True,
        "remote_scope_verified": False,
        "evidence": "human_dashboard_attestation_only",
    }


def _http(
    method: str,
    path: str,
    token: str | None,
    body: dict[str, Any] | None,
    sensitive: tuple[str, ...],
) -> tuple[int, Any]:
    require(
        (method == "GET" and path in GET_PATHS and body is None)
        or (method == "POST" and path == ADMIN_PATH and body == {})
        or (method == "POST" and path == "/v1/routes/explain" and body == EXPLAIN),
        "http",
        "request_not_allowed",
    )
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=HTTP_SECONDS)
    deadline = time.monotonic() + HTTP_SECONDS
    try:
        connection.connect()
        socket = connection.sock
        require(socket is not None, "http", "connection_unavailable")
        assert socket is not None
        socket.settimeout(max(0.001, deadline - time.monotonic()))
        connection.request(method, path, body=payload, headers=headers)
        socket.settimeout(max(0.001, deadline - time.monotonic()))
        response = connection.getresponse()
        require(not 300 <= response.status < 400, "http", "redirect_rejected")
        require(
            response.getheader("Content-Type", "").split(";", 1)[0] == "application/json",
            "http",
            "content_type",
        )
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "http", "deadline")
            socket.settimeout(remaining)
            chunk = response.read1(min(8192, BODY_BOUND + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            require(len(data) <= BODY_BOUND, "http", "body_bound")
            if response.isclosed():
                break
        require(
            not any(secret.encode() in data for secret in sensitive), "http", "secret_reflection"
        )
        return response.status, _json(bytes(data))
    finally:
        connection.close()


def _error(status: int, data: Any, expected_status: int, error: str) -> None:
    require(
        status == expected_status
        and isinstance(data, dict)
        and set(data) <= {"error", "message", "wait_until"}
        and data.get("error") == error,
        "http",
        "authorization_or_error",
    )


def _rows(data: Any, fields: set[str]) -> None:
    require(isinstance(data, list), "http", "metadata_rows_schema")
    for row in data:
        require(
            isinstance(row, dict)
            and set(row) <= fields
            and all(
                isinstance(row.get(name), str) and row[name]
                for name in ("target_id", "provider", "model")
            ),
            "http",
            "metadata_rows_schema",
        )


def _api(initial: bool, empty: bool) -> dict[str, Any]:
    client, admin = _decrypt("client_token"), _decrypt("admin_token")
    require(client != admin, "credentials", "roles_not_separate")
    sensitive = (client, admin)
    status, data = _http("GET", "/v1/diagnostics", None, None, sensitive)
    _error(status, data, 401, "unauthorized")
    status, diagnosis = _http("GET", "/v1/diagnostics", client, None, sensitive)
    require(
        status == 200
        and isinstance(diagnosis, dict)
        and set(diagnosis) == {"as_of", "ready_targets", "targets", "basis", "queue_worker"},
        "http",
        "diagnostics_schema",
    )
    _rows(diagnosis["targets"], DIAGNOSIS_FIELDS)
    require(
        isinstance(diagnosis["targets"], list)
        and type(diagnosis["ready_targets"]) is int
        and 0 <= diagnosis["ready_targets"] <= len(diagnosis["targets"]),
        "http",
        "diagnostics_schema",
    )
    worker = diagnosis["queue_worker"]
    require(
        isinstance(worker, dict)
        and set(worker) == {"enabled", "running", "stopped", "error_code", "last_tick_at"}
        and worker.get("enabled") is False
        and worker.get("running") is False
        and worker.get("error_code") is None,
        "http",
        "queue_worker_enabled",
    )
    status, catalog = _http("GET", "/v1/catalog", client, None, sensitive)
    require(status == 200, "http", "catalog_status")
    _rows(catalog, CATALOG_FIELDS)
    require(not initial or catalog == diagnosis["targets"] == [], "http", "targets_not_empty")
    result = {
        "unauthenticated_status": 401,
        "client_diagnostics_status": 200,
        "catalog_rows": len(catalog),
        "diagnostic_target_rows": len(diagnosis["targets"]),
        "ready_targets": diagnosis["ready_targets"],
        "queue_worker_enabled": False,
        "credential_decryptions": 2,
    }
    if initial:
        status, data = _http("POST", ADMIN_PATH, client, {}, sensitive)
        _error(status, data, 401, "unauthorized")
        status, data = _http("POST", ADMIN_PATH, admin, {}, sensitive)
        _error(status, data, 404, "not_found")
        result["client_admin_status"], result["admin_missing_target_status"] = 401, 404
    else:
        result["admin_post"] = "skipped_observer"
    if initial and empty:
        status, plan = _http("POST", "/v1/routes/explain", client, EXPLAIN, sensitive)
        require(
            status == 200
            and isinstance(plan, dict)
            and set(plan)
            == {
                "selected_target_id",
                "estimated_input_tokens",
                "candidates",
                "temporary",
                "permanent_rejection",
                "next_retry_at",
            }
            and plan["selected_target_id"] is None
            and plan["candidates"] == []
            and plan["permanent_rejection"] is True,
            "http",
            "route_not_fail_closed",
        )
        result["explain"] = "no_candidates"
        for path in ("/v1/usage", "/v1/tasks", "/v1/queue"):
            status, data = _http("GET", path, client, None, sensitive)
            if path != "/v1/usage":
                require(
                    isinstance(data, dict)
                    and set(data)
                    == ({"tasks", "next_before"} if path == "/v1/tasks" else {"tasks"}),
                    "http",
                    "task_list_schema",
                )
            rows = (
                data
                if path == "/v1/usage"
                else data.get("tasks")
                if isinstance(data, dict)
                else None
            )
            require(status == 200 and rows == [], "http", "unexpected_task_rows")
        result["usage_rows"] = result["task_rows"] = result["queue_rows"] = 0
    else:
        result["maintenance_probes"] = "skipped_observer" if empty else "skipped_ledger_not_empty"
    return result


def _orderflow() -> dict[str, Any]:
    metadata = _systemd("orderflow.service", ("ActiveState", "NRestarts"))
    require(metadata == {"ActiveState": "active", "NRestarts": "0"}, "orderflow", "service_changed")
    connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=HTTP_SECONDS)
    try:
        connection.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
        require(connection.getresponse().status == 200, "orderflow", "http_status")
    finally:
        connection.close()
    return {"active": True, "n_restarts": 0, "http_status": 200}


def run_checks(*, initial: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "initial" if initial else "observer",
        "checked_at": datetime.now(UTC).isoformat(),
        "provider_calls": 0,
        "doppler_calls": 0,
        "service_changes": 0,
        "checks": {},
    }
    try:
        _execution_gate()
        summary, systemd, uid, gid = _service()
        report["checks"]["service"] = summary
        report["credential_metadata"] = _token_metadata()
        config = _json(_read(CONFIG, BODY_BOUND))
        require(
            isinstance(config, dict) and isinstance(config.get("targets"), list),
            "config",
            "invalid_schema",
        )
        require(not initial or config == {"targets": []}, "config", "targets_not_empty")
        before = _database(uid, gid)
        empty = not any(before["rows"].values())
        require(not initial or empty, "sqlite", "ledger_not_empty")
        report["checks"]["sqlite"] = before
        report["database"] = before
        report["checks"]["api"] = _api(initial, empty)
        require(_database(uid, gid) == before, "sqlite", "ledger_changed")
        after = _systemd(SERVICE, PROPERTIES)
        require(after == systemd, "service", "service_changed")
        report["checks"]["orderflow"] = _orderflow()
        report["status"] = "passed"
    except AcceptanceError as exc:
        report.update(status="failed", phase=exc.phase, reason=exc.reason)
    except (OSError, ValueError, KeyError, sqlite3.Error, http.client.HTTPException):
        report.update(status="failed", phase="observer", reason="unavailable_or_invalid_metadata")
    return report


def _receipt(path: Path, report: dict[str, Any]) -> None:
    require(
        path.parent == BACKUPS
        and re.fullmatch(r"acceptance[-A-Za-z0-9_]{1,80}\.json", path.name) is not None,
        "receipt",
        "path_not_allowed",
    )
    _metadata(BACKUPS, 0, 0, 0o700, directory=True)
    parent = _directory(BACKUPS)
    try:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(report, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(parent)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise AcceptanceError("options", "invalid_options")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--initial", action="store_true")
    parser.add_argument("--receipt", type=Path)
    try:
        args = parser.parse_args(argv)
    except AcceptanceError:
        print('{"schema_version":1,"status":"failed","phase":"options","reason":"invalid_options"}')
        return 2
    if not args.execute:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "dry_plan",
                    "execution_authorized": False,
                    "provider_calls": 0,
                    "observer_root_service_required": True,
                    "core_limit": 0,
                    "swap_limit": 0,
                    "http_seconds": HTTP_SECONDS,
                    "http_body_bound": BODY_BOUND,
                    "credential_decryptions": ["client_token", "admin_token"],
                    "initial_local_maintenance": True,
                },
                sort_keys=True,
            )
        )
        return 0
    report = run_checks(initial=args.initial)
    try:
        if args.receipt is not None:
            _receipt(args.receipt, report)
    except (OSError, AcceptanceError):
        report = {
            "schema_version": 1,
            "status": "failed",
            "phase": "receipt",
            "reason": "write_failed",
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
