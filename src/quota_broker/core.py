"""SQLite-backed, cooperative quota admission and lifecycle."""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import MODELS
from .config import Quota, Target


class BrokerError(ValueError):
    def __init__(self, code: str, message: str, wait_until: str | None = None):
        super().__init__(message)
        self.code = code
        self.wait_until = wait_until


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def day_bounds(now: datetime, zone: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(zone))
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_local = (start_local + timedelta(days=1)).replace(fold=0)
    return start_local.astimezone(UTC), next_local.astimezone(UTC)


class Broker:
    def __init__(
        self, db: str | Path, targets: tuple[Target, ...], clock: Callable[[], datetime] = utcnow
    ) -> None:
        self.db = str(db)
        self.targets = targets
        self.clock = clock
        self._validate_shared()
        with sqlite3.connect(self.db) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL, target_id TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, dispatched_at TEXT,
                    provider_request_id TEXT, error_status INTEGER,
                    target_snapshot TEXT, shared_scope TEXT
                );
                CREATE TABLE IF NOT EXISTS charges (
                    reservation_id TEXT NOT NULL, bucket TEXT NOT NULL,
                    metric TEXT NOT NULL, amount INTEGER NOT NULL,
                    at TEXT NOT NULL, day_start TEXT,
                    PRIMARY KEY (reservation_id, bucket),
                    FOREIGN KEY(reservation_id) REFERENCES reservations(id)
                );
                CREATE INDEX IF NOT EXISTS charges_bucket_at ON charges(bucket, at);
                CREATE TABLE IF NOT EXISTS reports (
                    report_key TEXT PRIMARY KEY, reservation_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cooldowns (
                    target_id TEXT PRIMARY KEY, until_at TEXT NOT NULL
                );
                """
            )
            # v0.1 databases created before route snapshots remain readable.
            # Legacy unsent rows cannot dispatch; legacy active rows block admission
            # until reconciled because their original route is unknowable.
            columns = {row[1] for row in con.execute("PRAGMA table_info(reservations)")}
            for name in ("target_snapshot", "shared_scope"):
                if name not in columns:
                    con.execute(f"ALTER TABLE reservations ADD COLUMN {name} TEXT")

    def _validate_shared(self) -> None:
        definitions: dict[str, Quota] = {}
        shared_limits: dict[str, int] = {}
        for target in self.targets:
            if (target.shared_concurrency_scope is None) != (
                target.shared_concurrency_limit is None
            ):
                raise ValueError("shared concurrency scope and limit must be set together")
            if target.shared_concurrency_scope is not None and (
                not isinstance(target.shared_concurrency_scope, str)
                or not target.shared_concurrency_scope
                or type(target.shared_concurrency_limit) is not int
                or target.shared_concurrency_limit < 1
            ):
                raise ValueError("invalid shared concurrency scope or limit")
            for q in target.quotas:
                prior = definitions.setdefault(q.bucket, q)
                if prior != q:
                    raise ValueError(f"conflicting shared bucket: {q.bucket}")
            if target.shared_concurrency_scope is not None:
                assert target.shared_concurrency_limit is not None
                prior_limit = shared_limits.setdefault(
                    target.shared_concurrency_scope, target.shared_concurrency_limit
                )
                if prior_limit != target.shared_concurrency_limit:
                    raise ValueError("conflicting shared concurrency limit")

    @staticmethod
    def _snapshot(target: Target) -> str:
        return canonical(
            {
                "provider": target.provider,
                "model": target.model,
                "account_id": target.account_id,
                "endpoint": target.endpoint,
                "quotas": sorted((q.__dict__ for q in target.quotas), key=lambda q: q["bucket"]),
                "concurrency_limit": target.concurrency_limit,
                "shared_concurrency_scope": target.shared_concurrency_scope,
                "shared_concurrency_limit": target.shared_concurrency_limit,
                "max_output_tokens": target.max_output_tokens,
            }
        )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def catalog(self) -> list[dict]:
        now = self.clock()
        result = []
        for target in self.targets:
            model = MODELS[target.model]
            result.append(
                {
                    "target_id": target.id,
                    "provider": target.provider,
                    "model": target.model,
                    "author": model.author,
                    "host": model.host,
                    "endpoint": target.endpoint,
                    "origin": model.origin,
                    "capabilities": [model.capability],
                    "context_tokens": model.context_tokens,
                    "max_output_tokens": min(model.max_output_tokens, target.max_output_tokens),
                    "free_kind": model.free_kind,
                    "use_restrictions": model.use_restrictions,
                    "source": model.source,
                    "source_verified_at": model.verified_at,
                    "account_source": target.source,
                    "account_verified_at": (
                        stamp(target.verified_at) if target.verified_at else None
                    ),
                    "account_expires_at": stamp(target.expires_at) if target.expires_at else None,
                    "free_eligible": target.free_eligible,
                    "billing_enabled": target.billing_enabled,
                    "available": target.available(now),
                    "quotas": [q.__dict__ for q in target.quotas],
                    "concurrency_limit": target.concurrency_limit,
                    "shared_concurrency_scope": target.shared_concurrency_scope,
                    "shared_concurrency_limit": target.shared_concurrency_limit,
                }
            )
        return result

    def _cost(self, q: Quota, input_bound: int, neuron_bound: int | None) -> int:
        if q.metric == "requests":
            return 1
        if q.metric == "input_tokens":
            return input_bound
        if neuron_bound is None:
            raise BrokerError("invalid_request", "Cloudflare requires a conservative Neurons bound")
        return neuron_bound

    def _used(
        self, con: sqlite3.Connection, q: Quota, now: datetime, cost: int
    ) -> tuple[int, str | None]:
        if cost > q.limit:
            return q.limit, None
        if q.window == "rolling_minute":
            since = stamp(now - timedelta(seconds=60))
            rows = con.execute(
                "SELECT amount, at FROM charges WHERE bucket=? AND at>? AND amount>0",
                (q.bucket, since),
            ).fetchall()
            used = sum(row["amount"] for row in rows)
            remaining = used
            wait = None
            events = [
                (row["amount"], datetime.fromisoformat(row["at"]) + timedelta(seconds=60))
                for row in rows
            ]
            for amount, expires in sorted(events, key=lambda item: item[1]):
                remaining -= amount
                if remaining + cost <= q.limit:
                    wait = expires
                    break
        else:
            start, reset = day_bounds(now, q.timezone)
            rows = con.execute(
                "SELECT amount FROM charges WHERE bucket=? AND day_start=? AND amount>0",
                (q.bucket, stamp(start)),
            ).fetchall()
            used = sum(row["amount"] for row in rows)
            wait = reset
        return used, stamp(wait) if wait else None

    def _expire_unsent(self, con: sqlite3.Connection, now: datetime) -> None:
        ids = [
            row["id"]
            for row in con.execute(
                "SELECT id FROM reservations WHERE state='reserved' AND expires_at<=?",
                (stamp(now),),
            )
        ]
        for reservation_id in ids:
            con.execute("UPDATE reservations SET state='expired' WHERE id=?", (reservation_id,))
            con.execute("UPDATE charges SET amount=0 WHERE reservation_id=?", (reservation_id,))

    def _view(self, con: sqlite3.Connection, reservation_id: str) -> dict:
        row = con.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        if row is None:
            raise BrokerError("not_found", "reservation not found")
        snapshot = json.loads(row["target_snapshot"]) if row["target_snapshot"] else None
        target = next((t for t in self.targets if t.id == row["target_id"]), None)
        route_current = bool(
            snapshot and target and row["target_snapshot"] == self._snapshot(target)
        )
        # Never offer an obsolete route to an unsent client. Sent requests retain
        # their original route for diagnosis and later usage reconciliation.
        endpoint = (
            snapshot["endpoint"]
            if snapshot and (row["state"] != "reserved" or route_current)
            else None
        )
        charges = con.execute(
            "SELECT bucket,metric,amount,at,day_start FROM charges WHERE reservation_id=?",
            (reservation_id,),
        ).fetchall()
        return {
            "reservation_id": row["id"],
            "target_id": row["target_id"],
            "provider": snapshot["provider"] if snapshot else None,
            "model": snapshot["model"] if snapshot else None,
            "endpoint": endpoint,
            "route_current": route_current,
            "route_evidence": "snapshot" if snapshot else "legacy_missing",
            "original_quotas": snapshot["quotas"] if snapshot else None,
            "shared_concurrency_scope": snapshot["shared_concurrency_scope"] if snapshot else None,
            "charges": [dict(charge) for charge in charges],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "dispatched_at": row["dispatched_at"],
            "provider_request_id": row["provider_request_id"],
            "error_status": row["error_status"],
        }

    def reserve(self, data: dict) -> dict:
        allowed = {
            "request_key",
            "capability",
            "model",
            "input_token_bound",
            "max_output_tokens",
            "neuron_bound",
        }
        if set(data) - allowed or not isinstance(data.get("request_key"), str):
            raise BrokerError("invalid_request", "invalid reservation fields")
        if not data["request_key"] or len(data["request_key"]) > 160:
            raise BrokerError("invalid_request", "invalid request key")
        if data.get("capability") != "text_generation":
            raise BrokerError("unavailable", "unsupported capability")
        input_bound = data.get("input_token_bound")
        output_max = data.get("max_output_tokens")
        neuron_bound = data.get("neuron_bound")
        if (
            type(input_bound) is not int
            or input_bound < 1
            or type(output_max) is not int
            or output_max < 1
            or (neuron_bound is not None and (type(neuron_bound) is not int or neuron_bound < 1))
        ):
            raise BrokerError("invalid_request", "positive integer bounds required")
        now = self.clock()
        fingerprint = digest(data)
        with self._tx() as con:
            self._expire_unsent(con, now)
            existing = con.execute(
                "SELECT id, fingerprint FROM reservations WHERE request_key=?",
                (data["request_key"],),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise BrokerError("conflict", "request key used for different reservation")
                return self._view(con, existing["id"])
            legacy_active = con.execute(
                "SELECT 1 FROM reservations WHERE target_snapshot IS NULL "
                "AND state IN ('reserved','dispatched','unknown') LIMIT 1"
            ).fetchone()
            if legacy_active:
                raise BrokerError(
                    "unavailable", "legacy active reservation requires reconciliation"
                )
            waits = []
            candidates = sorted(self.targets, key=lambda t: (t.priority, t.id))
            for target in candidates:
                model = MODELS[target.model]
                if (
                    data.get("model") not in (None, target.model)
                    or not target.available(now)
                    or model.capability != data["capability"]
                    or input_bound + output_max > model.context_tokens
                    or output_max > target.max_output_tokens
                ):
                    continue
                current_snapshot = self._snapshot(target)
                changed_active = con.execute(
                    "SELECT 1 FROM reservations WHERE target_id=? AND target_snapshot!=? "
                    "AND state IN ('reserved','dispatched','unknown') LIMIT 1",
                    (target.id, current_snapshot),
                ).fetchone()
                if changed_active:
                    continue
                cooldown = con.execute(
                    "SELECT until_at FROM cooldowns WHERE target_id=?", (target.id,)
                ).fetchone()
                cooldown_until = (
                    cooldown["until_at"] if cooldown and cooldown["until_at"] > stamp(now) else None
                )
                active = con.execute(
                    "SELECT count(*) FROM reservations WHERE target_id=? "
                    "AND state IN ('reserved','dispatched','unknown')",
                    (target.id,),
                ).fetchone()[0]
                if active >= target.concurrency_limit:
                    continue
                if target.shared_concurrency_scope is not None:
                    shared_active = con.execute(
                        "SELECT count(*) FROM reservations WHERE shared_scope=? "
                        "AND state IN ('reserved','dispatched','unknown')",
                        (target.shared_concurrency_scope,),
                    ).fetchone()[0]
                    if shared_active >= target.shared_concurrency_limit:
                        continue
                costs = []
                blocked_until = [cooldown_until] if cooldown_until else []
                indefinite = False
                for q in target.quotas:
                    cost = self._cost(q, input_bound, neuron_bound)
                    used, wait = self._used(con, q, now, cost)
                    if used + cost > q.limit:
                        if wait:
                            blocked_until.append(wait)
                        else:
                            indefinite = True
                    costs.append((q, cost))
                if indefinite or blocked_until:
                    if not indefinite:
                        waits.append(max(blocked_until))
                    continue
                else:
                    reservation_id = str(uuid.uuid4())
                    expires = now + timedelta(seconds=30)
                    con.execute(
                        "INSERT INTO reservations(id,request_key,fingerprint,target_id,state,created_at,"
                        "expires_at,target_snapshot,shared_scope) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            reservation_id,
                            data["request_key"],
                            fingerprint,
                            target.id,
                            "reserved",
                            stamp(now),
                            stamp(expires),
                            current_snapshot,
                            target.shared_concurrency_scope,
                        ),
                    )
                    for q, cost in costs:
                        start = stamp(day_bounds(now, q.timezone)[0]) if q.window == "day" else None
                        con.execute(
                            "INSERT INTO charges VALUES(?,?,?,?,?,?)",
                            (reservation_id, q.bucket, q.metric, cost, stamp(now), start),
                        )
                    return self._view(con, reservation_id)
            raise BrokerError(
                "unavailable", "no verified free capacity", min(waits) if waits else None
            )

    def dispatch(self, reservation_id: str) -> dict:
        now = self.clock()
        with self._tx() as con:
            self._expire_unsent(con, now)
            view = self._view(con, reservation_id)
            if view["state"] != "reserved":
                raise BrokerError("invalid_transition", "dispatch is allowed only once")
            if not view["route_current"]:
                raise BrokerError("configuration_changed", "reservation route or quota changed")
            target = next(t for t in self.targets if t.id == view["target_id"])
            if not target.available(now):
                raise BrokerError("unavailable", "free eligibility no longer verified")
            cooldown = con.execute(
                "SELECT until_at FROM cooldowns WHERE target_id=?", (target.id,)
            ).fetchone()
            if cooldown and cooldown["until_at"] > stamp(now):
                raise BrokerError("unavailable", "provider cooldown", cooldown["until_at"])
            # Move held charges to dispatch time. A reservation crossing a
            # reset must compete in the new window before it may be sent.
            charges = con.execute(
                "SELECT bucket,amount FROM charges WHERE reservation_id=?", (reservation_id,)
            ).fetchall()
            con.execute("UPDATE charges SET amount=0 WHERE reservation_id=?", (reservation_id,))
            for q in target.quotas:
                amount = next(row["amount"] for row in charges if row["bucket"] == q.bucket)
                used, wait = self._used(con, q, now, amount)
                if used + amount > q.limit:
                    raise BrokerError("unavailable", "quota changed before dispatch", wait)
                day_start = stamp(day_bounds(now, q.timezone)[0]) if q.window == "day" else None
                con.execute(
                    "UPDATE charges SET amount=?,at=?,day_start=? "
                    "WHERE reservation_id=? AND bucket=?",
                    (amount, stamp(now), day_start, reservation_id, q.bucket),
                )
            con.execute(
                "UPDATE reservations SET state='dispatched', dispatched_at=? WHERE id=?",
                (stamp(now), reservation_id),
            )
            return self._view(con, reservation_id)

    def report(self, data: dict) -> dict:
        allowed = {
            "reservation_id",
            "report_key",
            "state",
            "usage",
            "provider_request_id",
            "error_status",
            "retry_after_seconds",
        }
        if set(data) - allowed or not all(
            isinstance(data.get(k), str) and data[k]
            for k in ("reservation_id", "report_key", "state")
        ):
            raise BrokerError("invalid_request", "invalid report")
        if data["state"] not in {"completed", "failed", "unknown"}:
            raise BrokerError("invalid_request", "invalid report state")
        status = data.get("error_status")
        retry = data.get("retry_after_seconds")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise BrokerError("invalid_request", "invalid HTTP status")
        if retry is not None and (type(retry) is not int or not 0 <= retry <= 86_400):
            raise BrokerError("invalid_request", "invalid retry delay")
        usage = data.get("usage")
        if data["state"] == "unknown":
            if usage not in (None, {}):
                raise BrokerError("invalid_request", "unknown cannot assert usage")
        elif not isinstance(usage, dict):
            raise BrokerError("invalid_request", "known outcome requires usage")
        fingerprint = digest(data)
        now = self.clock()
        with self._tx() as con:
            old = con.execute(
                "SELECT reservation_id,fingerprint FROM reports WHERE report_key=?",
                (data["report_key"],),
            ).fetchone()
            if old:
                if (
                    old["reservation_id"] != data["reservation_id"]
                    or old["fingerprint"] != fingerprint
                ):
                    raise BrokerError("conflict", "report key reused with different content")
                return self._view(con, data["reservation_id"])
            view = self._view(con, data["reservation_id"])
            if view["state"] not in {"dispatched", "unknown"}:
                raise BrokerError("invalid_transition", "report requires dispatched or unknown")
            if view["state"] == "unknown" and data["state"] == "unknown":
                raise BrokerError("invalid_transition", "unknown already recorded")
            charges = con.execute(
                "SELECT bucket,metric,amount FROM charges WHERE reservation_id=?",
                (data["reservation_id"],),
            ).fetchall()
            if data["state"] != "unknown":
                assert isinstance(usage, dict)
                required = {row["metric"] for row in charges}
                if set(usage) != required or any(
                    type(v) is not int or v < 0 for v in usage.values()
                ):
                    raise BrokerError(
                        "invalid_request", "usage must include every actual quota metric"
                    )
                if usage.get("requests") != 1:
                    raise BrokerError("invalid_request", "dispatched request counts as one request")
                for row in charges:
                    con.execute(
                        "UPDATE charges SET amount=? WHERE reservation_id=? AND bucket=?",
                        (usage[row["metric"]], data["reservation_id"], row["bucket"]),
                    )
            con.execute(
                "UPDATE reservations SET state=?,provider_request_id=?,error_status=? WHERE id=?",
                (data["state"], data.get("provider_request_id"), status, data["reservation_id"]),
            )
            if status == 429:
                # No automatic retry. A missing Retry-After uses a conservative one-minute cooldown.
                seconds = retry if retry is not None else 60
                until = stamp(now + timedelta(seconds=seconds))
                prior = con.execute(
                    "SELECT until_at FROM cooldowns WHERE target_id=?", (view["target_id"],)
                ).fetchone()
                if prior is None or until > prior["until_at"]:
                    con.execute(
                        "INSERT INTO cooldowns(target_id,until_at) VALUES(?,?) "
                        "ON CONFLICT(target_id) DO UPDATE SET until_at=excluded.until_at",
                        (view["target_id"], until),
                    )
            con.execute(
                "INSERT INTO reports VALUES(?,?,?)",
                (data["report_key"], data["reservation_id"], fingerprint),
            )
            return self._view(con, data["reservation_id"])

    def status(self, reservation_id: str) -> dict:
        with self._tx() as con:
            self._expire_unsent(con, self.clock())
            return self._view(con, reservation_id)
