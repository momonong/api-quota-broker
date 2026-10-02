"""Authenticated gateway: content lives only during one request, metadata in SQLite."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import SecretInventory, Target
from .core import Broker, BrokerError, canonical, stamp, utcnow
from .gateway_providers import (
    ProviderError,
    ProviderHeaders,
    ProviderPhaseTimeout,
    chat_completion_metadata,
    provider_http,
    safe_http_error_code,
    safe_response_diagnostics,
    safe_transport_diagnostics,
)
from .registry import Registry
from .retry import parse_retry_after
from .routing import RoutingState

ProviderTransport = Callable[
    [str, dict[str, str], dict[str, object], float], tuple[int, dict[str, str], bytes]
]
SecretResolver = Callable[[str], str]
CAPACITY_ORDER = {"short_renewable": 0, "unknown": 1, "one_time_gift": 2}
COLUMNS = (
    "request_key",
    "state",
    "reservation_id",
    "target_id",
    "provider",
    "model",
    "route_reason",
    "created_at",
    "dispatched_at",
    "completed_at",
    "latency_ms",
    "http_status",
    "error_code",
    "provider_request_id",
    "estimated_input_tokens",
    "reported_input_tokens",
    "reported_output_tokens",
    "reported_neurons",
    "usage_source",
    "finish_reason",
    "response_truncated",
    "diagnostics_json",
)
ATTEMPT_COLUMNS = (
    "attempt_no",
    "reservation_id",
    "target_id",
    "provider",
    "model",
    "state",
    "created_at",
    "dispatched_at",
    "completed_at",
    "latency_ms",
    "http_status",
    "error_code",
    "provider_request_id",
    "estimated_input_tokens",
    "reported_input_tokens",
    "reported_output_tokens",
    "reported_neurons",
    "usage_source",
    "input_bytes",
    "finish_reason",
    "response_truncated",
    "diagnostics_json",
)


class GatewayError(ValueError):
    def __init__(self, code: str, message: str, wait_until: str | None = None):
        super().__init__(message)
        self.code = code
        self.wait_until = wait_until


def validate_task(raw: dict[str, Any], registry: Registry | None = None) -> dict[str, Any]:
    registry = registry or Registry.builtin()
    allowed = {
        "request_key",
        "capability",
        "input",
        "max_output_tokens",
        "provider",
        "model",
        "source_language",
        "target_language",
        "neuron_bound",
        "requirements",
        "priority",
        "deadline",
        "wait_policy",
        "max_attempts",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise GatewayError("invalid_request", "invalid task fields")
    key = raw.get("request_key")
    content = raw.get("input")
    capability = raw.get("capability")
    output = raw.get("max_output_tokens")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", key):
        raise GatewayError("invalid_request", "request_key must be an opaque URI-safe id")
    if not isinstance(capability, str) or capability not in {
        "text_generation",
        "translation",
        "ocr",
    }:
        raise GatewayError("invalid_request", "unsupported capability")
    if not isinstance(content, str) or not content:
        raise GatewayError("invalid_request", "input must be nonempty")
    if capability == "ocr":
        if len(content) > 48_000 or not content.isascii():
            raise GatewayError("invalid_request", "OCR image exceeds local size limit")
        try:
            image = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GatewayError("invalid_request", "OCR input must be base64") from exc
        if not 0 < len(image) <= 36_000 or not image.startswith(
            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
        ):
            raise GatewayError("invalid_request", "OCR requires a small PNG or JPEG image")
        if output is None:
            output = 1
        if output != 1:
            raise GatewayError("invalid_request", "OCR max_output_tokens must be 1")
    elif len(content.encode("utf-8")) > 32_768:
        raise GatewayError("invalid_request", "input must be at most 32768 bytes")
    if type(output) is not int or not 1 <= output <= 4096:
        raise GatewayError("invalid_request", "max_output_tokens must be 1..4096")
    provider, model = raw.get("provider"), raw.get("model")
    if provider is not None and (
        not isinstance(provider, str) or provider not in registry.providers
    ):
        raise GatewayError("invalid_request", "unsupported provider")
    if model is not None:
        try:
            spec = registry.resolve(model, provider)
        except (ValueError, TypeError) as exc:
            raise GatewayError("invalid_request", "unknown or ambiguous provider/model") from exc
        if spec.capability != capability:
            raise GatewayError("invalid_request", "model/capability mismatch")
    source, target = raw.get("source_language"), raw.get("target_language")
    if capability == "translation":
        if (
            any(
                not isinstance(language, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", language)
                for language in (source, target)
            )
            or source == target
        ):
            raise GatewayError(
                "invalid_request", "translation requires distinct language identifiers"
            )
        candidates = [
            spec
            for spec in registry.models.values()
            if spec.capability == capability
            and provider in (None, spec.provider)
            and model in (None, spec.model)
        ]
        accepted = False
        for candidate in candidates:
            try:
                registry.admit(candidate, raw)
                accepted = True
                break
            except ValueError:
                continue
        if not accepted:
            raise GatewayError(
                "invalid_request", "translation does not meet selected adapter policy"
            )
    elif source is not None or target is not None:
        raise GatewayError("invalid_request", "language fields require translation")
    neuron_bound = raw.get("neuron_bound")
    if neuron_bound is not None and (type(neuron_bound) is not int or neuron_bound < 1):
        raise GatewayError("invalid_request", "neuron_bound must be positive")
    if neuron_bound is not None and capability != "text_generation":
        raise GatewayError("invalid_request", "neuron_bound only applies to text generation")
    extras: dict[str, Any] = {}
    if "requirements" in raw:
        requirements = raw["requirements"]
        if not isinstance(requirements, dict) or set(requirements) - {"features"}:
            raise GatewayError("invalid_request", "invalid task requirements")
        features = requirements.get("features", [])
        if (
            not isinstance(features, list)
            or len(features) > 16
            or any(
                not isinstance(f, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", f)
                for f in features
            )
        ):
            raise GatewayError("invalid_request", "invalid task features")
        extras["requirements"] = {"features": sorted(set(features))}
    if "priority" in raw:
        if type(raw["priority"]) is not int or not -100 <= raw["priority"] <= 100:
            raise GatewayError("invalid_request", "priority must be -100..100")
        extras["priority"] = raw["priority"]
    if "deadline" in raw:
        try:
            deadline = datetime.fromisoformat(raw["deadline"])
            if deadline.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise GatewayError("invalid_request", "deadline requires offset") from exc
        extras["deadline"] = stamp(deadline)
    if "wait_policy" in raw:
        if not isinstance(raw["wait_policy"], str) or raw["wait_policy"] not in {"wait", "reject"}:
            raise GatewayError("invalid_request", "wait_policy must be wait or reject")
        extras["wait_policy"] = raw["wait_policy"]
    if "max_attempts" in raw:
        if type(raw["max_attempts"]) is not int or not 1 <= raw["max_attempts"] <= 32:
            raise GatewayError("invalid_request", "max_attempts must be 1..32")
        extras["max_attempts"] = raw["max_attempts"]
    return {
        "request_key": key,
        "capability": capability,
        "input": content,
        "max_output_tokens": output,
        "provider": provider,
        "model": model,
        "source_language": source,
        "target_language": target,
        "neuron_bound": neuron_bound,
        **extras,
    }


class Gateway:
    def __init__(
        self,
        db: str | Path,
        targets: tuple[Target, ...],
        digest_key: bytes,
        secret_resolver: SecretResolver,
        transport: ProviderTransport = provider_http,
        clock: Callable[[], datetime] = utcnow,
        secret_inventory: SecretInventory | None = None,
        registry: Registry | None = None,
    ) -> None:
        if len(digest_key) < 32:
            raise ValueError("persisted HMAC key must be at least 32 bytes")
        if any(target.secret_ref is None for target in targets):
            raise ValueError("every gateway target requires a secret reference")
        self.db = str(db)
        self.targets = targets
        self.digest_key = digest_key
        self.secret_resolver = secret_resolver
        self.transport = transport
        self.clock = clock
        self.secret_inventory = secret_inventory
        self.registry = registry or Registry.builtin()
        self.broker = Broker(db, targets, clock, self.registry)
        self.routing = RoutingState(self.db)
        self.broker.routing_policy = self._route_plan
        self.broker.dispatch_policy = self._permit_dispatch
        self.broker.reservation_policy = self._hold_observations
        with sqlite3.connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""
                CREATE TABLE IF NOT EXISTS gateway_tasks (
                    request_key TEXT PRIMARY KEY, payload_hmac TEXT NOT NULL,
                    state TEXT NOT NULL, reservation_id TEXT, target_id TEXT,
                    provider TEXT, model TEXT, route_reason TEXT,
                    created_at TEXT NOT NULL, dispatched_at TEXT, completed_at TEXT,
                    latency_ms INTEGER, http_status INTEGER, error_code TEXT,
                    provider_request_id TEXT, estimated_input_tokens INTEGER,
                    reported_input_tokens INTEGER, reported_output_tokens INTEGER,
                    reported_neurons INTEGER, usage_source TEXT
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS gateway_usage_at ON gateway_tasks(dispatched_at, provider, model)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS gateway_attempts (
                    request_key TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                    reservation_id TEXT NOT NULL UNIQUE, target_id TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, dispatched_at TEXT, completed_at TEXT,
                    latency_ms INTEGER, http_status INTEGER, error_code TEXT,
                    provider_request_id TEXT, estimated_input_tokens INTEGER,
                    reported_input_tokens INTEGER, reported_output_tokens INTEGER,
                    reported_neurons INTEGER, usage_source TEXT, input_bytes INTEGER,
                    PRIMARY KEY(request_key, attempt_no)
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS gateway_attempts_usage_at "
                "ON gateway_attempts(dispatched_at, provider, model)"
            )
            # Additive, serialized and repeatable: old rows/holds retain their semantics.
            for table in ("gateway_tasks", "gateway_attempts"):
                existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                for column, kind in (
                    ("finish_reason", "TEXT"),
                    ("response_truncated", "INTEGER"),
                    ("diagnostics_json", "TEXT"),
                ):
                    if column not in existing:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def _hmac(self, data: dict[str, Any]) -> str:
        return hmac.new(self.digest_key, canonical(data).encode(), hashlib.sha256).hexdigest()

    def validate_task(self, raw: dict[str, Any]) -> dict[str, Any]:
        return validate_task(raw, self.registry)

    def _hold_observations(
        self, con: sqlite3.Connection, target: Target, reservation_id: str, data: dict[str, Any]
    ) -> None:
        scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
        neurons = target.neurons(
            data["input_token_bound"],
            data["max_output_tokens"],
            self.clock(),
            data.get("neuron_bound"),
        )
        self.routing.hold(
            con,
            scope,
            reservation_id,
            data["input_token_bound"],
            data["max_output_tokens"],
            neurons,
            self.clock(),
            self._quota_observations(con, target, self.clock()),
        )

    def _quota_observations(
        self,
        con: sqlite3.Connection,
        target: Target,
        now: datetime,
        exclude_reservation: str | None = None,
    ) -> list[dict[str, Any]]:
        scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
        observations = self.routing.observations(con, scope, now, exclude_reservation)
        for fact in target.provider_quota_facts:
            evidence = fact.remaining
            item = {
                "metric": fact.metric,
                "window": fact.window,
                "remaining": evidence.value,
                "limit": fact.limit.value,
                "as_of": stamp(evidence.as_of) if evidence.as_of else None,
                "valid_until": stamp(evidence.valid_until) if evidence.valid_until else None,
                "reset_at": None,
                "source": evidence.source,
                "provenance": evidence.provenance,
                "confidence": "configured_evidence" if evidence.value is not None else "unknown",
            }
            observations.append(self.routing.with_holds(con, scope, item, now, exclude_reservation))
        return observations

    def _permit_dispatch(
        self, con: sqlite3.Connection, target: Target, reservation_id: str
    ) -> None:
        if not self.routing.permit(con, target.id, reservation_id, self.clock()):
            raise BrokerError("unavailable", "provider circuit blocks dispatch")

    def observe_quota(self, target_id: str, observation: dict[str, Any]) -> None:
        target = next((t for t in self.targets if t.id == target_id), None)
        if target is None:
            raise GatewayError("not_found", "target not found")
        scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
        try:
            self.routing.observe(scope, observation, self.clock())
        except (TypeError, ValueError, KeyError) as exc:
            raise GatewayError("invalid_request", "invalid quota observation") from exc

    def reset_health(self, target_id: str) -> None:
        if not any(t.id == target_id for t in self.targets):
            raise GatewayError("not_found", "target not found")
        self.routing.reset_health(target_id)

    def _view(self, request_key: str) -> dict[str, Any]:
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT " + ",".join(COLUMNS) + " FROM gateway_tasks WHERE request_key=?",
                (request_key,),
            ).fetchone()
            attempts = con.execute(
                "SELECT " + ",".join(ATTEMPT_COLUMNS) + " FROM gateway_attempts "
                "WHERE request_key=? ORDER BY attempt_no",
                (request_key,),
            ).fetchall()
        if row is None:
            raise GatewayError("not_found", "task not found")
        result = dict(row)
        result["attempts"] = [dict(attempt) for attempt in attempts]
        for item in [result, *result["attempts"]]:
            saved = item.pop("diagnostics_json")
            item["diagnostics"] = json.loads(saved) if saved else None
            if item["response_truncated"] is not None:
                item["response_truncated"] = bool(item["response_truncated"])
        for attempt in result["attempts"]:
            with sqlite3.connect(self.db) as completion_con:
                finished = completion_con.execute(
                    "SELECT finished_at FROM execution_completion WHERE reservation_id=?",
                    (attempt["reservation_id"],),
                ).fetchone()
            attempt["execution_finished_at"] = finished[0] if finished else None
            ledger_attempt = self.broker.status(attempt["reservation_id"], expire_unsent=False)
            attempt["ledger_state"] = ledger_attempt["state"]
            attempt["ledger_basis"] = (
                "settled_provider_usage"
                if ledger_attempt["state"] in {"completed", "failed"}
                else "rejected_zero_usage"
                if ledger_attempt["state"] == "quota_rejected"
                else "held_estimate"
                if ledger_attempt["state"] in {"dispatched", "unknown"}
                else "unsent"
            )
            attempt["ledger_charges"] = [
                {"bucket": charge["bucket"], "metric": charge["metric"], "amount": charge["amount"]}
                for charge in ledger_attempt["charges"]
            ]
        if result["reservation_id"]:
            # A crash at either boundary is not permission to issue another POST.
            ledger = self.broker.status(result["reservation_id"], expire_unsent=False)
            result["ledger_state"] = ledger["state"]
            result["ledger_dispatched_at"] = ledger["dispatched_at"]
            result["ledger_basis"] = (
                "settled_provider_usage"
                if ledger["state"] in {"completed", "failed"}
                else "held_estimate"
                if ledger["state"] in {"dispatched", "unknown"}
                else "rejected_zero_usage"
                if ledger["state"] == "quota_rejected"
                else "unsent"
            )
            result["ledger_charges"] = [
                {"bucket": charge["bucket"], "metric": charge["metric"], "amount": charge["amount"]}
                for charge in ledger["charges"]
            ]
            if result["state"] in {"preparing", "dispatched"} and ledger["state"] in {
                "dispatched",
                "unknown",
            }:
                result["state"] = "unknown"
            elif (
                result["state"] in {"preparing", "dispatched"}
                and ledger["state"] == "quota_rejected"
            ):
                result["state"] = "quota_rejected"
        else:
            result["ledger_state"] = None
            result["ledger_dispatched_at"] = None
            result["ledger_basis"] = None
            result["ledger_charges"] = []
        return result

    def status(self, request_key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", request_key):
            raise GatewayError("invalid_request", "invalid request key")
        return self._view(request_key)

    def catalog(self) -> list[dict[str, Any]]:
        diagnosis = {row["target_id"]: row for row in self.diagnostics()["targets"]}
        items = self.broker.catalog()
        for item in items:
            current = diagnosis[item["target_id"]]
            item["configured_available"] = item["available"]
            item["available"] = item["available"] and current["state"] != "blocked"
            item["admission_state"] = current["state"]
            item["health"] = current["health"]
            item["quota_observations"] = current["quota_observations"]
            item["features"] = list(self.registry.resolve(item["model"], item["provider"]).features)
        return items

    def _credentials(self, target: Target) -> dict[str, Any]:
        required = sorted({name for name in (target.secret_ref, target.account_id_ref) if name})
        inventory = self.secret_inventory
        current = inventory is not None and inventory.current(self.clock())
        missing = sorted(set(required) - inventory.names) if current and inventory else None
        return {
            "required_names": required,
            "missing_names": missing,
            "state": "missing" if missing else "present" if current else "unknown",
            "verified_at": stamp(inventory.verified_at) if inventory else None,
            "expires_at": stamp(inventory.expires_at) if inventory else None,
        }

    def diagnostics(self) -> dict[str, Any]:
        """Read-only minimum-admission snapshot; no secret lookup or provider I/O."""
        rows: list[dict[str, Any]] = []
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            for target in sorted(self.targets, key=self._sort_key):
                model = self.registry.resolve(target.model, target.provider)
                data = {
                    "provider": None,
                    "model": None,
                    "capability": model.capability,
                    "max_output_tokens": 1,
                    "neuron_bound": None,
                }
                candidate = next(
                    item
                    for item in self._route_plan(con, data, 1)["candidates"]
                    if item["target_id"] == target.id
                )
                reasons = list(candidate["reasons"])
                credentials = self._credentials(target)
                if credentials["state"] == "unknown":
                    reasons.append("credential_inventory_unknown")
                untils = [
                    row[0]
                    for row in con.execute(
                        "SELECT until_at FROM cooldowns WHERE target_id=? UNION ALL "
                        "SELECT until_at FROM quota_scope_cooldowns WHERE scope=?",
                        (
                            target.id,
                            self.broker._quota_scope(json.loads(self.broker._snapshot(target))),
                        ),
                    )
                    if row[0] > stamp(self.clock())
                ]
                rows.append(
                    {
                        "target_id": target.id,
                        "provider": target.provider,
                        "model": target.model,
                        "capability": model.capability,
                        "state": "blocked"
                        if any(reason != "credential_inventory_unknown" for reason in reasons)
                        else "unknown"
                        if reasons
                        else "ready",
                        "reasons": reasons,
                        "credentials": credentials,
                        "cooldown_until": max(untils) if untils else None,
                        "health": candidate["health"],
                        "quota_observations": candidate["quota_observations"],
                        "ranking_factors": candidate["ranking_factors"],
                        "temporary": candidate["temporary"],
                        "next_retry_at": candidate["next_retry_at"],
                        "capacity": target.capacity.view(),
                        "effective_capacity_kind": self._capacity_kind(target),
                    }
                )
        return {
            "as_of": stamp(self.clock()),
            "ready_targets": sum(row["state"] == "ready" for row in rows),
            "targets": rows,
            "basis": "local_minimum_admission_snapshot",
        }

    def recent(
        self,
        *,
        limit: int = 20,
        before: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Stable bounded pagination over content-free task/attempt status."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise GatewayError("invalid_request", "limit must be 1..100")
        if provider is not None and provider not in self.registry.providers:
            raise GatewayError("invalid_request", "invalid provider filter")
        if model is not None and not any(key[1] == model for key in self.registry.models):
            raise GatewayError("invalid_request", "invalid model filter")
        if state is not None and state not in {
            "preparing",
            "dispatched",
            "unknown",
            "completed",
            "completed_usage_unknown",
            "quota_rejected",
            "quota_exhausted",
            "rejected",
        }:
            raise GatewayError("invalid_request", "invalid state filter")
        conditions = []
        params: list[Any] = []
        with sqlite3.connect(self.db) as con:
            if before is not None:
                if not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", before):
                    raise GatewayError("invalid_request", "invalid pagination key")
                cursor = con.execute(
                    "SELECT created_at,request_key FROM gateway_tasks WHERE request_key=?",
                    (before,),
                ).fetchone()
                if cursor is None:
                    raise GatewayError("not_found", "pagination task not found")
                conditions.append("(g.created_at,g.request_key)<(?,?)")
                params.extend(cursor)
            effective_state = (
                "CASE WHEN g.state IN ('preparing','dispatched') AND r.state IN ('dispatched','unknown') "
                "THEN 'unknown' WHEN g.state IN ('preparing','dispatched') AND r.state='quota_rejected' "
                "THEN 'quota_rejected' ELSE g.state END"
            )
            for column, value in (
                ("g.provider", provider),
                ("g.model", model),
                (effective_state, state),
            ):
                if value is not None:
                    conditions.append(column + "=?")
                    params.append(value)
            keys = con.execute(
                "SELECT g.request_key FROM gateway_tasks g LEFT JOIN reservations r ON r.id=g.reservation_id"
                + (" WHERE " + " AND ".join(conditions) if conditions else "")
                + " ORDER BY g.created_at DESC,g.request_key DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        tasks = [self._view(row[0]) for row in keys[:limit]]
        return {
            "tasks": tasks,
            "next_before": tasks[-1]["request_key"] if len(keys) > limit else None,
        }

    def _capacity_kind(self, target: Target) -> str:
        if target.capacity.expires_at and target.capacity.expires_at <= self.clock():
            return "unknown"
        return target.capacity.kind

    def _sort_key(self, target: Target) -> tuple[int, int, int, str]:
        kind = self._capacity_kind(target)
        return (
            CAPACITY_ORDER[kind],
            (target.capacity.refresh_seconds or 0) if kind == "short_renewable" else 0,
            target.priority,
            target.id,
        )

    def _reasons(
        self, con: sqlite3.Connection, target: Target, data: dict[str, Any], bound: int
    ) -> list[str]:
        now = self.clock()
        data = {
            **data,
            "neuron_bound": target.neurons(
                bound, data["max_output_tokens"], now, data.get("neuron_bound")
            ),
        }
        model = self.registry.resolve(target.model, target.provider)
        reasons = []
        if data["provider"] not in (None, target.provider):
            reasons.append("provider_constraint")
        if data["model"] not in (None, target.model):
            reasons.append("model_constraint")
        if model.capability != data["capability"]:
            reasons.append("capability_mismatch")
        elif "input" in data:
            try:
                self.registry.admit(model, data)
            except ValueError:
                reasons.append("adapter_admission")
        if not set(data.get("requirements", {}).get("features", [])) <= set(model.features):
            reasons.append("features_mismatch")
        if not target.secret_ref:
            reasons.append("credential_scope_missing")
        if not target.available(now):
            reasons.append("eligibility_or_official_capacity")
            if not target.enabled:
                reasons.append("disabled")
            if not target.free_eligible:
                reasons.append("free_eligibility_unverified")
            if target.billing_enabled:
                reasons.append("billing_enabled")
            if target.verified_at is None or target.expires_at is None:
                reasons.append("free_evidence_missing")
            elif not target.verified_at <= now < target.expires_at:
                reasons.append("free_evidence_not_current")
            if any(
                f.remaining.provenance == "official"
                and f.remaining.value == 0
                and f.remaining.as_of is not None
                and f.remaining.as_of <= now
                and f.remaining.valid_until is not None
                and now < f.remaining.valid_until
                for f in target.provider_quota_facts
            ):
                reasons.append("official_capacity_zero")
        if self._credentials(target)["state"] == "missing":
            reasons.append("credential_name_missing")
        health = self.routing.health(con, target.id, now)
        if health["state"] in {"open", "repair_required"}:
            reasons.append(
                "circuit_open" if health["state"] == "open" else "health_repair_required"
            )
        if health["probe_busy"]:
            reasons.append("half_open_probe_busy")
        scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
        for observation in self._quota_observations(con, target, now):
            cost = (
                1
                if observation["metric"] in {"requests", "conversions"}
                else data["neuron_bound"]
                if observation["metric"] == "neurons"
                else bound + data["max_output_tokens"]
                if observation["metric"] == "tokens"
                else bound
            )
            if (
                observation["current"]
                and observation["effective_remaining"] is not None
                and cost is not None
                and observation["effective_remaining"] < cost
            ):
                reasons.append("observed_capacity:" + observation["metric"])
        if con.execute(
            "SELECT 1 FROM reservations WHERE target_snapshot IS NULL "
            "AND state IN ('reserved','dispatched','unknown') AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id) LIMIT 1"
        ).fetchone():
            reasons.append("legacy_active_requires_reconciliation")
        if bound + data["max_output_tokens"] > model.context_tokens:
            reasons.append("context_limit")
        if data["max_output_tokens"] > target.max_output_tokens:
            reasons.append("output_limit")
        if any(q.metric == "neurons" for q in target.quotas) and data["neuron_bound"] is None:
            reasons.append("estimation_unconfigured")
        snapshot = self.broker._snapshot(target)
        changed = con.execute(
            "SELECT 1 FROM reservations WHERE target_id=? AND target_snapshot!=? "
            "AND state IN ('reserved','dispatched','unknown') AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id) LIMIT 1",
            (target.id, snapshot),
        ).fetchone()
        if changed:
            reasons.append("active_configuration_changed")
        cooldown = con.execute(
            "SELECT until_at FROM cooldowns WHERE target_id=?", (target.id,)
        ).fetchone()
        if cooldown and cooldown[0] > stamp(now):
            reasons.append("cooldown")
        scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
        scoped = con.execute(
            "SELECT until_at FROM quota_scope_cooldowns WHERE scope=?", (scope,)
        ).fetchone()
        if scoped and scoped[0] > stamp(now):
            reasons.append("account_quota_cooldown")
        active = con.execute(
            "SELECT count(*) FROM reservations WHERE target_id=? "
            "AND state IN ('reserved','dispatched','unknown') AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id)",
            (target.id,),
        ).fetchone()[0]
        if active >= target.concurrency_limit:
            reasons.append("concurrency_limit")
        if target.shared_concurrency_scope:
            shared = con.execute(
                "SELECT count(*) FROM reservations WHERE shared_scope=? "
                "AND state IN ('reserved','dispatched','unknown') AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id)",
                (target.shared_concurrency_scope,),
            ).fetchone()[0]
            if shared >= target.shared_concurrency_limit:
                reasons.append("shared_concurrency_limit")
        for quota in target.quotas:
            if quota.metric == "neurons" and data["neuron_bound"] is None:
                continue
            cost = self.broker._cost(quota, bound, data["neuron_bound"])
            used, _ = self.broker._used(con, quota, now, cost)
            if used + cost > quota.limit:
                reasons.append("local_cap:" + quota.bucket)
        return reasons

    def _route_plan(
        self, con: sqlite3.Connection, data: dict[str, Any], bound: int
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        now = self.clock()
        permanent_reasons = {
            "provider_constraint",
            "model_constraint",
            "capability_mismatch",
            "features_mismatch",
            "adapter_admission",
            "credential_scope_missing",
            "credential_name_missing",
            "disabled",
            "billing_enabled",
            "free_eligibility_unverified",
            "free_evidence_missing",
            "free_evidence_not_current",
            "context_limit",
            "output_limit",
            "estimation_unconfigured",
            "active_configuration_changed",
            "legacy_active_requires_reconciliation",
            "health_repair_required",
        }
        for target in self.targets:
            target_data = {
                **data,
                "neuron_bound": target.neurons(
                    bound, data["max_output_tokens"], now, data.get("neuron_bound")
                ),
            }
            reasons = self._reasons(con, target, data, bound)
            health = self.routing.health(con, target.id, now)
            scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
            observations = self._quota_observations(con, target, now)
            waits = []
            for until in (health.get("until_at"), health.get("probe_until")):
                if until and until > stamp(now):
                    waits.append(until)
            for cooldown in con.execute(
                "SELECT until_at FROM cooldowns WHERE target_id=? UNION ALL SELECT until_at FROM quota_scope_cooldowns WHERE scope=?",
                (target.id, scope),
            ):
                if cooldown[0] > stamp(now):
                    waits.append(cooldown[0])
            permanent = any(reason in permanent_reasons for reason in reasons)
            pressure = []
            for quota in target.quotas:
                cost = (
                    self.broker._cost(quota, bound, target_data.get("neuron_bound"))
                    if quota.metric != "neurons" or target_data.get("neuron_bound")
                    else 0
                )
                used, wait = self.broker._used(con, quota, now, cost)
                pressure.append(used / quota.limit)
                if used + cost > quota.limit:
                    if cost > quota.limit:
                        permanent = True
                    elif wait:
                        waits.append(wait)
            for item in observations:
                if (
                    item["current"]
                    and item["remaining"] == 0
                    and (item.get("reset_at") or item.get("valid_until"))
                ):
                    waits.append(item.get("reset_at") or item["valid_until"])
                elif item["current"] and any(
                    r == "observed_capacity:" + item["metric"] for r in reasons
                ):
                    if item.get("reset_at") or item.get("valid_until"):
                        waits.append(item.get("reset_at") or item["valid_until"])
                    else:
                        permanent = True
            active = con.execute(
                "SELECT state,expires_at FROM reservations WHERE target_id=? AND state IN ('reserved','dispatched','unknown') AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id)",
                (target.id,),
            ).fetchall()
            inflight = len(active)
            if "concurrency_limit" in reasons:
                if sum(row[0] == "unknown" for row in active) >= target.concurrency_limit:
                    permanent = True
                    reasons.append("unknown_requires_reconciliation")
                else:
                    waits.append(stamp(now + timedelta(seconds=5)))
            if "shared_concurrency_limit" in reasons:
                unknowns = con.execute(
                    "SELECT count(*) FROM reservations WHERE shared_scope=? AND state='unknown' AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=reservations.id)",
                    (target.shared_concurrency_scope,),
                ).fetchone()[0]
                if unknowns >= (target.shared_concurrency_limit or 1):
                    permanent = True
                    reasons.append("unknown_requires_reconciliation")
                else:
                    waits.append(stamp(now + timedelta(seconds=5)))
            known = [
                item
                for item in observations
                if item["current"]
                and item.get("effective_remaining", item["remaining"]) is not None
            ]
            headroom = min(
                (
                    item.get("effective_remaining", item["remaining"])
                    / max(1, item["limit"] or item["remaining"])
                    for item in known
                ),
                default=None,
            )
            factors = {
                "capacity_kind": self._capacity_kind(target),
                "budget_refresh_seconds": target.capacity.refresh_seconds,
                "health_failures": health["failures"],
                "observed_headroom": headroom,
                "local_pressure": max(pressure, default=0),
                "inflight": inflight,
                "latency_ms": health["latency_ms"],
                "priority": target.priority,
            }
            key = (
                *self._sort_key(target)[:2],
                health["failures"],
                1 if headroom is None else 0,
                -(headroom or 0),
                factors["local_pressure"],
                inflight,
                health["latency_ms"] or 0,
                target.priority,
                target.id,
            )
            rows.append(
                {
                    "target_id": target.id,
                    "provider": target.provider,
                    "model": target.model,
                    "eligible": not reasons,
                    "reasons": reasons,
                    "capacity": target.capacity.view(),
                    "effective_capacity_kind": self._capacity_kind(target),
                    "priority": target.priority,
                    "neuron_estimate": target.neuron_estimate.view()
                    if target.neuron_estimate
                    else None,
                    "effective_neuron_bound": target_data["neuron_bound"],
                    "features": list(self.registry.resolve(target.model, target.provider).features),
                    "health": health,
                    "quota_observations": observations,
                    "ranking_factors": factors,
                    "temporary": bool(reasons) and not permanent,
                    "next_retry_at": max(waits) if waits and not permanent else None,
                    "_key": key,
                }
            )
        rows.sort(key=lambda row: row.pop("_key"))
        selected = next((row["target_id"] for row in rows if row["eligible"]), None)
        retries = [
            row["next_retry_at"] for row in rows if row["temporary"] and row["next_retry_at"]
        ]
        temporary = any(row["temporary"] for row in rows)
        return {
            "selected_target_id": selected,
            "estimated_input_tokens": None if data["capability"] == "ocr" else bound,
            "candidates": rows,
            "temporary": selected is None and temporary,
            "permanent_rejection": selected is None and not temporary,
            "next_retry_at": min(retries) if retries else None,
        }

    def explain(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = self.validate_task(raw)
        bound = 1 if data["capability"] == "ocr" else 4 * len(data["input"].encode("utf-8")) + 256
        with self.broker._tx() as con:
            self.broker._expire_unsent(con, self.clock())
            return self._route_plan(con, data, bound)

    def _update(self, key: str, values: dict[str, Any]) -> None:
        allowed = set(COLUMNS) - {"request_key", "created_at"}
        if not values or set(values) - allowed:
            raise ValueError("invalid gateway update")
        with sqlite3.connect(self.db, timeout=15) as con:
            con.execute(
                "UPDATE gateway_tasks SET "
                + ",".join(f"{name}=?" for name in values)
                + " WHERE request_key=?",
                (*values.values(), key),
            )

    def _attempt_update(self, key: str, number: int, values: dict[str, Any]) -> None:
        allowed = set(ATTEMPT_COLUMNS) - {"attempt_no", "reservation_id", "created_at"}
        if not values or set(values) - allowed:
            raise ValueError("invalid attempt update")
        with sqlite3.connect(self.db, timeout=15) as con:
            con.execute(
                "UPDATE gateway_attempts SET "
                + ",".join(f"{name}=?" for name in values)
                + " WHERE request_key=? AND attempt_no=?",
                (*values.values(), key, number),
            )

    def run(
        self,
        raw: dict[str, Any],
        *,
        dispatch_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any]:
        data = self.validate_task(raw)
        key = data["request_key"]
        fingerprint = self._hmac(data)
        created_at = stamp(self.clock())
        with sqlite3.connect(self.db, timeout=15, isolation_level=None) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT payload_hmac FROM gateway_tasks WHERE request_key=?", (key,)
            ).fetchone()
            if row:
                con.execute("COMMIT")
                if not hmac.compare_digest(row[0], fingerprint):
                    raise GatewayError("conflict", "request key used for different payload")
                return self._view(key)
            if data.get("deadline") is not None and data["deadline"] <= created_at:
                con.execute("ROLLBACK")
                raise GatewayError("deadline_expired", "task deadline elapsed")
            con.execute(
                "INSERT INTO gateway_tasks(request_key,payload_hmac,state,created_at) VALUES(?,?,?,?)",
                (key, fingerprint, "preparing", created_at),
            )
            con.execute("COMMIT")
        execution_deadline = self.clock() + timedelta(seconds=180)
        if data.get("deadline"):
            execution_deadline = min(execution_deadline, datetime.fromisoformat(data["deadline"]))
        input_bound = (
            1 if data["capability"] == "ocr" else 4 * len(data["input"].encode("utf-8")) + 256
        )
        excluded: list[str] = []
        for candidate in self.targets:
            try:
                self.registry.admit(
                    self.registry.resolve(candidate.model, candidate.provider), data
                )
            except ValueError:
                excluded.append(candidate.id)
        excluded += [
            target.id for target in self.targets if self._credentials(target)["state"] == "missing"
        ]
        for attempt in range(min(len(self.targets), data.get("max_attempts", 32))):
            if self.clock() >= execution_deadline:
                self._update(
                    key,
                    {
                        "state": "rejected",
                        "error_code": "deadline_expired",
                        "completed_at": stamp(self.clock()),
                    },
                )
                return self._view(key)
            request = {
                "request_key": f"gw:{key}:{attempt}",
                "capability": data["capability"],
                "provider": data["provider"],
                "model": data["model"],
                "input_token_bound": input_bound,
                "max_output_tokens": data["max_output_tokens"],
                "neuron_bound": data["neuron_bound"],
                "exclude_target_ids": excluded,
                "requirements": data.get("requirements", {}),
            }
            try:
                plan = self.broker.reserve(request)
            except BrokerError as exc:
                had_quota_refusal = exc.code == "unavailable" and any(
                    item["state"] == "quota_rejected" for item in self._view(key)["attempts"]
                )
                self._update(
                    key,
                    {
                        "state": "quota_exhausted" if had_quota_refusal else "rejected",
                        "error_code": exc.code,
                        "completed_at": stamp(self.clock()),
                    },
                )
                if had_quota_refusal:
                    return self._view(key)
                raise GatewayError(exc.code, str(exc), exc.wait_until) from exc
            target = next(t for t in self.targets if t.id == plan["target_id"])
            effective_neurons = target.neurons(
                input_bound, data["max_output_tokens"], self.clock(), data.get("neuron_bound")
            )
            kind = self._capacity_kind(target)
            refresh = target.capacity.refresh_seconds if kind == "short_renewable" else None
            reason = f"{kind}:{refresh or 'unknown'};priority={target.priority}"
            self._update(
                key,
                {
                    "reservation_id": plan["reservation_id"],
                    "target_id": target.id,
                    "provider": target.provider,
                    "model": target.model,
                    "route_reason": reason,
                    "estimated_input_tokens": None if data["capability"] == "ocr" else input_bound,
                },
            )
            with sqlite3.connect(self.db, timeout=15) as con:
                con.execute(
                    "INSERT INTO gateway_attempts(request_key,attempt_no,reservation_id,"
                    "target_id,provider,model,state,created_at,estimated_input_tokens,input_bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        attempt,
                        plan["reservation_id"],
                        target.id,
                        target.provider,
                        target.model,
                        "preparing",
                        stamp(self.clock()),
                        None if data["capability"] == "ocr" else input_bound,
                        len(base64.b64decode(data["input"]))
                        if data["capability"] == "ocr"
                        else None,
                    ),
                )
            pre_send_phase = "credential"
            try:
                assert target.secret_ref is not None
                secret = self.secret_resolver(target.secret_ref)
                if not isinstance(secret, str) or not secret:
                    raise ValueError("empty secret")
                account_id = (
                    self.secret_resolver(target.account_id_ref)
                    if target.account_id_ref
                    else target.account_id
                )
                pre_send_phase = "request"
                spec = self.registry.resolve(target.model, target.provider)
                self.registry.admit(spec, data)
                url, headers, payload = self.registry.request(
                    spec,
                    account_id,
                    secret,
                    data["input"],
                    data["max_output_tokens"],
                    data["source_language"],
                    data["target_language"],
                )
                if "json_output" in data.get("requirements", {}).get("features", []):
                    payload["response_format"] = {"type": "json_object"}
            except Exception:  # noqa: BLE001 - resolver plugins must fail closed
                # A resolver must not strand a pre-send row or expose its message.
                try:
                    self.broker.cancel(plan["reservation_id"])
                except BrokerError:
                    pass
                self._attempt_update(
                    key,
                    attempt,
                    {
                        "state": "pre_send_failed",
                        "error_code": pre_send_phase + "_unavailable",
                        "completed_at": stamp(self.clock()),
                    },
                )
                excluded.append(target.id)
                continue
            try:

                def guarded_send(
                    con: sqlite3.Connection,
                    target: Target = target,
                    reservation_id: str = plan["reservation_id"],
                    effective_neurons: int | None = effective_neurons,
                ) -> None:
                    if dispatch_guard is not None:
                        dispatch_guard(con)
                    if self.clock() >= execution_deadline:
                        raise ValueError("task deadline elapsed")
                    for observation in self._quota_observations(
                        con, target, self.clock(), reservation_id
                    ):
                        cost = (
                            1
                            if observation["metric"] in {"requests", "conversions"}
                            else effective_neurons
                            if observation["metric"] == "neurons"
                            else input_bound + data["max_output_tokens"]
                            if observation["metric"] == "tokens"
                            else input_bound
                        )
                        if (
                            observation["current"]
                            and cost is not None
                            and observation["effective_remaining"] is not None
                            and observation["effective_remaining"] < cost
                        ):
                            raise ValueError("observed quota changed")

                self.broker.dispatch(plan["reservation_id"], guard=guarded_send)
            except BrokerError:
                try:
                    self.broker.cancel(plan["reservation_id"])
                except BrokerError:
                    pass
                self._attempt_update(
                    key,
                    attempt,
                    {
                        "state": "pre_send_failed",
                        "error_code": "dispatch_unavailable",
                        "completed_at": stamp(self.clock()),
                    },
                )
                excluded.append(target.id)
                continue
            self._update(key, {"state": "dispatched", "dispatched_at": stamp(self.clock())})
            self._attempt_update(
                key,
                attempt,
                {
                    "state": "dispatched",
                    "dispatched_at": stamp(self.clock()),
                },
            )
            started = time.monotonic()
            request_started_at = self.clock()
            answer: str | None = None
            input_tokens: int | None = None
            output_tokens: int | None = None
            neurons: int | None = None
            request_id: str | None = None
            status: int | None = None
            response_headers: dict[str, str] = {}
            response = b""
            retry: int | None = None
            error_code: str | None = None
            finish_reason: str | None = None
            response_truncated: bool | None = None
            diagnostics: dict[str, str | int | bool | None] = {}
            bounded_chat = (
                target.provider in {"groq", "mistral"}
                or target.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
                or spec.adapter == "openai_chat"
            )
            try:
                timeout = (
                    120.0
                    if target.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
                    else 60.0
                    if target.provider == "nvidia"
                    else 30.0
                )
                timeout = min(
                    timeout, max(0.001, (execution_deadline - self.clock()).total_seconds())
                )
                status, response_headers, response = (
                    self.registry.transport(spec, url, headers, payload, timeout)
                    if self.transport is provider_http
                    else self.transport(url, headers, payload, timeout)
                )
                answer, input_tokens, output_tokens, neurons, request_id = self.registry.interpret(
                    spec,
                    status,
                    response,
                )
                diagnostics = safe_response_diagnostics(
                    target.provider,
                    status,
                    response_headers,
                    response,
                    sensitive_values=(secret, account_id, data["input"]),
                )
                if request_id is not None and any(
                    value and value.casefold() in request_id.casefold()
                    for value in (secret, account_id, data["input"])
                ):
                    request_id = None
                if (
                    target.provider in {"nvidia", "groq", "mistral", "openrouter"}
                    or spec.adapter == "openai_chat"
                ):
                    finish_reason, response_truncated = chat_completion_metadata(status, response)
                if answer is not None and not answer.strip():
                    answer = None
                if bounded_chat:
                    if isinstance(response_headers, ProviderHeaders):
                        diagnostics.update(safe_transport_diagnostics(response_headers.diagnostics))
                    if output_tokens is not None and output_tokens > data["max_output_tokens"]:
                        input_tokens = output_tokens = None
                    if request_id is not None and (
                        not re.fullmatch(
                            r"(?:req_[A-Za-z0-9_-]{1,100}|(?:chatcmpl|cmpl)-[A-Za-z0-9_-]{1,100}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
                            request_id,
                        )
                        or secret.casefold() in request_id.casefold()
                    ):
                        request_id = None
                if status == 429:
                    retry = parse_retry_after(
                        response_headers.get("Retry-After") or response_headers.get("retry-after"),
                        self.clock(),
                    )
                if status == 202:
                    error_code = "pending_provider_result"
                elif status != 200:
                    error_code = safe_http_error_code(target.provider, status, response)
                elif answer is None:
                    error_code = "provider_response_invalid"
            except (OSError, TimeoutError, ValueError, ProviderError) as exc:
                if isinstance(exc, ProviderPhaseTimeout):
                    diagnostics.update(safe_transport_diagnostics(exc.diagnostics))
                error_code = (
                    exc.code
                    if isinstance(exc, ProviderPhaseTimeout)
                    else type(exc).__name__
                    if type(exc).__name__ in {"TimeoutError", "ProviderError"}
                    else "transport_error"
                )
            try:
                quota_rejected = status is not None and self.registry.quota_rejection(
                    spec,
                    status,
                    response,
                    response_headers,
                    sensitive=(secret, account_id, data["input"]),
                )
            except (ValueError, TypeError):
                quota_rejected = False
                diagnostics["adapter_metadata_invalid"] = True
            if quota_rejected and (
                answer is not None
                or any(value is not None for value in (input_tokens, output_tokens, neurons))
            ):
                quota_rejected = False
            diagnostics["non_execution_quota_proven"] = quota_rejected
            elapsed = round((time.monotonic() - started) * 1000)
            required_metrics = {quota.metric for quota in target.quotas}
            known_quota_usage = (
                "input_tokens" not in required_metrics or input_tokens is not None
            ) and ("neurons" not in required_metrics or neurons is not None)
            if bounded_chat:
                known_quota_usage = (
                    known_quota_usage and input_tokens is not None and output_tokens is not None
                )
            completed = status == 200 and answer is not None
            self.routing.record(
                target.id,
                self.clock(),
                status,
                elapsed,
                completed,
                retry,
                error_code,
                as_of=request_started_at,
            )
            scope = self.broker._quota_scope(json.loads(self.broker._snapshot(target)))
            try:
                observations = self.registry.quota_observations(
                    spec,
                    status or 0,
                    response,
                    response_headers,
                    self.clock(),
                    as_of=request_started_at,
                )
                for observation in observations:
                    encoded = canonical(observation).casefold()
                    if any(
                        value and value.casefold() in encoded
                        for value in (secret, account_id, data["input"])
                    ):
                        continue
                    if not spec.adapter.startswith("builtin:"):
                        observation = {**observation, "source": "adapter:" + spec.adapter}
                    self.routing.observe(scope, observation, self.clock())
            except (ValueError, TypeError, KeyError):
                diagnostics["adapter_metadata_invalid"] = True
            state = (
                "completed"
                if completed
                and known_quota_usage
                and (
                    data["capability"] == "ocr"
                    or (input_tokens is not None and output_tokens is not None)
                )
                else "completed_usage_unknown"
                if completed
                else "unknown"
            )
            report: dict[str, Any] = {
                "reservation_id": plan["reservation_id"],
                "report_key": str(uuid.uuid4()),
                "state": "quota_rejected"
                if quota_rejected
                else "completed"
                if completed and known_quota_usage
                else "unknown",
                "error_status": status if status is not None and status >= 400 else None,
                "provider_request_id": request_id,
            }
            if known_quota_usage and completed:
                usage = {"requests": 1}
                if any(q.metric == "input_tokens" for q in target.quotas):
                    assert input_tokens is not None
                    usage["input_tokens"] = input_tokens
                if any(q.metric == "neurons" for q in target.quotas):
                    assert neurons is not None
                    usage["neurons"] = neurons
                report["usage"] = usage
            if retry is not None:
                report["retry_after_seconds"] = retry
            if completed:
                self.broker.mark_execution_finished(plan["reservation_id"])
            reported = False
            try:
                self.broker.report(report)
                reported = True
            except BrokerError:
                # Provider may already have executed. Preserve the durable reservation.
                state = "unknown" if not completed else "completed_usage_unknown"
                error_code = "ledger_report_failed"
            if quota_rejected and reported:
                state = "quota_rejected"
                error_code = "provider_quota_rejected"
            result_values = {
                "state": state,
                "completed_at": stamp(self.clock()),
                "latency_ms": elapsed,
                "http_status": status,
                "error_code": error_code,
                "provider_request_id": request_id,
                "reported_input_tokens": input_tokens,
                "reported_output_tokens": output_tokens,
                "reported_neurons": neurons,
                "usage_source": "documented_quota_rejection"
                if quota_rejected and reported
                else "provider_reported"
                if any(value is not None for value in (input_tokens, output_tokens, neurons))
                else "unknown",
                "finish_reason": finish_reason,
                "response_truncated": response_truncated,
                "diagnostics_json": json.dumps(diagnostics, sort_keys=True)
                if diagnostics
                else None,
            }
            self._attempt_update(key, attempt, result_values)
            self._update(
                key,
                result_values,
            )
            if quota_rejected and reported:
                excluded.append(target.id)
                continue
            result = self._view(key)
            if completed:
                result["answer"] = answer
            return result
        self._update(
            key,
            {
                "state": "quota_exhausted"
                if any(
                    attempt["state"] == "quota_rejected" for attempt in self._view(key)["attempts"]
                )
                else "rejected",
                "error_code": "provider_quota_rejected"
                if any(
                    attempt["state"] == "quota_rejected" for attempt in self._view(key)["attempts"]
                )
                else "credential_or_dispatch_unavailable",
                "completed_at": stamp(self.clock()),
            },
        )
        if self._view(key)["state"] == "quota_exhausted":
            return self._view(key)
        raise GatewayError("unavailable", "no verified free route with available credentials")

    def usage(
        self,
        provider: str | None = None,
        model: str | None = None,
        from_at: str | None = None,
        to_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if provider is not None and provider not in self.registry.providers:
            raise GatewayError("invalid_request", "invalid provider filter")
        if model is not None and not any(key[1] == model for key in self.registry.models):
            raise GatewayError("invalid_request", "invalid model filter")

        def normalized(value: str | None) -> str | None:
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError as exc:
                raise GatewayError("invalid_request", "time filters require offset") from exc
            return stamp(parsed)

        from_at, to_at = normalized(from_at), normalized(to_at)
        if from_at is not None and to_at is not None and from_at >= to_at:
            raise GatewayError("invalid_request", "invalid time range")
        dispatch_time = "COALESCE(r.dispatched_at,g.dispatched_at)"
        conditions = [f"{dispatch_time} IS NOT NULL"]
        params: list[str] = []
        for column, value, op in (
            ("g.provider", provider, "="),
            ("g.model", model, "="),
            (dispatch_time, from_at, ">="),
            (dispatch_time, to_at, "<"),
        ):
            if value is not None:
                conditions.append(f"{column}{op}?")
                params.append(value)
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT g.provider,g.model,count(*) AS requests,"
                "sum(g.estimated_input_tokens) AS estimated_input_tokens,"
                "sum(g.reported_input_tokens) AS reported_input_tokens,"
                "sum(g.reported_output_tokens) AS reported_output_tokens,"
                "sum(g.reported_neurons) AS reported_neurons,"
                "sum(c.ledger_input_tokens) AS ledger_input_tokens,"
                "sum(c.ledger_neurons) AS ledger_neurons,"
                "sum(g.input_bytes) AS input_bytes,"
                "CASE WHEN g.provider='ocrspace' THEN NULL ELSE "
                "sum(g.reported_input_tokens IS NULL AND g.state!='quota_rejected') "
                "END AS input_unknown_count,"
                "CASE WHEN g.provider='ocrspace' THEN NULL ELSE "
                "sum(g.reported_output_tokens IS NULL AND g.state!='quota_rejected') "
                "END AS output_unknown_count,"
                "CASE WHEN g.provider='cloudflare' THEN "
                "sum(g.reported_neurons IS NULL AND g.state!='quota_rejected') "
                "ELSE NULL END AS neurons_unknown_count,"
                "sum(g.state='quota_rejected') AS quota_rejected_count,"
                "sum(g.state IN ('unknown','dispatched','preparing')) AS outcome_unknown_count,"
                "sum(g.response_truncated=1) AS truncated_count,"
                "sum(r.state IN ('dispatched','unknown')) AS ledger_held_count "
                "FROM ("
                "SELECT request_key,reservation_id,provider,model,dispatched_at,"
                "estimated_input_tokens,reported_input_tokens,reported_output_tokens,"
                "reported_neurons,state,input_bytes,response_truncated FROM gateway_attempts "
                "UNION ALL SELECT request_key,reservation_id,provider,model,dispatched_at,"
                "estimated_input_tokens,reported_input_tokens,reported_output_tokens,"
                "reported_neurons,state,NULL AS input_bytes,response_truncated FROM gateway_tasks legacy "
                "WHERE NOT EXISTS (SELECT 1 FROM gateway_attempts a "
                "WHERE a.request_key=legacy.request_key)) g "
                "LEFT JOIN reservations r ON r.id=g.reservation_id "
                "LEFT JOIN (SELECT reservation_id,"
                "max(CASE WHEN metric='input_tokens' THEN amount END) AS ledger_input_tokens,"
                "max(CASE WHEN metric='neurons' THEN amount END) AS ledger_neurons "
                "FROM charges GROUP BY reservation_id) c ON c.reservation_id=g.reservation_id "
                "WHERE "
                + " AND ".join(conditions)
                + " GROUP BY g.provider,g.model ORDER BY g.provider,g.model",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
