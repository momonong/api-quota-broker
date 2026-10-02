"""Persistent health and expiring quota observations, separate from execution ledger."""

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .core import canonical, stamp


class RoutingState:
    def __init__(self, db: str):
        self.db = db
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS gateway_health (
                    target_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'closed',
                    failures INTEGER NOT NULL DEFAULT 0, until_at TEXT,
                    latency_ms INTEGER, observed_at TEXT, probe_id TEXT, probe_until TEXT,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS quota_observations (
                    scope TEXT NOT NULL, metric TEXT NOT NULL, window TEXT NOT NULL,
                    observation_json TEXT NOT NULL, PRIMARY KEY(scope,metric,window)
                );
                CREATE TABLE IF NOT EXISTS observed_holds (
                    reservation_id TEXT NOT NULL, scope TEXT NOT NULL,
                    metric TEXT NOT NULL, window TEXT NOT NULL, amount INTEGER NOT NULL,
                    at TEXT NOT NULL, PRIMARY KEY(reservation_id,metric,window)
                );
            """)

    def health(self, con: sqlite3.Connection, target_id: str, now: datetime) -> dict[str, Any]:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM gateway_health WHERE target_id=?", (target_id,)).fetchone()
        result: dict[str, Any] = (
            dict(row)
            if row
            else {
                "target_id": target_id,
                "state": "closed",
                "failures": 0,
                "until_at": None,
                "latency_ms": None,
                "observed_at": None,
                "probe_id": None,
                "probe_until": None,
                "reason": None,
            }
        )
        state = result["state"]
        if state == "open" and result["until_at"] is not None and result["until_at"] <= stamp(now):
            result["state"] = "half_open"
        result["probe_busy"] = bool(result["probe_until"] and result["probe_until"] > stamp(now))
        result.pop("probe_id", None)
        return result

    def permit(
        self, con: sqlite3.Connection, target_id: str, reservation_id: str, now: datetime
    ) -> bool:
        health = self.health(con, target_id, now)
        if health["state"] in {"open", "repair_required"} or health["probe_busy"]:
            return False
        if health["state"] == "half_open":
            con.execute(
                "UPDATE gateway_health SET probe_id=?,probe_until=? WHERE target_id=?",
                (reservation_id, stamp(now + timedelta(seconds=150)), target_id),
            )
        return True

    def record(
        self,
        target_id: str,
        now: datetime,
        status: int | None,
        latency_ms: int,
        completed: bool,
        retry_after: int | None,
        error_code: str | None,
        *,
        as_of: datetime | None = None,
    ) -> None:
        with sqlite3.connect(self.db, timeout=15) as con:
            con.execute("BEGIN IMMEDIATE")
            prior = self.health(con, target_id, now)
            observation_time = stamp(as_of or now)
            if prior["observed_at"] is not None and prior["observed_at"] > observation_time:
                return
            failure = prior["failures"] + 1
            if completed:
                state, until, failure, reason = "closed", None, 0, None
            elif status in {401, 403} or (
                status == 404 and error_code in {"google_not_found", "google_model_not_found"}
            ):
                state, until, reason = "repair_required", None, "authentication_or_configuration"
            elif status in {400, 422}:
                state, until, failure, reason = (
                    prior["state"],
                    prior["until_at"],
                    prior["failures"],
                    "task_rejected",
                )
            elif status == 429:
                state, until, reason = (
                    "open",
                    stamp(now + timedelta(seconds=max(1, retry_after or 60))),
                    "rate_limited",
                )
            else:
                # Degraded ranking on first failure; bounded circuit after consecutive failures.
                state = "open" if failure >= 2 else "closed"
                until = (
                    stamp(now + timedelta(seconds=min(60 * 2 ** min(failure - 2, 5), 1800)))
                    if state == "open"
                    else None
                )
                reason = "temporary_failure"
            prior_latency = prior["latency_ms"]
            latency = (
                latency_ms if prior_latency is None else round((prior_latency * 3 + latency_ms) / 4)
            )
            con.execute(
                "INSERT INTO gateway_health(target_id,state,failures,until_at,latency_ms,observed_at,reason) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET state=excluded.state,"
                "failures=excluded.failures,until_at=excluded.until_at,latency_ms=excluded.latency_ms,"
                "observed_at=excluded.observed_at,reason=excluded.reason,probe_id=NULL,probe_until=NULL",
                (target_id, state, failure, until, latency, observation_time, reason),
            )

    def reset_health(self, target_id: str) -> None:
        """Trusted operator entry point. Does not reconcile or release unknown ledger rows."""
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM gateway_health WHERE target_id=?", (target_id,))

    def observe(self, scope: str, observation: dict[str, Any], now: datetime) -> None:
        allowed = {
            "metric",
            "window",
            "remaining",
            "limit",
            "as_of",
            "valid_until",
            "reset_at",
            "provenance",
            "source",
            "confidence",
        }
        if set(observation) != allowed:
            raise ValueError("invalid quota observation fields")
        if observation["metric"] not in {
            "requests",
            "input_tokens",
            "tokens",
            "neurons",
        } or observation["window"] not in {"rolling_minute", "day", "month", "budget"}:
            raise ValueError("invalid quota observation dimension")
        for field in ("remaining", "limit"):
            if observation[field] is not None and (
                type(observation[field]) is not int or not 0 <= observation[field] <= 10**12
            ):
                raise ValueError("invalid quota observation value")
        if (
            observation["remaining"] is not None
            and observation["limit"] is not None
            and observation["remaining"] > observation["limit"]
        ):
            raise ValueError("contradictory quota observation")
        if observation["provenance"] not in {"official", "observed"} or observation[
            "confidence"
        ] not in {"provider_reported", "administrator_verified"}:
            raise ValueError("invalid quota observation evidence")
        if observation["source"] not in {"groq_rate_limit_headers", "trusted_operator"} and not (
            isinstance(observation["source"], str)
            and re.fullmatch(r"adapter:[A-Za-z0-9._-]{1,128}", observation["source"])
        ):
            raise ValueError("unrecognized quota observation source")
        for field in ("as_of", "valid_until", "reset_at"):
            value = observation[field]
            if value is None and field == "reset_at":
                continue
            if not isinstance(value, str):
                raise TypeError("invalid observation timestamp")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError("observation timestamp requires offset")
            observation = {**observation, field: stamp(parsed)}
        if observation["as_of"] > stamp(now) or observation["valid_until"] <= observation["as_of"]:
            raise ValueError("invalid quota observation interval")
        if observation["reset_at"] is not None and observation["reset_at"] < observation["as_of"]:
            raise ValueError("invalid quota reset")
        with sqlite3.connect(self.db, timeout=15) as con:
            con.execute(
                "INSERT INTO quota_observations VALUES(?,?,?,?) ON CONFLICT(scope,metric,window) "
                "DO UPDATE SET observation_json=excluded.observation_json WHERE "
                "json_extract(excluded.observation_json,'$.as_of')>json_extract(quota_observations.observation_json,'$.as_of') OR "
                "(json_extract(excluded.observation_json,'$.as_of')=json_extract(quota_observations.observation_json,'$.as_of') AND "
                "coalesce(json_extract(excluded.observation_json,'$.remaining'),1000000000000)<=coalesce(json_extract(quota_observations.observation_json,'$.remaining'),1000000000000))",
                (scope, observation["metric"], observation["window"], canonical(observation)),
            )

    def hold(
        self,
        con: sqlite3.Connection,
        scope: str,
        reservation_id: str,
        bound: int,
        output: int,
        neurons: int | None,
        now: datetime,
        observations: list[dict[str, Any]] | None = None,
    ) -> None:
        for item in (
            observations if observations is not None else self.observations(con, scope, now)
        ):
            if not item["current"]:
                continue
            cost = (
                1
                if item["metric"] in {"requests", "conversions"}
                else neurons
                if item["metric"] == "neurons"
                else bound + output
                if item["metric"] == "tokens"
                else bound
            )
            if cost is not None:
                con.execute(
                    "INSERT OR REPLACE INTO observed_holds VALUES(?,?,?,?,?,?)",
                    (reservation_id, scope, item["metric"], item["window"], cost, stamp(now)),
                )

    def observations(
        self,
        con: sqlite3.Connection,
        scope: str,
        now: datetime,
        exclude_reservation: str | None = None,
    ) -> list[dict[str, Any]]:
        result = []
        for row in con.execute(
            "SELECT observation_json FROM quota_observations WHERE scope=? ORDER BY metric,window",
            (scope,),
        ):
            item = json.loads(row[0])
            result.append(self.with_holds(con, scope, item, now, exclude_reservation))
        return result

    def with_holds(
        self,
        con: sqlite3.Connection,
        scope: str,
        item: dict[str, Any],
        now: datetime,
        exclude_reservation: str | None = None,
    ) -> dict[str, Any]:
        held = con.execute(
            "SELECT coalesce(sum(h.amount),0) FROM observed_holds h JOIN reservations r ON r.id=h.reservation_id WHERE h.scope=? AND h.metric=? AND h.window=? AND r.id!=? AND (r.state IN ('reserved','dispatched','unknown') OR (r.state IN ('completed','failed') AND h.at>?))",
            (scope, item["metric"], item["window"], exclude_reservation or "", item["as_of"]),
        ).fetchone()[0]
        return {
            **item,
            "current": bool(
                item["as_of"]
                and item["valid_until"]
                and item["as_of"] <= stamp(now) < item["valid_until"]
            ),
            "local_observation_hold": held,
            "effective_remaining": max(0, item["remaining"] - held)
            if item["remaining"] is not None
            else None,
        }

    def capture(
        self,
        provider: str,
        scope: str,
        headers: dict[str, str],
        now: datetime,
        sensitive: tuple[str, ...],
        *,
        as_of: datetime | None = None,
    ) -> None:
        """Compatibility wrapper; Gateway obtains observations through its Registry adapter."""
        if provider != "groq":
            return
        for observation in groq_quota_observations(headers, now, as_of=as_of):
            if not any(value and value in canonical(observation) for value in sensitive):
                self.observe(scope, observation, now)


def groq_quota_observations(
    headers: dict[str, str], now: datetime, *, as_of: datetime | None = None
) -> list[dict[str, Any]]:
    sensitive: tuple[str, ...] = ()
    result: list[dict[str, Any]] = []
    normalized = {key.lower(): value for key, value in headers.items()}
    for suffix, metric, window in (
        ("requests", "requests", "day"),
        ("tokens", "tokens", "rolling_minute"),
    ):
        remaining = normalized.get("x-ratelimit-remaining-" + suffix)
        limit = normalized.get("x-ratelimit-limit-" + suffix)
        reset = normalized.get("x-ratelimit-reset-" + suffix)
        if not isinstance(remaining, str) or not re.fullmatch(r"[0-9]{1,12}", remaining):
            continue
        if any(
            secret and secret in value
            for secret in sensitive
            for value in (remaining, limit or "", reset or "")
        ):
            continue
        if not isinstance(reset, str) or not re.fullmatch(
            r"(?:[0-9]+(?:\.[0-9]+)?[hms]){1,3}", reset
        ):
            continue
        pieces = re.findall(r"([0-9]+(?:\.[0-9]+)?)([hms])", reset)
        seconds = sum(float(value) * {"h": 3600, "m": 60, "s": 1}[unit] for value, unit in pieces)
        if not math.isfinite(seconds) or not 0 < seconds <= 86400:
            continue
        until = stamp(now + timedelta(seconds=seconds))
        if (
            isinstance(limit, str)
            and re.fullmatch(r"[0-9]{1,12}", limit)
            and int(remaining) > int(limit)
        ):
            continue
        result.append(
            {
                "metric": metric,
                "window": window,
                "remaining": int(remaining),
                "limit": int(limit)
                if isinstance(limit, str) and re.fullmatch(r"[0-9]{1,12}", limit)
                else None,
                "as_of": stamp(as_of or now),
                "valid_until": until,
                "reset_at": until,
                "provenance": "observed",
                "source": "groq_rate_limit_headers",
                "confidence": "provider_reported",
            }
        )
    return result
