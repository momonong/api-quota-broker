"""Explicitly enabled, encrypted SQLite queue; uncertain execution is never replayed.

The HTTP layer must authenticate all callers, especially ``result``. This module
never creates a key: the operator supplies a persistent key independent of the
Gateway HMAC key. Metadata remains after ciphertext expiry to prevent replay.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .core import canonical, stamp
from .gateway import GatewayError, validate_task

PENDING = {"queued", "waiting"}
TERMINAL = {"completed", "failed", "expired", "cancelled", "unknown"}
SAFE_RESULT_FIELDS = {
    "provider",
    "model",
    "target_id",
    "reservation_id",
    "ledger_state",
    "ledger_basis",
    "reported_input_tokens",
    "reported_output_tokens",
    "reported_neurons",
    "usage_source",
    "finish_reason",
    "response_truncated",
    "http_status",
    "latency_ms",
}


def _private(path: Path, mode: int, *, directory: bool = False) -> None:
    """Reject symlinks, wrong owners and permissive files instead of fixing them."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise GatewayError("queue_security", "queue private file is unavailable") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
        raise GatewayError("queue_security", "queue requires private directory/file permissions")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise GatewayError("queue_security", "queue private file has a different owner")


def prepare_queue_storage(db: str | Path, key: str | Path) -> None:
    """Check private queue storage before constructing a Gateway or altering its DB.

    This checks key-file metadata only. DurableQueue authenticates the key later.
    Existing files are never repaired, opened for writing, or changed here.
    """
    key_path = Path(key)
    db_path = Path(db)
    if str(db_path) == ":memory:":
        raise GatewayError("queue_config", "queue requires a persistent private database")
    _private(key_path.parent, 0o700, directory=True)
    _private(key_path, 0o600)
    _private(db_path.parent, 0o700, directory=True)
    existing = db_path.exists() or db_path.is_symlink()
    if existing:
        _private(db_path, 0o600)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _private(sidecar, 0o600)
    if not existing:
        try:
            fd = os.open(
                db_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise GatewayError(
                "queue_security", "private queue database cannot be created"
            ) from exc
        os.close(fd)
        _private(db_path, 0o600)


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("offset required")
        return parsed.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise GatewayError("invalid_request", "queue deadline requires an aware timestamp") from exc


class DurableQueue:
    def __init__(
        self,
        gateway: Any,
        key: bytes | str | Path,
        *,
        ttl_seconds: int = 86400,
        lease_seconds: int = 180,
        max_waiting: int = 1000,
        retry_seconds: int = 5,
        execution_lease_seconds: int = 180,
    ) -> None:
        for value in (
            ttl_seconds,
            lease_seconds,
            max_waiting,
            retry_seconds,
            execution_lease_seconds,
        ):
            if type(value) is not int or value < 1:
                raise GatewayError("queue_config", "queue limits must be positive integers")
        self.gateway = gateway
        self.db = str(gateway.db)
        self.clock = gateway.clock
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds
        self.max_waiting = max_waiting
        self.retry_seconds = retry_seconds
        self.execution_lease_seconds = execution_lease_seconds
        if isinstance(key, (str, Path)):
            key_path = Path(key)
            _private(key_path.parent, 0o700, directory=True)
            _private(key_path, 0o600)
            # O_NOFOLLOW closes the usual check/read symlink race on POSIX.
            fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 256:
                    raise GatewayError("queue_security", "invalid queue key file")
                key_bytes = os.read(fd, 257)
                if len(key_bytes) != 32:
                    key_bytes = key_bytes.strip()
            finally:
                os.close(fd)
        elif isinstance(key, bytes):
            key_bytes = key
        else:
            raise GatewayError("queue_key_missing", "an explicit persistent queue key is required")
        if key_bytes == getattr(gateway, "digest_key", None):
            raise GatewayError("queue_config", "queue key must be independent of Gateway HMAC key")
        if len(key_bytes) == 32:
            key_bytes = base64.urlsafe_b64encode(key_bytes)
        try:
            self.cipher = Fernet(key_bytes)
            self._fingerprint_key = base64.urlsafe_b64decode(key_bytes)
        except (ValueError, TypeError) as exc:
            raise GatewayError(
                "queue_key_invalid", "queue key must be a Fernet or 32-byte key"
            ) from exc
        if self._fingerprint_key == getattr(gateway, "digest_key", None):
            raise GatewayError("queue_config", "queue key must be independent of Gateway HMAC key")
        path = Path(self.db)
        if self.db == ":memory:":
            raise GatewayError("queue_config", "queue requires a persistent private database")
        _private(path.parent, 0o700, directory=True)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        _private(path, 0o600)
        self._check_files()
        with self._tx() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS queue_settings (id INTEGER PRIMARY KEY, verifier BLOB NOT NULL)"
            )
            verifier = con.execute("SELECT verifier FROM queue_settings WHERE id=1").fetchone()
            if verifier is None:
                con.execute(
                    "INSERT INTO queue_settings VALUES (1,?)",
                    (self.cipher.encrypt(b"quota-broker-queue-v1"),),
                )
            else:
                try:
                    marker = self.cipher.decrypt(verifier[0])
                except InvalidToken as exc:
                    raise GatewayError(
                        "queue_key_mismatch", "persistent queue key does not match"
                    ) from exc
                if marker != b"quota-broker-queue-v1":
                    raise GatewayError("queue_key_mismatch", "persistent queue key does not match")
            con.execute("""
                CREATE TABLE IF NOT EXISTS queue_jobs (
                    request_key TEXT PRIMARY KEY, payload_hmac TEXT NOT NULL,
                    payload BLOB, result BLOB, state TEXT NOT NULL,
                    priority INTEGER NOT NULL, deadline TEXT, wait_policy TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, completed_at TEXT,
                    next_retry_at TEXT, lease_until TEXT, lease_owner TEXT, lease_token TEXT, execution_until TEXT,
                    execution_key TEXT, run_started INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS queue_attempts (
                    request_key TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                    execution_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    PRIMARY KEY (request_key, attempt_no)
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS queue_pending ON queue_jobs(state,next_retry_at,priority,deadline)"
            )
            columns = {r[1] for r in con.execute("PRAGMA table_info(queue_jobs)")}
            if "execution_until" not in columns:
                con.execute("ALTER TABLE queue_jobs ADD COLUMN execution_until TEXT")

    def _check_files(self) -> None:
        path = Path(self.db)
        _private(path.parent, 0o700, directory=True)
        _private(path, 0o600)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(self.db + suffix)
            if sidecar.exists():
                _private(sidecar, 0o600)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._check_files()
        with sqlite3.connect(self.db, timeout=15, isolation_level=None) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA secure_delete=ON")
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            else:
                con.execute("COMMIT")

    def _encrypt(self, value: dict[str, Any], key: str, purpose: str) -> bytes:
        envelope = {"key": key, "purpose": purpose, "data": value}
        return self.cipher.encrypt(canonical(envelope).encode())

    def _decrypt(self, value: bytes | None, key: str, purpose: str) -> dict[str, Any]:
        if value is None:
            raise GatewayError("queue_content_expired", "queue content is no longer retained")
        try:
            decoded = json.loads(self.cipher.decrypt(value))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise GatewayError(
                "queue_ciphertext_invalid", "queue content authentication failed"
            ) from exc
        if (
            not isinstance(decoded, dict)
            or decoded.get("key") != key
            or decoded.get("purpose") != purpose
            or not isinstance(decoded.get("data"), dict)
        ):
            raise GatewayError("queue_ciphertext_invalid", "queue content authentication failed")
        return decoded["data"]

    def _row(self, con: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM queue_jobs WHERE request_key=?", (key,)).fetchone()
        if row is None:
            raise GatewayError("not_found", "queue task not found")
        return row

    def _view(self, con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        fields = (
            "request_key",
            "state",
            "priority",
            "deadline",
            "wait_policy",
            "max_attempts",
            "attempt_count",
            "created_at",
            "expires_at",
            "completed_at",
            "next_retry_at",
            "error_code",
            "execution_key",
        )
        result = {field: row[field] for field in fields}
        result.update(json.loads(row["metadata_json"]))
        result["result_available"] = row["result"] is not None
        result["attempt_budget_used"] = self._budget_used(con, row["request_key"])
        result["attempts"] = [
            dict(item)
            for item in con.execute(
                "SELECT attempt_no,execution_key,created_at FROM queue_attempts WHERE request_key=? ORDER BY attempt_no",
                (row["request_key"],),
            )
        ]
        return result

    def _budget_used(self, con: sqlite3.Connection, key: str) -> int:
        """Each Gateway attempt costs one; a run without an attempt also costs one."""
        executions = con.execute(
            "SELECT execution_key FROM queue_attempts WHERE request_key=?", (key,)
        ).fetchall()
        has_attempts = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gateway_attempts'"
        ).fetchone()
        total = 0
        for execution in executions:
            count = (
                con.execute(
                    "SELECT count(*) FROM gateway_attempts WHERE request_key=?", (execution[0],)
                ).fetchone()[0]
                if has_attempts
                else 0
            )
            total += max(1, count)
        return total

    def _execution(self, con: sqlite3.Connection, row: sqlite3.Row) -> str:
        """Read dispatch evidence; only a settled explicit refusal proves no execution."""
        key = row["execution_key"]
        if key is None or not row["run_started"]:
            return "unsent"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gateway_tasks" not in tables:
            return "unsent"
        task = con.execute("SELECT * FROM gateway_tasks WHERE request_key=?", (key,)).fetchone()
        if task is None:
            return "unsent"
        attempts = (
            list(con.execute("SELECT * FROM gateway_attempts WHERE request_key=?", (key,)))
            if "gateway_attempts" in tables
            else []
        )
        # Gateway task is the last-attempt summary. Use individual attempts when
        # available so a final refusal cannot hide an earlier uncertain dispatch.
        items = attempts or [task]
        for item in items:
            reservation = item["reservation_id"]
            ledger = (
                con.execute(
                    "SELECT state,dispatched_at FROM reservations WHERE id=?", (reservation,)
                ).fetchone()
                if reservation and "reservations" in tables
                else None
            )
            if (
                ledger is not None
                and ledger["state"] == "quota_rejected"
                and item["state"] == "quota_rejected"
            ):
                continue
            if item["dispatched_at"] or item["state"] in {
                "dispatched",
                "unknown",
                "completed",
                "completed_usage_unknown",
                "quota_rejected",
            }:
                return "unknown"
            if reservation and (
                ledger is None
                or ledger["dispatched_at"]
                or ledger["state"]
                in {"dispatched", "unknown", "completed", "failed", "quota_rejected"}
            ):
                return "unknown"
        # A ledger dispatch can precede insertion/update of Gateway attempts.
        if task["reservation_id"] and not any(
            item["reservation_id"] == task["reservation_id"] for item in attempts
        ):
            ledger = (
                con.execute(
                    "SELECT state,dispatched_at FROM reservations WHERE id=?",
                    (task["reservation_id"],),
                ).fetchone()
                if "reservations" in tables
                else None
            )
            if (
                ledger is None
                or ledger["dispatched_at"]
                or ledger["state"] in {"dispatched", "unknown"}
            ):
                return "unknown"
        return "unsent"

    def _finish(
        self,
        con: sqlite3.Connection,
        row: sqlite3.Row,
        state: str,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        metadata = {key: result[key] for key in SAFE_RESULT_FIELDS if result and key in result}
        if result:
            metadata["gateway_state"] = result.get("state")
        con.execute(
            "UPDATE queue_jobs SET state=?,payload=NULL,result=?,completed_at=?,error_code=?,"
            "next_retry_at=NULL,lease_until=NULL,lease_owner=NULL,lease_token=NULL,metadata_json=? WHERE request_key=?",
            (
                state,
                self._encrypt(result, row["request_key"], "result")
                if result and row["expires_at"] > stamp(self.clock())
                else None,
                stamp(self.clock()),
                error,
                canonical(metadata),
                row["request_key"],
            ),
        )

    def _maintenance(self, con: sqlite3.Connection) -> None:
        now = stamp(self.clock())
        for row in con.execute(
            "SELECT * FROM queue_jobs WHERE state='running' AND (lease_until<=? OR expires_at<=? OR (deadline IS NOT NULL AND deadline<=?))",
            (now, now, now),
        ).fetchall():
            dispatched = self._execution(con, row) != "unsent"
            if dispatched:
                self._finish(con, row, "unknown", error="execution_uncertain")
            elif row["expires_at"] <= now or (row["deadline"] and row["deadline"] <= now):
                self._finish(con, row, "expired", error="deadline_expired")
            else:
                con.execute(
                    "UPDATE queue_jobs SET state='queued',lease_until=NULL,lease_owner=NULL,lease_token=NULL,next_retry_at=NULL,run_started=0 WHERE request_key=?",
                    (row["request_key"],),
                )
        for row in con.execute(
            "SELECT * FROM queue_jobs WHERE state IN ('queued','waiting') AND (expires_at<=? OR (deadline IS NOT NULL AND deadline<=?))",
            (now, now),
        ).fetchall():
            self._finish(con, row, "expired", error="deadline_expired")
        con.execute("UPDATE queue_jobs SET payload=NULL,result=NULL WHERE expires_at<=?", (now,))

    def submit(self, raw: dict[str, Any]) -> dict[str, Any]:
        validator = getattr(self.gateway, "validate_task", validate_task)
        supplied = {
            **raw,
            "wait_policy": raw.get("wait_policy", "wait"),
            "max_attempts": raw.get("max_attempts", 32),
        }
        data = validator(supplied)
        priority = data.get("priority", raw.get("priority", 0))
        deadline = data.get("deadline", raw.get("deadline"))
        wait_policy = data.get("wait_policy", raw.get("wait_policy", "wait"))
        max_attempts = data.get("max_attempts", raw.get("max_attempts", 32))
        if type(priority) is not int or not -1000 <= priority <= 1000:
            raise GatewayError("invalid_request", "queue priority must be -1000..1000")
        if wait_policy not in {"wait", "reject"}:
            raise GatewayError("invalid_request", "queue wait_policy must be wait or reject")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise GatewayError("invalid_request", "queue max_attempts must be 1..32")
        if deadline is not None:
            if not isinstance(deadline, str):
                raise GatewayError("invalid_request", "invalid queue deadline")
            deadline = stamp(_date(deadline))
        data.update(priority=priority, wait_policy=wait_policy, max_attempts=max_attempts)
        if deadline is None:
            data.pop("deadline", None)
        else:
            data["deadline"] = deadline
        fingerprint = hmac.new(
            self._fingerprint_key, canonical(data).encode(), hashlib.sha256
        ).hexdigest()
        key = data["request_key"]
        with self._tx() as con:
            self._maintenance(con)
            prior = con.execute("SELECT * FROM queue_jobs WHERE request_key=?", (key,)).fetchone()
            if prior:
                if not hmac.compare_digest(prior["payload_hmac"], fingerprint):
                    raise GatewayError("conflict", "queue key already used for different task")
                return self._view(con, prior)
            count = con.execute(
                "SELECT count(*) FROM queue_jobs WHERE state IN ('queued','waiting','running')"
            ).fetchone()[0]
            if count >= self.max_waiting:
                raise GatewayError("queue_full", "queue capacity reached")
            now = self.clock()
            expired = deadline is not None and _date(deadline) <= now
            con.execute(
                "INSERT INTO queue_jobs(request_key,payload_hmac,payload,state,priority,deadline,wait_policy,max_attempts,created_at,expires_at,completed_at,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    fingerprint,
                    None if expired else self._encrypt(data, key, "payload"),
                    "expired" if expired else "queued",
                    priority,
                    deadline,
                    wait_policy,
                    max_attempts,
                    stamp(now),
                    stamp(now + timedelta(seconds=self.ttl_seconds)),
                    stamp(now) if expired else None,
                    "deadline_expired" if expired else None,
                ),
            )
            return self._view(con, self._row(con, key))

    def status(self, key: str) -> dict[str, Any]:
        with self._tx() as con:
            self._maintenance(con)
            return self._view(con, self._row(con, key))

    def result(self, key: str) -> dict[str, Any]:
        with self._tx() as con:
            self._maintenance(con)
            row = self._row(con, key)
            if row["result"] is None and row["state"] not in TERMINAL:
                raise GatewayError("queue_result_pending", "queue result is not ready")
            return self._decrypt(row["result"], key, "result")

    def recent(self, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GatewayError("invalid_request", "queue limit must be 1..1000")
        with self._tx() as con:
            self._maintenance(con)
            rows = con.execute(
                "SELECT * FROM queue_jobs ORDER BY created_at DESC,request_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return {"tasks": [self._view(con, row) for row in rows]}

    def cancel(self, key: str) -> dict[str, Any]:
        with self._tx() as con:
            self._maintenance(con)
            row = self._row(con, key)
            if row["state"] in PENDING:
                self._finish(con, row, "cancelled")
            elif row["state"] == "running":
                # Once run starts, cancellation cannot establish provider termination.
                if row["run_started"]:
                    raise GatewayError(
                        "invalid_transition", "running execution cannot be cancelled"
                    )
                self._finish(con, row, "cancelled")
            return self._view(con, self._row(con, key))

    def _claim(self, worker_id: str) -> sqlite3.Row | None:
        with self._tx() as con:
            self._maintenance(con)
            now = self.clock()
            row = con.execute(
                "SELECT * FROM queue_jobs WHERE state IN ('queued','waiting') AND (next_retry_at IS NULL OR next_retry_at<=?) "
                "ORDER BY priority DESC,deadline IS NULL,deadline,created_at,request_key LIMIT 1",
                (stamp(now),),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            con.execute(
                "UPDATE queue_jobs SET state='running',lease_owner=?,lease_token=?,lease_until=?,run_started=0 WHERE request_key=?",
                (
                    worker_id,
                    token,
                    stamp(now + timedelta(seconds=self.lease_seconds)),
                    row["request_key"],
                ),
            )
            return self._row(con, row["request_key"])

    def _owned(self, con: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
        current = self._row(con, row["request_key"])
        if (
            current["state"] != "running"
            or current["lease_token"] != row["lease_token"]
            or current["lease_until"] <= stamp(self.clock())
        ):
            return None
        return current

    def _wait(self, con: sqlite3.Connection, row: sqlite3.Row, retry_at: str | None) -> None:
        if row["wait_policy"] == "reject":
            self._finish(con, row, "failed", error="temporarily_unavailable")
            return
        when = self.clock() + timedelta(seconds=self.retry_seconds)
        if retry_at:
            try:
                when = max(when, _date(retry_at))
            except GatewayError:
                pass
        con.execute(
            "UPDATE queue_jobs SET state='waiting',next_retry_at=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,error_code='temporarily_unavailable' WHERE request_key=?",
            (stamp(when), row["request_key"]),
        )

    def tick(self, worker_id: str) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", worker_id):
            raise GatewayError("invalid_request", "worker id must be opaque")
        row = self._claim(worker_id)
        if row is None:
            return None
        try:
            data = self._decrypt(row["payload"], row["request_key"], "payload")
            plan = self.gateway.explain(data)
        except GatewayError:
            with self._tx() as con:
                current = self._owned(con, row)
                if current:
                    self._finish(con, current, "failed", error="queue_task_invalid")
            return self.status(row["request_key"])
        with self._tx() as con:
            current = self._owned(con, row)
            if current is None:
                return self._view(con, self._row(con, row["request_key"]))
            if plan.get("selected_target_id") is None:
                if plan.get("temporary") and not plan.get("permanent_rejection"):
                    self._wait(con, current, plan.get("next_retry_at"))
                else:
                    self._finish(con, current, "failed", error="no_compatible_route")
                return self._view(con, self._row(con, row["request_key"]))
            budget_used = self._budget_used(con, row["request_key"])
            if budget_used >= current["max_attempts"]:
                self._finish(con, current, "failed", error="attempt_limit")
                return self._view(con, self._row(con, row["request_key"]))
            execution = "q-" + uuid.uuid4().hex
            attempt = current["attempt_count"] + 1
            con.execute(
                "INSERT INTO queue_attempts VALUES(?,?,?,?)",
                (row["request_key"], attempt, execution, stamp(self.clock())),
            )
            con.execute(
                "UPDATE queue_jobs SET attempt_count=?,execution_key=?,run_started=1,execution_until=? WHERE request_key=?",
                (
                    attempt,
                    execution,
                    stamp(self.clock() + timedelta(seconds=self.execution_lease_seconds)),
                    row["request_key"],
                ),
            )
        data["request_key"] = execution
        data["max_attempts"] = current["max_attempts"] - budget_used
        try:

            def dispatch_guard(con: sqlite3.Connection) -> None:
                current = self._owned(con, row)
                if current is None or current["execution_key"] != execution:
                    raise GatewayError("unavailable", "queue lease is no longer owned")
                now = stamp(self.clock())
                if current["expires_at"] <= now or (
                    current["deadline"] and current["deadline"] <= now
                ):
                    raise GatewayError("unavailable", "queue deadline expired")
                if current["execution_until"] is None or current["execution_until"] <= now:
                    raise GatewayError("unavailable", "bounded queue execution lease expired")
                con.execute(
                    "UPDATE queue_jobs SET lease_until=? WHERE request_key=? AND lease_token=?",
                    (current["execution_until"], row["request_key"], row["lease_token"]),
                )

            outcome = self.gateway.run(data, dispatch_guard=dispatch_guard)
        except GatewayError as exc:
            with self._tx() as con:
                current = self._owned(con, row)
                if current:
                    if self._execution(con, current) != "unsent":
                        self._finish(con, current, "unknown", error="execution_uncertain")
                    elif (
                        exc.code
                        in {"unavailable", "temporarily_unavailable", "dispatch_unavailable"}
                        and self._budget_used(con, row["request_key"]) < current["max_attempts"]
                    ):
                        self._wait(con, current, exc.wait_until)
                    else:
                        self._finish(con, current, "failed", error="execution_rejected")
            return self.status(row["request_key"])
        except Exception:  # noqa: BLE001 - execution failures must retain no-replay state
            # Exception text can contain input, credentials or response. Never persist it.
            with self._tx() as con:
                current = self._owned(con, row)
                if current:
                    self._finish(con, current, "unknown", error="execution_uncertain")
            return self.status(row["request_key"])
        retry_plan = self.gateway.explain(data) if outcome.get("state") == "quota_exhausted" else {}
        with self._tx() as con:
            current = self._row(con, row["request_key"])
            state = outcome.get("state")
            same_execution = current["execution_key"] == execution
            running_owner = (
                current["state"] == "running" and current["lease_token"] == row["lease_token"]
            )
            late_known_result = (
                current["state"] == "unknown" and current["error_code"] == "execution_uncertain"
            )
            if (
                state in {"completed", "completed_usage_unknown"}
                and same_execution
                and (running_owner or late_known_result)
            ):
                # Dispatch expiry fences future calls; it does not discard a
                # successfully received answer from this exact execution.
                self._finish(con, current, "completed", result=outcome)
                return self._view(con, self._row(con, row["request_key"]))
            self._maintenance(con)
            current = self._owned(con, row)
            if current is None:
                return self._view(con, self._row(con, row["request_key"]))
            if (
                state in {"dispatched", "unknown", "preparing"}
                or self._execution(con, current) != "unsent"
            ):
                self._finish(con, current, "unknown", error="execution_uncertain", result=outcome)
            elif (
                (outcome.get("temporary") or state == "quota_exhausted")
                and self._execution(con, current) == "unsent"
                and self._budget_used(con, row["request_key"]) < current["max_attempts"]
                and not retry_plan.get("permanent_rejection")
            ):
                self._wait(
                    con, current, retry_plan.get("next_retry_at") or outcome.get("next_retry_at")
                )
            else:
                self._finish(con, current, "failed", error="execution_failed", result=outcome)
            return self._view(con, self._row(con, row["request_key"]))
