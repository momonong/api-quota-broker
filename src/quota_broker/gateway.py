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
from datetime import datetime
from pathlib import Path
from typing import Any

from .catalog import MODELS
from .config import Target
from .core import Broker, BrokerError, canonical, stamp, utcnow
from .gateway_providers import (
    ProviderError,
    ProviderHeaders,
    ProviderPhaseTimeout,
    chat_completion_metadata,
    explicit_quota_rejection,
    interpret,
    official_request,
    provider_http,
    safe_http_error_code,
    safe_response_diagnostics,
    safe_transport_diagnostics,
)
from .retry import parse_retry_after

ProviderTransport = Callable[
    [str, dict[str, str], dict[str, object], float], tuple[int, dict[str, str], bytes]
]
SecretResolver = Callable[[str], str]
LANGUAGES = {
    "en",
    "cs",
    "da",
    "de",
    "el",
    "es-es",
    "es-us",
    "fi",
    "fr",
    "hu",
    "it",
    "lt",
    "lv",
    "nl",
    "no",
    "pl",
    "pt-pt",
    "pt-br",
    "ro",
    "ru",
    "sk",
    "sv",
    "zh-cn",
    "zh-tw",
    "ja",
    "hi",
    "ko",
    "et",
    "sl",
    "bg",
    "uk",
    "hr",
    "ar",
    "vi",
    "tr",
    "id",
    "th",
}
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


def validate_task(raw: dict[str, Any]) -> dict[str, Any]:
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
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise GatewayError("invalid_request", "invalid task fields")
    key = raw.get("request_key")
    content = raw.get("input")
    capability = raw.get("capability")
    output = raw.get("max_output_tokens")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", key):
        raise GatewayError("invalid_request", "request_key must be an opaque URI-safe id")
    if capability not in {"text_generation", "translation", "ocr"}:
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
    if provider is not None and provider not in {model.provider for model in MODELS.values()}:
        raise GatewayError("invalid_request", "unsupported provider")
    if model is not None and (not isinstance(model, str) or model not in MODELS):
        raise GatewayError("invalid_request", "unknown model")
    if model is not None and provider is not None and MODELS[model].provider != provider:
        raise GatewayError("invalid_request", "provider/model mismatch")
    if model is not None and MODELS[model].capability != capability:
        raise GatewayError("invalid_request", "model/capability mismatch")
    source, target = raw.get("source_language"), raw.get("target_language")
    if capability == "translation":
        if (
            source not in LANGUAGES
            or target not in LANGUAGES
            or source == target
            or "en" not in {source, target}
        ):
            raise GatewayError(
                "invalid_request", "Riva translation needs a supported English language pair"
            )
        if len(content) > 1952:
            raise GatewayError("invalid_request", "Riva text exceeds hosted input policy")
    elif source is not None or target is not None:
        raise GatewayError("invalid_request", "language fields require translation")
    neuron_bound = raw.get("neuron_bound")
    if neuron_bound is not None and (type(neuron_bound) is not int or neuron_bound < 1):
        raise GatewayError("invalid_request", "neuron_bound must be positive")
    if neuron_bound is not None and capability != "text_generation":
        raise GatewayError("invalid_request", "neuron_bound only applies to text generation")
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
        self.broker = Broker(db, targets, clock)
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
            ledger_attempt = self.broker.status(attempt["reservation_id"])
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
            ledger = self.broker.status(result["reservation_id"])
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
        return self.broker.catalog()

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
        model = MODELS[target.model]
        reasons = []
        if data["provider"] not in (None, target.provider):
            reasons.append("provider_constraint")
        if data["model"] not in (None, target.model):
            reasons.append("model_constraint")
        if model.capability != data["capability"]:
            reasons.append("capability_mismatch")
        if not target.secret_ref:
            reasons.append("credential_scope_missing")
        if not target.available(now):
            reasons.append("eligibility_or_official_capacity")
        if bound + data["max_output_tokens"] > model.context_tokens:
            reasons.append("context_limit")
        if data["max_output_tokens"] > target.max_output_tokens:
            reasons.append("output_limit")
        if target.provider == "cloudflare" and data["neuron_bound"] is None:
            reasons.append("neuron_bound_required")
        snapshot = self.broker._snapshot(target)
        changed = con.execute(
            "SELECT 1 FROM reservations WHERE target_id=? AND target_snapshot!=? "
            "AND state IN ('reserved','dispatched','unknown') LIMIT 1",
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
            "AND state IN ('reserved','dispatched','unknown')",
            (target.id,),
        ).fetchone()[0]
        if active >= target.concurrency_limit:
            reasons.append("concurrency_limit")
        if target.shared_concurrency_scope:
            shared = con.execute(
                "SELECT count(*) FROM reservations WHERE shared_scope=? "
                "AND state IN ('reserved','dispatched','unknown')",
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

    def explain(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = validate_task(raw)
        bound = 1 if data["capability"] == "ocr" else 4 * len(data["input"].encode("utf-8")) + 256
        rows = []
        with self.broker._tx() as con:
            self.broker._expire_unsent(con, self.clock())
            for target in sorted(self.targets, key=self._sort_key):
                reasons = self._reasons(con, target, data, bound)
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
                    }
                )
        selected = next((row["target_id"] for row in rows if row["eligible"]), None)
        return {
            "selected_target_id": selected,
            "estimated_input_tokens": None if data["capability"] == "ocr" else bound,
            "candidates": rows,
        }

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

    def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = validate_task(raw)
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
            con.execute(
                "INSERT INTO gateway_tasks(request_key,payload_hmac,state,created_at) VALUES(?,?,?,?)",
                (key, fingerprint, "preparing", created_at),
            )
            con.execute("COMMIT")
        input_bound = (
            1 if data["capability"] == "ocr" else 4 * len(data["input"].encode("utf-8")) + 256
        )
        excluded: list[str] = []
        for attempt in range(min(len(self.targets), 3)):
            request = {
                "request_key": f"gw:{key}:{attempt}",
                "capability": data["capability"],
                "provider": data["provider"],
                "model": data["model"],
                "input_token_bound": input_bound,
                "max_output_tokens": data["max_output_tokens"],
                "neuron_bound": data["neuron_bound"],
                "exclude_target_ids": excluded,
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
                url, headers, payload = official_request(
                    target.provider,
                    target.model,
                    account_id,
                    secret,
                    data["input"],
                    data["max_output_tokens"],
                    data["source_language"],
                    data["target_language"],
                )
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
                self.broker.dispatch(plan["reservation_id"])
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
            )
            try:
                status, response_headers, response = self.transport(
                    url,
                    headers,
                    payload,
                    120.0
                    if target.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
                    else 60.0
                    if target.provider == "nvidia"
                    else 30.0,
                )
                answer, input_tokens, output_tokens, neurons, request_id = interpret(
                    target.provider, status, response, model_id=target.model
                )
                if bounded_chat:
                    diagnostics = safe_response_diagnostics(
                        target.provider,
                        status,
                        response_headers,
                        response,
                        sensitive_values=(secret, data["input"]),
                    )
                    if isinstance(response_headers, ProviderHeaders):
                        diagnostics.update(safe_transport_diagnostics(response_headers.diagnostics))
                    finish_reason, response_truncated = chat_completion_metadata(status, response)
                    if answer is not None and not answer.strip():
                        answer = None
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
            quota_rejected = status is not None and explicit_quota_rejection(
                target.provider, status, response_headers, response
            )
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
        if provider is not None and provider not in {model.provider for model in MODELS.values()}:
            raise GatewayError("invalid_request", "invalid provider filter")
        if model is not None and model not in MODELS:
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
