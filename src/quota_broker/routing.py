"""Persistent health and expiring quota observations, separate from execution ledger."""

import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any

from .config import QUOTA_WINDOWS, RESOURCE_METRICS
from .core import canonical, stamp


def observation_freshness(item: dict[str, Any], now: datetime) -> str:
    """An observed balance cannot survive its validity or known reset boundary."""
    try:
        since = datetime.fromisoformat(item["as_of"])
        until = datetime.fromisoformat(item["valid_until"])
        reset = datetime.fromisoformat(item["reset_at"]) if item.get("reset_at") else None
        if since.tzinfo is None or until.tzinfo is None or (reset and reset.tzinfo is None):
            return "invalid_interval"
        if until <= since or (reset is not None and reset < since):
            return "invalid_interval"
        # A day counter with a month-long reset is contradictory evidence.
        # Allow a 25-hour civil day plus clock margin; never roll it to a full balance.
        window_limit = {
            "rolling_minute": 120,
            "rolling_hour": 7200,
            "day": 26 * 3600,
            "month": 32 * 86400,
        }.get(str(item.get("window")))
        if (
            window_limit is not None
            and reset is not None
            and (reset - since).total_seconds() > window_limit
        ):
            return "window_mismatch"
        if now < since:
            return "not_yet_valid"
        if reset is not None and now >= reset:
            return "reset_reached"
        if now >= until:
            return "validity_expired"
        if window_limit is not None and now >= since + timedelta(seconds=window_limit):
            return "window_elapsed"
        return "current"
    except (KeyError, TypeError, ValueError):
        return "invalid_interval"


class RoutingState:
    def __init__(self, db: str):
        self.db = db
        with closing(sqlite3.connect(db)) as con, con:
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
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
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
            elif status in {429, 503} and (status == 429 or retry_after is not None):
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
        with closing(sqlite3.connect(self.db)) as con, con:
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
        if observation["metric"] not in RESOURCE_METRICS | {"tokens"} or observation[
            "window"
        ] not in QUOTA_WINDOWS | {"budget"}:
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
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
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
        resource_bounds: dict[str, int] | None = None,
    ) -> None:
        for item in (
            observations if observations is not None else self.observations(con, scope, now)
        ):
            if not item["current"]:
                continue
            metric = "total_tokens" if item["metric"] == "tokens" else item["metric"]
            resources = resource_bounds or {}
            cost: int | None
            if metric == "requests":
                cost = 1
            elif resource_bounds is not None:
                cost = resources.get(metric)
                if metric == "neurons" and neurons is not None:
                    cost = max(neurons, cost or 0)
            elif metric == "input_tokens":
                cost = bound
            elif metric == "output_tokens":
                cost = output
            elif metric == "total_tokens":
                cost = bound + output
            elif metric == "neurons":
                cost = neurons
            else:
                cost = None
            if cost is None:
                raise ValueError("quota observation requires an explicit resource bound")
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
        freshness = observation_freshness(item, now)
        return {
            **item,
            "current": freshness == "current",
            "freshness_reason": freshness,
            "local_observation_hold": held,
            "effective_remaining": max(0, item["remaining"] - held)
            if item["remaining"] is not None and freshness == "current"
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
                "valid_until": stamp((as_of or now) + timedelta(days=1))
                if metric == "requests" and int(remaining) > 0
                else until,
                "reset_at": until,
                "provenance": "observed",
                "source": "groq_rate_limit_headers",
                "confidence": "provider_reported",
            }
        )
    return result


PROFILES = frozenset({"general", "coding", "reasoning", "translation", "extraction"})
ALGORITHM = "expiry_utility_v1"


def initialize(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS model_quality_evaluations (
        provider TEXT NOT NULL, model TEXT NOT NULL, profile TEXT NOT NULL,
        evaluation_id TEXT NOT NULL, passed INTEGER NOT NULL, total INTEGER NOT NULL,
        as_of TEXT NOT NULL, valid_until TEXT NOT NULL,
        PRIMARY KEY(provider,model,profile,evaluation_id))""")


def observe_quality(
    con: sqlite3.Connection,
    provider: str,
    model: str,
    profile: str,
    *,
    passed: int,
    total: int,
    evaluation_id: str,
    as_of: datetime,
    valid_until: datetime,
    now: datetime,
) -> None:
    if (
        profile not in PROFILES
        or type(passed) is not int
        or type(total) is not int
        or not 0 <= passed <= total <= 10000
        or total == 0
        or not isinstance(evaluation_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", evaluation_id)
        or any(v.tzinfo is None for v in (as_of, valid_until, now))
        or not as_of <= now < valid_until <= as_of + timedelta(days=90)
    ):
        raise ValueError("invalid quality evaluation")
    values = (
        provider,
        model,
        profile,
        evaluation_id,
        passed,
        total,
        stamp(as_of),
        stamp(valid_until),
    )
    old = con.execute(
        "SELECT * FROM model_quality_evaluations WHERE provider=? AND model=? AND profile=? AND evaluation_id=?",
        values[:4],
    ).fetchone()
    if old is not None and tuple(old) != values:
        raise ValueError("quality evaluation identity conflict")
    con.execute("INSERT OR IGNORE INTO model_quality_evaluations VALUES(?,?,?,?,?,?,?,?)", values)


def _bounded(value: Any, default: float) -> float:
    return (
        min(1.0, max(0.0, float(value)))
        if type(value) in {int, float} and math.isfinite(value)
        else default
    )


def score(
    con: sqlite3.Connection,
    *,
    target_id: str,
    provider: str,
    model: str,
    profile: str,
    task_profiles: tuple[str, ...],
    now: datetime,
    factors: dict[str, Any],
) -> dict[str, Any]:
    """Maximize weighted expected value while urgently consuming expiring capacity.

    The 60% expiry weight is a declared user preference, not a learned benchmark.
    A one-day urgency horizon differentiates nearby daily resets continuously.
    Beta posteriors regularize sparse observations and expose sample counts.
    """
    quality = con.execute(
        "SELECT COALESCE(sum(passed),0),COALESCE(sum(total),0) FROM model_quality_evaluations WHERE provider=? AND model=? AND profile=? AND as_of<=? AND valid_until>?",
        (provider, model, profile, stamp(now), stamp(now)),
    ).fetchone()
    passed, samples = int(quality[0]), int(quality[1])
    # Recent actual attempts are read from the durable ledger. A rejection counts
    # as availability failure; terminal success without token counters still succeeds.
    outcomes = con.execute(
        "SELECT state FROM gateway_attempts WHERE target_id=? AND dispatched_at>=? AND dispatched_at<=? ORDER BY dispatched_at DESC,attempt_no DESC LIMIT 100",
        (target_id, stamp(now - timedelta(days=30)), stamp(now)),
    ).fetchall()
    successes = sum(r[0] in {"completed", "completed_usage_unknown"} for r in outcomes)
    reliability = (successes + 2) / (len(outcomes) + 3)
    # A just-observed failure may precede the attempt projection; do not reward it.
    reliability /= 1 + 0.15 * min(int(factors.get("health_failures") or 0), 10)
    horizon = factors.get("value_loss_seconds")
    urgency = (
        86400 / (86400 + max(0, horizon))
        if isinstance(horizon, (int, float))
        and not isinstance(horizon, bool)
        and math.isfinite(horizon)
        else 0.0
    )
    latency_ms = factors.get("latency_ms")
    latency = (
        1500 / (1500 + latency_ms)
        if isinstance(latency_ms, (int, float))
        and not isinstance(latency_ms, bool)
        and math.isfinite(latency_ms)
        and latency_ms >= 0
        else 0.5
    )
    components = {
        "expiry_urgency": urgency,
        "task_fit": 1.0 if profile in task_profiles else 0.75 if profile == "general" else 0.5,
        "quality": (passed + 1) / (samples + 2),
        "reliability": reliability,
        "free_headroom": _bounded(factors.get("observed_headroom"), 0.25),
        "latency": latency,
        "load": (1 - _bounded(factors.get("local_pressure"), 0))
        / (1 + max(0, int(factors.get("inflight") or 0))),
    }
    utility = (
        0.60 * urgency
        + 0.17 * components["task_fit"] * components["quality"]
        + 0.15 * reliability
        + 0.05 * components["free_headroom"]
        + 0.02 * latency
        + 0.01 * components["load"]
    )
    return {
        "algorithm": ALGORITHM,
        "score": round(100 * utility, 8),
        "components": components,
        "task_profile": profile,
        "quality_basis": "measured" if samples else "unmeasured",
        "quality_samples": samples,
        "reliability_samples": len(outcomes),
    }
