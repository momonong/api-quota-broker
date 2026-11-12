"""Allowlisted dashboard projection and conservative, advice-only admission."""

import copy
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from .core import stamp, utcnow
from .dashboard_accounts import CreditAccounts
from .dashboard_provider_metadata import (
    COUNTER_TTL,
    KEY_URL,
    MODEL_TTL,
    quota_projection,
    read_metadata,
    refresh_projection,
)
from .gateway import RECORDED_TASK_TYPES, Gateway, GatewayError, validate_task
from .gateway_server import _bounded_json, _json_depth

MAX_RESPONSE = 2 * 1024 * 1024
PROVIDER_INVENTORY = Path(__file__).with_name("dashboard_assets") / "provider-inventory.json"
POLICY = {"execution_allowed": False, "paid_allowed": False, "mode": "single_task_advice"}
USAGE_FIELDS = (
    "provider",
    "model",
    "requests",
    "reported_input_tokens",
    "reported_output_tokens",
    "reported_total_tokens",
    "reported_neurons",
    "reported_audio_seconds",
    "reported_images",
    "reported_pages",
    "reported_conversions",
    "input_unknown_count",
    "output_unknown_count",
    "neurons_unknown_count",
    "ledger_held_count",
    "outcome_unknown_count",
    "quota_rejected_count",
)
TASK_TYPES = RECORDED_TASK_TYPES | {"unknown"}


class DashboardError(ValueError):
    """Safe fixed error code, never an upstream exception."""


class Source(Protocol):
    kind: str

    def source_metadata(self) -> dict[str, Any]: ...
    def catalog(self) -> Any: ...
    def diagnostics(self) -> Any: ...
    def usage(self) -> Any: ...
    def recent(self) -> Any: ...
    def models(self) -> Any: ...
    def usage_history(self, filters: dict[str, Any]) -> Any: ...
    def explain(self, task: dict[str, Any]) -> Any: ...


class GatewaySource:
    kind = "demo"

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def source_metadata(self) -> dict[str, Any]:
        return {}

    def catalog(self) -> Any:
        return self.gateway.catalog()

    def diagnostics(self) -> Any:
        return self.gateway.diagnostics()

    def usage(self) -> Any:
        return self.gateway.usage()

    def recent(self) -> Any:
        return self.gateway.recent(limit=6)

    def models(self) -> Any:
        return self.gateway.model_inventory()

    def usage_history(self, filters: dict[str, Any]) -> Any:
        return {
            "rows": self.gateway.usage(
                provider=filters["provider"],
                model=filters["model"],
                from_at=filters["from"],
                to_at=filters["to"],
                task_type=filters["task"],
            ),
            "task_types_recorded": True,
        }

    def explain(self, task: dict[str, Any]) -> Any:
        return self.gateway.explain(task)


class UpstreamSource:
    """Only a literal IPv4 loopback Gateway; no proxy, redirects or generic proxy API."""

    kind = "gateway"
    ROUTES = frozenset(
        {
            ("GET", "/v1/catalog"),
            ("GET", "/v1/model-inventory"),
            ("GET", "/v1/diagnostics"),
            ("GET", "/v1/usage"),
            ("GET", "/v1/tasks?limit=6"),
            ("POST", "/v1/routes/explain"),
        }
    )

    def __init__(self, base_url: str, token: str) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not parsed.port
        ):
            raise ValueError("literal loopback Gateway URL required")
        if not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("private upstream token required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._port = parsed.port

    def _get(self, method: str, path: str, task: dict[str, Any] | None = None) -> Any:
        allowed = (method, path) in self.ROUTES
        if not allowed and method == "GET" and ("GET", "/v1/usage") in self.ROUTES:
            try:
                parsed = urlsplit(path)
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                if (
                    parsed.path != "/v1/usage"
                    or parsed.netloc
                    or parsed.scheme
                    or parsed.fragment
                    or not query
                    or set(query) - {"provider", "model", "from", "to", "task"}
                    or any(len(values) != 1 for values in query.values())
                ):
                    raise ValueError
                _usage_filters(
                    {
                        key: values[0]
                        for key, values in query.items()
                        if not (key == "task" and values == ["all"])
                    }
                )
                allowed = True
            except (ValueError, DashboardError):
                pass
        if not allowed:
            raise DashboardError("route_not_allowed")
        try:
            data = _bounded_json(task, 65536) if task is not None else None
            deadline = time.monotonic() + 5
            connection = HTTPConnection("127.0.0.1", self._port, timeout=5)
            try:
                connection.request(
                    method,
                    path,
                    body=data,
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                if response.status == 400 and method == "POST":
                    raise DashboardError("invalid_task")
                if response.status != 200:
                    raise ValueError("invalid response")
                chunks = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, MAX_RESPONSE + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE:
                        raise ValueError("invalid response")
                raw = bytes(chunks)
            finally:
                connection.close()
            if len(raw) > MAX_RESPONSE or self._token.encode() in raw:
                raise ValueError("invalid response")
            _json_depth(raw)
            result = json.loads(raw)
            if self._token in json.dumps(result, ensure_ascii=False):
                raise ValueError("invalid response")
            return result
        except DashboardError:
            raise
        except Exception:  # noqa: BLE001 - fixed error never reflects upstream/token
            raise DashboardError("source_unavailable") from None

    def source_metadata(self) -> dict[str, Any]:
        return {}

    def catalog(self) -> Any:
        return self._get("GET", "/v1/catalog")

    def diagnostics(self) -> Any:
        return self._get("GET", "/v1/diagnostics")

    def usage(self) -> Any:
        return self._get("GET", "/v1/usage")

    def recent(self) -> Any:
        return self._get("GET", "/v1/tasks?limit=6")

    def models(self) -> Any:
        return self._get("GET", "/v1/model-inventory")

    def usage_history(self, filters: dict[str, Any]) -> Any:
        fields = {key: filters[key] for key in ("provider", "model", "from", "to") if filters[key]}
        query = urlencode({**fields, "task": filters["task"] or "all"})
        try:
            return {"rows": self._get("GET", "/v1/usage?" + query), "task_types_recorded": True}
        except DashboardError:
            if filters["task"] is not None:
                raise
            # Old Gateways reject the new task query. Only the unfiltered history
            # may fall back; a filtered failure must never become a false zero.
            legacy = urlencode(fields)
            return {
                "rows": self._get("GET", "/v1/usage" + ("?" + legacy if legacy else "")),
                "task_types_recorded": False,
            }

    def explain(self, task: dict[str, Any]) -> Any:
        return self._get("POST", "/v1/routes/explain", task)


class ActivitySource(UpstreamSource):
    """Read recorded activity and runtime switches, without advice or execution."""

    ROUTES = frozenset({("GET", "/v1/catalog"), ("GET", "/v1/usage"), ("GET", "/v1/tasks?limit=6")})


def _rows(value: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or len(value) > 256
        or any(not isinstance(r, dict) for r in value)
    ):
        raise DashboardError("source_unavailable")
    return value


def _text(value: Any) -> str | None:
    return value[:160] if isinstance(value, str) else None


def _number(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**15 else None


def _fraction(value: Any) -> int | float | None:
    # Comparisons reject NaN/infinity; booleans are not numeric evidence.
    return value if type(value) in {int, float} and 0 <= value <= 1 else None


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result if result and result.tzinfo else None
    except ValueError:
        return None


def _current(start: Any, end: Any, now: datetime) -> bool:
    since, until = _time(start), _time(end)
    return bool(since and until and since <= now < until)


def _usage_filters(raw: Any) -> dict[str, str | None]:
    if not isinstance(raw, dict) or set(raw) - {"provider", "model", "from", "to", "task"}:
        raise DashboardError("invalid_filters")
    result: dict[str, str | None] = {
        key: None for key in ("provider", "model", "from", "to", "task")
    }
    for key, value in raw.items():
        if not isinstance(value, str) or len(value) > (160 if key == "model" else 80) or not value:
            raise DashboardError("invalid_filters")
        if key in {"from", "to"}:
            parsed = _time(value)
            if parsed is None:
                raise DashboardError("invalid_filters")
            result[key] = stamp(parsed)
        elif key == "provider":
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
                raise DashboardError("invalid_filters")
            result[key] = value
        elif key == "model":
            if not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", value):
                raise DashboardError("invalid_filters")
            result[key] = value
        elif value in TASK_TYPES:
            result[key] = value
        else:
            raise DashboardError("invalid_filters")
    if result["from"] and result["to"] and result["from"] >= result["to"]:
        raise DashboardError("invalid_filters")
    return result


def _model_projection(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": _text(item.get("provider")),
        "model": _text(item.get("model")),
        "context_tokens": _number(item.get("context_tokens")),
        "max_output_tokens": _number(item.get("max_output_tokens")),
        "capability_basis": "configured_registry",
    }
    for field in ("capabilities", "features", "input_modalities", "output_modalities"):
        values = item.get(field)
        result[field] = (
            [
                value
                for value in values
                if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
            ][:32]
            if isinstance(values, list)
            else []
        )
    result["variants"] = [
        {
            "task": v["task"],
            **{
                field: [
                    x
                    for x in v[field]
                    if isinstance(x, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", x)
                ][:32]
                for field in ("inputs", "outputs", "features")
            },
        }
        for v in _rows(item.get("variants", []))[:32]
        if v.get("task") in result["capabilities"]
        and all(isinstance(v.get(field), list) for field in ("inputs", "outputs", "features"))
    ]
    return result


def _strings(value: Any) -> list[str]:
    # Dynamic bucket names and scopes in Gateway reasons are private metadata.
    known = {
        "disabled",
        "billing_enabled",
        "free_eligibility_unverified",
        "free_evidence_missing",
        "free_evidence_not_current",
        "credential_inventory_unknown",
        "credential_name_missing",
        "credential_scope_missing",
        "provider_constraint",
        "model_constraint",
        "capability_mismatch",
        "features_mismatch",
        "adapter_admission",
        "context_limit",
        "output_limit",
        "local_cap",
        "observed_capacity",
        "concurrency_limit",
        "shared_concurrency_limit",
        "health_repair_required",
        "estimation_unconfigured",
        "token_preflight_required",
        "unknown_requires_reconciliation",
        "credit_expired",
    }
    if not isinstance(value, list):
        return ["gateway_blocked"]
    return list(
        dict.fromkeys(
            v.split(":", 1)[0]
            if isinstance(v, str) and v.split(":", 1)[0] in known
            else "gateway_blocked"
            for v in value[:32]
        )
    )


def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _number(row.get(key))
        if key not in {"provider", "model", "task_type", "state", "created_at", "completed_at"}
        else _text(row.get(key))
        for key in fields
    }


class Dashboard:
    def __init__(
        self,
        source: Source,
        *,
        clock: Callable[[], datetime] = utcnow,
        accounts: CreditAccounts | None = None,
        activity: UpstreamSource | None = None,
        provider_metadata_file: str | Path | None = None,
        reviewed_evidence: Source | None = None,
    ) -> None:
        self.source, self.clock = source, clock
        self.accounts, self.activity = accounts, activity
        if reviewed_evidence is not None and reviewed_evidence.kind != "reviewed":
            raise ValueError("reviewed display evidence source required")
        self.reviewed_evidence = reviewed_evidence
        self.provider_metadata_file = (
            Path(provider_metadata_file) if provider_metadata_file else None
        )

    def _display_targets(
        self, catalog: list[dict[str, Any]], diagnostics: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach display facts only; live admission fields never come from evidence."""
        targets = [self._target(item, diagnostics[item["target_id"]]) for item in catalog]
        if self.reviewed_evidence is None:
            return targets
        reviewed = _rows(self.reviewed_evidence.catalog())
        identity = lambda row: (row.get("provider"), row.get("model"))
        live_counts = Counter(identity(row) for row in catalog)
        reviewed_counts = Counter(identity(row) for row in reviewed)
        evidence = {identity(row): row for row in reviewed}
        metadata = self.reviewed_evidence.source_metadata()
        projector = Dashboard(self.reviewed_evidence, clock=self.clock)
        for target in targets:
            key = identity(target)
            if live_counts[key] != 1 or reviewed_counts[key] != 1:
                continue
            display = projector._target(evidence[key], {"quota_observations": []})
            if not isinstance(display["reviewed_account"], dict):
                raise DashboardError("source_unavailable")
            target["reviewed_account"] = {
                **copy.deepcopy(display["reviewed_account"]),
                **{
                    name: metadata.get(name)
                    for name in ("audit_session_at", "review_due_at", "time_precision")
                },
                "display_only": True,
            }
            target["provider_limits"] = copy.deepcopy(display["provider_limits"])
        return targets

    def _openrouter_counter(
        self,
        catalog: list[dict[str, Any]],
        diagnostics: dict[str, dict[str, Any]],
        report: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Project one authoritative account counter, never local reservation holds."""
        now = self.clock()
        fallback = quota_projection(report, now) if report else None
        readings = []
        for target in catalog:
            if target.get("provider") != "openrouter":
                continue
            for row in _rows(diagnostics.get(target["target_id"], {}).get("quota_observations")):
                if not all(
                    row.get(key) == expected
                    for key, expected in (
                        ("metric", "requests"),
                        ("window", "day"),
                        ("provenance", "official"),
                        ("confidence", "provider_reported"),
                        ("source", "adapter:openrouter_key_account"),
                    )
                ):
                    continue
                remaining, limit = _number(row.get("remaining")), _number(row.get("limit"))
                checked, until = _time(row.get("as_of")), _time(row.get("valid_until"))
                if (
                    remaining is None
                    or limit is None
                    or not 0 <= remaining <= limit <= 10**12
                    or limit == 0
                    or checked is None
                    or until is None
                    or not checked < until
                    or checked > now
                ):
                    continue
                reset = (checked.astimezone(UTC) + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if row.get("reset_at") is not None and _time(row["reset_at"]) != reset:
                    continue
                scope = target.get("shared_concurrency_scope") or ("target:" + target["target_id"])
                readings.append(
                    (
                        scope,
                        checked,
                        min(until, checked + COUNTER_TTL, reset),
                        reset,
                        {"used": limit - remaining, "limit": limit, "remaining": remaining},
                    )
                )
        if not readings:
            return fallback
        latest = max(row[1] for row in readings)
        current = [row for row in readings if row[1] == latest]
        ambiguous = (
            len({row[0] for row in readings}) != 1
            or len(
                {(row[3], row[4]["used"], row[4]["limit"], row[4]["remaining"]) for row in current}
            )
            != 1
        )
        if ambiguous:
            return {
                "scope": "ambiguous_accounts_not_aggregated",
                "source": KEY_URL,
                "checked_at": None,
                "valid_until": None,
                "reset_at": None,
                "state": "unknown",
                "daily": None,
                "last_observed_daily": None,
            }
        until = min(row[2] for row in current)
        daily = current[0][4]
        projected = {
            "scope": "queried_key_tier_policy",
            "source": KEY_URL,
            "checked_at": latest.isoformat(),
            "valid_until": until.isoformat(),
            "reset_at": current[0][3].isoformat(),
            "state": "current" if now < until else "stale",
            "daily": daily if now < until else None,
            "last_observed_daily": daily,
        }
        # Cost observations are separate measurements. Never label their original
        # USD amounts with the fresh runtime counter's timestamp or freshness.
        if fallback and fallback.get("usage") is not None:
            projected.update(
                usage=fallback["usage"],
                usage_checked_at=fallback["checked_at"],
                usage_valid_until=fallback["valid_until"],
                usage_current=fallback["state"] == "current",
            )
        return projected

    def _provider_evidence(
        self,
        targets: list[dict[str, Any]],
        catalog: list[dict[str, Any]],
        diagnostics: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        now = self.clock()
        report = (
            read_metadata(self.provider_metadata_file, now) if self.provider_metadata_file else None
        )
        counter = self._openrouter_counter(catalog, diagnostics, report)
        if report is None:
            if self.provider_metadata_file is None and counter is None:
                return {}
            return {
                "provider_metadata": {
                    "state": "available" if counter else "unavailable",
                    "openrouter": counter,
                }
            }
        observations = {(row["provider"], row["model"]): row for row in report["models"]}
        for target in targets:
            row = observations.get((target["provider"], target["model"]))
            if row is not None:
                checked = _time(row["checked_at"])
                if checked is not None:
                    target["catalog_observation"] = {
                        **row,
                        "current": now < checked + MODEL_TTL,
                    }
        return {
            "provider_metadata": {
                "state": "available",
                "openrouter": counter,
                "refresh": refresh_projection(report),
            }
        }

    def _metadata(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
        catalog, diagnostic = self.source.catalog(), self.source.diagnostics()
        rows = _rows(catalog)
        if not isinstance(diagnostic, dict):
            raise DashboardError("source_unavailable")
        as_of = _time(diagnostic.get("as_of"))
        if not as_of or not 0 <= (self.clock() - as_of).total_seconds() <= 60:
            raise DashboardError("source_unavailable")
        diag = _rows(diagnostic.get("targets"))
        if any(not isinstance(r.get("target_id"), str) for r in [*rows, *diag]):
            raise DashboardError("source_unavailable")
        mapping = {r["target_id"]: r for r in diag}
        if (
            len(mapping) != len(diag)
            or len({r["target_id"] for r in rows}) != len(rows)
            or set(mapping) != {r["target_id"] for r in rows}
        ):
            raise DashboardError("source_unavailable")
        for row in rows:
            other = mapping[row["target_id"]]
            if any(
                not isinstance(row.get(key), str)
                or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", row[key])
                or other.get(key) != row[key]
                for key in ("provider", "model")
            ):
                raise DashboardError("source_unavailable")
        return rows, mapping, stamp(as_of)

    def _source(self, as_of: str) -> dict[str, Any]:
        return {
            "kind": self.source.kind,
            "label": {
                "demo": "合成示範資料・非帳號讀值",
                "reviewed": "人工核對帳戶快照・非即時同步・憑證未核對",
            }.get(self.source.kind, "本機 Gateway 中繼資料"),
            "as_of": as_of,
            "fetched_at": stamp(self.clock()),
            "freshness": "manually_reviewed_snapshot"
            if self.source.kind == "reviewed"
            else "current_snapshot",
            **(self.source.source_metadata() if self.source.kind == "reviewed" else {}),
        }

    def _target(self, item: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any]:
        credentials = diag.get("credentials")
        if not isinstance(credentials, dict):
            credentials = {}
        quotas = _rows(item.get("local_safety_caps") or item.get("quotas") or [])
        ledger = _rows(item.get("local_ledger_usage", []))
        caps = []
        for quota in quotas:
            matches = [
                row
                for row in ledger
                if all(
                    row.get(key) == quota.get(key)
                    for key in ("metric", "window", "timezone", "limit")
                )
            ]
            reading = matches[0] if len(matches) == 1 else {}
            when = _time(reading.get("as_of"))
            current = bool(when and 0 <= (self.clock() - when).total_seconds() <= 60)
            caps.append(
                {
                    "metric": _text(quota.get("metric")),
                    "window": _text(quota.get("window")),
                    "timezone": _text(quota.get("timezone")),
                    "limit": _number(quota.get("limit")),
                    "used": _number(reading.get("used"))
                    if current
                    and reading.get("basis") == "local_ledger_current_window_including_holds"
                    else None,
                    "as_of": _text(reading.get("as_of")),
                    "basis": "local_ledger_current_window_including_holds",
                }
            )
        reviewed = item.get("reviewed_account") if self.source.kind == "reviewed" else None
        contexts = (
            {(q["metric"], q["window"]): q for q in reviewed["limit_context"]}
            if isinstance(reviewed, dict)
            else {}
        )
        billing_state = (
            item["billing_evidence_state"]
            if item.get("billing_evidence_state") in ("enabled", "disabled", "unknown")
            else reviewed["billing_state"]
            if isinstance(reviewed, dict)
            else (
                "disabled"
                if item.get("billing_enabled") is False
                else "enabled"
                if item.get("billing_enabled") is True
                else "unknown"
            )
        )
        return {
            **_model_projection(item),
            "model_scope": "configured_gateway_target",
            "target_id": _text(item.get("target_id")),
            "enabled": item.get("enabled") if type(item.get("enabled")) is bool else None,
            "billing_state": billing_state,
            "reviewed_account": reviewed,
            "provider_limits": [
                {
                    "metric": _text(fact.get("metric")),
                    "window": _text(fact.get("window")),
                    "value": _number(fact.get("limit", {}).get("value")),
                    **{
                        key: _text(fact.get("limit", {}).get(key))
                        for key in ("provenance", "as_of", "valid_until")
                    },
                    "source": _text(fact.get("limit", {}).get("source")) if reviewed else None,
                    "scope": _text(fact.get("limit", {}).get("scope")) if reviewed else None,
                    "current": _current(
                        fact.get("limit", {}).get("as_of"),
                        fact.get("limit", {}).get("valid_until"),
                        self.clock(),
                    ),
                    "basis": contexts.get((fact.get("metric"), fact.get("window")), {}).get(
                        "basis", "unspecified"
                    ),
                    "reset": contexts.get((fact.get("metric"), fact.get("window")), {}).get(
                        "reset", "unknown"
                    ),
                }
                for fact in _rows(item.get("provider_quota_facts", []))
            ],
            "provider": _text(item.get("provider")),
            "model": _text(item.get("model")),
            "state": diag.get("state")
            if diag.get("state") in {"ready", "blocked", "unknown"}
            else "unknown",
            "free_eligible": item.get("free_eligible") is True,
            "billing_enabled": None if billing_state == "unknown" else billing_state == "enabled",
            "free_verified_at": _text(item.get("account_verified_at")),
            "free_evidence_expires_at": _text(item.get("account_expires_at")),
            "credit_expires_at": _text((item.get("capacity") or {}).get("credit_expires_at")),
            "credentials": {
                "state": credentials.get("state")
                if credentials.get("state") in {"present", "missing", "unknown"}
                else "unknown",
                "verified_at": _text(credentials.get("verified_at")),
                "expires_at": _text(credentials.get("expires_at")),
            },
            "reasons": [
                "billing_state_unknown"
                if reason == "billing_enabled" and billing_state == "unknown"
                else reason
                for reason in _strings(diag.get("reasons"))
            ],
            "local_caps": caps,
            "required_capacity": [
                {"metric": _text(q.get("metric")), "window": _text(q.get("window"))}
                for q in _rows(item.get("required_capacity"))
                if _text(q.get("metric")) and _text(q.get("window"))
            ],
            "observations": [
                {
                    **{
                        k: _text(q.get(k))
                        for k in (
                            "metric",
                            "window",
                            "as_of",
                            "valid_until",
                            "reset_at",
                            "provenance",
                            "confidence",
                            "remaining_basis",
                            "formula",
                        )
                    },
                    **{
                        k: _number(q.get(k))
                        for k in (
                            "used",
                            "remaining",
                            "limit",
                            "effective_remaining",
                            "local_observation_hold",
                        )
                    },
                    "source": KEY_URL
                    if item.get("provider") == "openrouter"
                    and q.get("source") == "adapter:openrouter_key_account"
                    else _text(q.get("source"))
                    if reviewed
                    else None,
                    "effective_remaining": _number(q.get("effective_remaining"))
                    if _current(q.get("as_of"), q.get("valid_until"), self.clock())
                    else None,
                    "current": _current(q.get("as_of"), q.get("valid_until"), self.clock()),
                }
                for q in _rows(diag.get("quota_observations"))
            ],
        }

    def _activity(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        source = self.activity or (self.source if self.source.kind != "reviewed" else None)
        metadata = {
            "kind": "gateway" if self.activity else self.source.kind,
            "state": "unavailable",
            "label": "本機 Gateway 已記錄活動"
            if self.activity or self.source.kind == "gateway"
            else "合成示範活動"
            if self.source.kind == "demo"
            else "未接入實際用量來源",
            "as_of": None,
            "fetched_at": stamp(self.clock()),
            "basis": "local_recorded_generation_attempts_not_provider_billing",
        }
        if source is None:
            return metadata, None, None
        try:
            usage = _rows(source.usage())
            recent = source.recent()
            if not isinstance(recent, dict):
                raise TypeError
            tasks = _rows(recent.get("tasks"))
            if any(
                not isinstance(row.get("provider"), str)
                or not isinstance(row.get("model"), str)
                or _number(row.get("requests")) is None
                for row in usage
            ):
                raise ValueError
            for row in [*usage, *tasks]:
                for key in ("provider", "model"):
                    if row.get(key) is not None and (
                        not isinstance(row[key], str)
                        or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", row[key])
                    ):
                        raise ValueError
            if any(
                not isinstance(row.get("state"), str)
                or not re.fullmatch(r"[a-z_]{1,80}", row["state"])
                or _time(row.get("created_at")) is None
                or (row.get("completed_at") is not None and _time(row["completed_at"]) is None)
                for row in tasks
            ):
                raise ValueError
            metadata.update(state="available", as_of=stamp(self.clock()))
            return metadata, usage, tasks
        except Exception:  # noqa: BLE001 - unavailable activity must not hide account evidence
            return metadata, None, None

    def _runtime_configuration(self, targets: list[dict[str, Any]]) -> None:
        """A reviewed fixture's protective switch is never a live service reading."""
        rows = None
        if self.source.kind == "reviewed" and self.activity is not None:
            try:
                rows = _rows(self.activity.catalog())
                if any(
                    type(row.get("enabled")) is not bool
                    or any(
                        not isinstance(row.get(key), str)
                        or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", row[key])
                        for key in ("provider", "model")
                    )
                    for row in rows
                ):
                    rows = None
            except Exception:  # noqa: BLE001 - runtime failure preserves reviewed evidence
                rows = None
        for target in targets:
            config: dict[str, Any] = {
                "state": "unknown",
                "enabled": None,
                "observed_at": None,
                "basis": "live_catalog_model_match_not_account_qualification",
            }
            if self.source.kind != "reviewed":
                config.update(
                    state="available", enabled=target["enabled"], observed_at=stamp(self.clock())
                )
            elif rows is not None:
                matches = [
                    row
                    for row in rows
                    if (row["provider"], row["model"]) == (target["provider"], target["model"])
                ]
                config.update(
                    state="available"
                    if len(matches) == 1
                    else "ambiguous"
                    if matches
                    else "not_configured",
                    observed_at=stamp(self.clock()),
                )
                if len(matches) == 1:
                    config["enabled"] = matches[0]["enabled"]
            target["runtime_configuration"] = config

    def snapshot(self) -> dict[str, Any]:
        try:
            catalog, diag, as_of = self._metadata()
            activity_source, usage, recent = self._activity()
            targets = self._display_targets(catalog, diag)
            self._runtime_configuration(targets)
            inventory, models = self._models(catalog)
            return {
                **POLICY,
                **self._provider_evidence(targets, catalog, diag),
                "source": self._source(as_of),
                "targets": targets,
                "models": models,
                "model_inventory": inventory,
                "provider_inventory": self._provider_inventory(),
                "accounts": self.accounts.snapshot(self.clock()) if self.accounts else [],
                "activity_source": activity_source,
                "usage_basis": "local_recorded_generation_attempts_not_provider_billing",
                "usage": [
                    _project(
                        r,
                        (
                            "provider",
                            "model",
                            "requests",
                            "reported_input_tokens",
                            "reported_output_tokens",
                            "reported_neurons",
                            "ledger_held_count",
                            "outcome_unknown_count",
                        ),
                    )
                    for r in usage
                ]
                if usage is not None
                else None,
                "recent": [
                    _project(
                        r,
                        ("provider", "model", "state", "created_at", "completed_at", "latency_ms"),
                    )
                    for r in recent[:6]
                ]
                if recent is not None
                else None,
            }
        except Exception:  # noqa: BLE001 - sanitized public boundary
            raise DashboardError("source_unavailable") from None

    def _provider_inventory(self) -> dict[str, Any] | None:
        if self.source.kind == "demo":
            return None
        try:
            with PROVIDER_INVENTORY.open("rb") as source:
                raw = source.read(512 * 1024 + 1)
            if len(raw) <= 512 * 1024:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("schema_version") == 1:
                    return data
        except (OSError, ValueError, RecursionError):
            pass
        return None

    def _models(self, catalog: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        metadata = {
            "state": "unavailable",
            "basis": "instance_registry_not_provider_complete_catalog",
        }
        try:
            rows = _rows(self.source.models())
            result = []
            seen = set()
            for row in rows:
                identity = row.get("provider"), row.get("model")
                if (
                    any(
                        not isinstance(value, str)
                        or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", value)
                        for value in identity
                    )
                    or identity in seen
                ):
                    raise ValueError
                seen.add(identity)
                target_ids = row.get("configured_target_ids")
                expected = [
                    target["target_id"]
                    for target in catalog
                    if (target["provider"], target["model"]) == identity
                ]
                if (
                    not isinstance(target_ids, list)
                    or len(target_ids) > 256
                    or any(
                        not isinstance(value, str)
                        or not re.fullmatch(r"[A-Za-z0-9._~-]{1,160}", value)
                        for value in target_ids
                    )
                    or sorted(target_ids) != sorted(expected)
                    or row.get("model_scope")
                    != ("configured_gateway_target" if target_ids else "registered_unconfigured")
                ):
                    raise ValueError
                result.append(
                    {
                        **_model_projection(row),
                        "configured_target_ids": target_ids,
                        "model_scope": row["model_scope"],
                        "execution_allowed": False,
                        "paid_allowed": False,
                    }
                )
            if {(row["provider"], row["model"]) for row in catalog} - seen:
                raise ValueError
            metadata["state"] = "available"
            return metadata, result
        except Exception:  # noqa: BLE001 - old/unavailable inventories preserve account evidence
            return metadata, []

    def _provider_usage_report(self, filters: dict[str, str | None]) -> dict[str, Any]:
        now = self.clock()
        from_dt = _time(filters.get("from"))
        to_dt = _time(filters.get("to"))

        def in_window(as_of_str: str | None) -> bool:
            if from_dt is None and to_dt is None:
                return True
            t = _time(as_of_str)
            if t is None:
                return True
            if from_dt is not None and t < from_dt:
                return False
            return not (to_dt is not None and t >= to_dt)

        rows: list[dict[str, Any]] = []
        try:
            accounts = self.accounts.snapshot(now) if self.accounts else []
            meta_file = getattr(self, "provider_metadata_file", None) or getattr(
                self, "provider_metadata", None
            )
            meta_report = read_metadata(Path(meta_file), now) if meta_file else None
            catalog, diag, _ = self._metadata()
            targets = self._display_targets(catalog, diag)
            or_proj = self._openrouter_counter(catalog, diag, meta_report)
            inv = self._provider_inventory() or {}
        except Exception:  # noqa: BLE001
            return {
                "state": "unavailable",
                "local_ledger_state": "unavailable",
                "rows": [],
                "providers": [],
            }

        inv_names: dict[str, str] = {}
        inv_tasks: dict[str, set[str]] = {}
        inv_models: dict[str, set[str]] = {}
        for p in inv.get("providers") or []:
            pid = p.get("id")
            if not isinstance(pid, str):
                continue
            inv_names[pid] = p.get("name") or pid
            for api_row in p.get("apis") or []:
                inv_tasks.setdefault(pid, set()).update(api_row.get("tasks") or [])
            for cat in p.get("catalogs") or []:
                for m in cat.get("models") or []:
                    if isinstance(m.get("id"), str):
                        inv_models.setdefault(pid, set()).add(m["id"])
                    for v in m.get("variants") or []:
                        if isinstance(v.get("task"), str):
                            inv_tasks.setdefault(pid, set()).add(v["task"])

        # 1. OpenRouter key/account usage
        if or_proj is not None:
            daily = or_proj.get("daily") or or_proj.get("last_observed_daily")
            usage = or_proj.get("usage") or {}
            usage_as_of = or_proj.get("usage_checked_at", or_proj.get("checked_at"))
            usage_current = or_proj.get("usage_current", or_proj.get("state") == "current")
            metrics = []
            win_ok = in_window(or_proj.get("checked_at"))
            if isinstance(daily, dict):
                metrics.append(
                    {
                        "metric": "free_model_daily_requests",
                        "label": "免費模型日計數",
                        "unit": "requests",
                        "window": "day",
                        "used": daily.get("used"),
                        "limit": daily.get("limit"),
                        "remaining": daily.get("remaining"),
                        "as_of": or_proj.get("checked_at"),
                        "current": or_proj.get("state") == "current",
                        "window_matched": win_ok,
                    }
                )
            for key, label, win in (
                ("daily_usd", "今日金鑰費用", "day"),
                ("weekly_usd", "本週金鑰費用", "week"),
                ("monthly_usd", "本月金鑰費用", "month"),
                ("total_usd", "累計金鑰費用", "all_time"),
            ):
                if usage.get(key) is not None:
                    metrics.append(
                        {
                            "metric": key,
                            "label": label,
                            "unit": "USD",
                            "window": win,
                            "used": usage.get(key),
                            "limit": None,
                            "remaining": None,
                            "as_of": usage_as_of,
                            "current": usage_current,
                            "window_matched": in_window(usage_as_of),
                        }
                    )
            scope_note = "涵蓋此 OpenRouter API Key 所有呼叫（含未經 Broker 外部呼叫）；不依單一模型或任務拆分。"
            rows.append(
                {
                    "provider": "openrouter",
                    "name": inv_names.get("openrouter", "OpenRouter"),
                    "state": "reported",
                    "usage_state": "observed_current"
                    if or_proj.get("state") == "current"
                    else "observed_historical",
                    "scope_kind": "key_or_account_shared",
                    "includes_external_usage": True,
                    "as_of": or_proj.get("checked_at"),
                    "source": or_proj.get("source"),
                    "in_time_window": win_ok,
                    "models": sorted(inv_models.get("openrouter", set()) | {"openrouter/free"}),
                    "tasks": sorted(
                        inv_tasks.get("openrouter", set()) | {"text_generation", "vision"}
                    ),
                    "metrics": metrics,
                    "reported_metrics": metrics,
                    "scope_note": scope_note,
                    "external_usage_scope": scope_note,
                    "note": scope_note,
                    "local_recorded_generation_attempts": None,
                    "ledger_held_count": 0,
                }
            )

        # 2. OpenAI credit grant & usage page
        for acc in accounts:
            prov = acc.get("provider")
            if not isinstance(prov, str):
                continue
            ev = acc.get("evidence") or {}
            as_of = ev.get("as_of")
            current = bool(ev.get("current"))
            win_ok = in_window(as_of)
            metrics = []
            for g in acc.get("grants") or []:
                metrics.append(
                    {
                        "metric": "credit_grant_spend",
                        "label": "帳戶點數觀測，非實際支出",
                        "unit": acc.get("currency", "USD"),
                        "window": f"{(g.get('received') or {}).get('value', '?')}..{(g.get('expires') or {}).get('value', '?')}",
                        "used": None,
                        "limit": g.get("amount"),
                        "remaining": g.get("remaining"),
                        "as_of": as_of,
                        "current": current,
                        "window_matched": win_ok,
                    }
                )
            up = acc.get("usage_page") or {}
            if up.get("state"):
                metrics.append(
                    {
                        "metric": "usage_page_audit",
                        "label": f"官方 Usage 頁核對（{up.get('from_date', '?')} 至 {up.get('through_date', '?')}）"
                        + (
                            "・未回報用量資料，不代表零支出" if up.get("state") == "no_data" else ""
                        ),
                        "unit": acc.get("currency", "USD"),
                        "window": f"{up.get('from_date', '?')}..{up.get('through_date', '?')}",
                        "used": None,
                        "reported_state": up.get("state"),
                        "limit": None,
                        "remaining": acc.get("balance"),
                        "as_of": as_of,
                        "current": current,
                        "window_matched": win_ok,
                    }
                )
            acc_models = {
                m.get("model")
                for m in (acc.get("model_limits") or [])
                if isinstance(m, dict) and isinstance(m.get("model"), str)
            } | inv_models.get(prov, set())
            scope_note = "涵蓋組織／專案所有金鑰與來源（含未經 Broker 外部呼叫）；已核對模型共用同一筆贈送點數池。"
            rows.append(
                {
                    "provider": prov,
                    "name": inv_names.get(prov, prov),
                    "state": "observed",
                    "usage_state": "observed_current" if current else "observed_historical",
                    "scope_kind": "key_or_account_shared",
                    "includes_external_usage": True,
                    "as_of": as_of,
                    "source": (ev.get("sources") or [None])[0],
                    "in_time_window": win_ok,
                    "models": sorted(acc_models),
                    "tasks": sorted(
                        inv_tasks.get(prov, set())
                        | {
                            "text_generation",
                            "vision",
                            "image_generation",
                            "tts",
                            "audio_transcription",
                        }
                    ),
                    "metrics": metrics,
                    "reported_metrics": metrics,
                    "scope_note": scope_note,
                    "external_usage_scope": scope_note,
                    "note": scope_note,
                    "local_recorded_generation_attempts": None,
                    "ledger_held_count": 0,
                }
            )

        # 3. Reviewed target providers (Cloudflare, Mistral, Google, Groq, NVIDIA, OCR.space, HF, Cerebras, SambaNova)
        seen_providers = {r["provider"] for r in rows}
        for t in targets:
            prov = t.get("provider")
            model = t.get("model")
            if not isinstance(prov, str) or prov in seen_providers:
                continue
            seen_providers.add(prov)
            rev = t.get("reviewed_account") or {}
            raw_facts = rev.get("display_facts") or []
            facts_map: dict[str, Any] = {}
            if isinstance(raw_facts, dict):
                facts_map = dict(raw_facts)
            elif isinstance(raw_facts, list):
                for f in raw_facts:
                    if isinstance(f, dict) and isinstance(f.get("kind"), str):
                        facts_map[f["kind"]] = f.get("value")
            obs_list = t.get("observations") or []
            metrics = []
            as_of = rev.get("audit_session_at") or t.get("free_verified_at")
            source_url = (rev.get("sources") or [None])[0]
            state_short = "unknown"
            usage_state = "usage_unobserved"
            scope_note = "尚未接通唯讀已用量查詢；不將未知已用量視為 0。"
            if obs_list and obs_list[0].get("remaining") is not None:
                q0 = obs_list[0]
                as_of = q0.get("as_of") or as_of
                source_url = q0.get("source") or source_url
                is_curr = bool(q0.get("current"))
                state_short = "observed"
                usage_state = "observed_current" if is_curr else "observed_historical"
                win_ok = in_window(as_of)
                metrics.append(
                    {
                        "metric": q0.get("metric", "neurons"),
                        "label": "每日免費 Neurons（帳戶推算）",
                        "unit": q0.get("metric", "neurons"),
                        "window": q0.get("window", "day"),
                        "used": q0.get("used"),
                        "limit": q0.get("limit"),
                        "remaining": q0.get("remaining"),
                        "formula": q0.get("formula"),
                        "as_of": as_of,
                        "current": is_curr,
                        "window_matched": win_ok,
                    }
                )
                scope_note = "帳戶級 Workers AI 每日免費 Neurons（含未經 Broker 外部呼叫），由已核對已用量與每日上限推算。"
            elif "used_allowance_usd" in facts_map or "monthly_used_usd" in facts_map:
                state_short = "observed"
                usage_state = "observed_historical"
                used_usd = facts_map.get("used_allowance_usd", facts_map.get("monthly_used_usd"))
                lim_usd = facts_map.get("monthly_allowance_usd")
                win_ok = in_window(as_of)
                metrics.append(
                    {
                        "metric": "monthly_allowance_usd",
                        "label": "免費方案月度內含額度（畫面四捨五入）",
                        "unit": "USD",
                        "window": "month",
                        "used": used_usd,
                        "limit": lim_usd,
                        "remaining": None,
                        "as_of": as_of,
                        "current": False,
                        "window_matched": win_ok,
                    }
                )
                scope_note = "Mistral Console 月度內含額度已用量（含未經 Broker 外部呼叫；畫面四捨五入不直接倒算精確剩餘）。"
            prov_models = set(inv_models.get(prov, set()))
            if isinstance(model, str):
                prov_models.add(model)
            prov_tasks = set(inv_tasks.get(prov, set())) | set(t.get("capabilities") or [])
            rows.append(
                {
                    "provider": prov,
                    "name": inv_names.get(prov, prov),
                    "state": state_short,
                    "usage_state": usage_state,
                    "scope_kind": "key_or_account_shared"
                    if metrics
                    else "organization_or_project"
                    if rev.get("qualification_basis") in {"account_plan", "public_conditions"}
                    else "unverified",
                    "includes_external_usage": True if metrics else None,
                    "as_of": as_of,
                    "source": source_url,
                    "in_time_window": in_window(as_of),
                    "models": sorted(prov_models),
                    "tasks": sorted(prov_tasks),
                    "metrics": metrics,
                    "reported_metrics": metrics,
                    "scope_note": scope_note,
                    "external_usage_scope": scope_note,
                    "note": scope_note,
                    "local_recorded_generation_attempts": None,
                    "ledger_held_count": 0,
                }
            )

        filtered = []
        for r in rows:
            if filters["provider"] is not None and r["provider"] != filters["provider"]:
                continue
            if filters["model"] is not None and filters["model"] not in r.get("models", []):
                continue
            if (
                filters["task"] is not None
                and filters["task"] != "unknown"
                and filters["task"] not in r.get("tasks", [])
            ):
                continue
            if not r.get("in_time_window", True):
                continue
            row_copy = dict(r)
            notes = [r.get("scope_note") or ""]
            if filters["model"] is not None:
                row_copy["model_filter_note"] = (
                    f"篩選模型 {filters['model']}：此供應商用量為帳戶／金鑰共享計數，非單一模型獨立計數。"
                )
                notes.append(row_copy["model_filter_note"])
            if filters["task"] is not None:
                row_copy["task_filter_note"] = (
                    f"篩選任務 {filters['task']}：供應商帳戶計數涵蓋所有任務類型，未依單一任務分拆。"
                )
                notes.append(row_copy["task_filter_note"])
            row_copy["note"] = " ".join(n for n in notes if n)
            filtered.append(row_copy)

        local_source = self.activity or (self.source if self.source.kind != "reviewed" else None)
        return {
            "state": "available",
            "local_ledger_state": "available" if local_source is not None else "unavailable",
            "basis": "provider_reported_or_audited_account_usage_distinct_from_broker_ledger",
            "rows": filtered,
            "providers": filtered,
        }

    def usage_report(self, raw: dict[str, Any]) -> dict[str, Any]:
        filters = _usage_filters(raw)
        provider_usage = self._provider_usage_report(filters)
        source = self.activity or (self.source if self.source.kind != "reviewed" else None)
        metadata: dict[str, Any] = {
            "kind": "gateway" if self.activity else self.source.kind,
            "state": "unavailable",
            "label": "合成示範活動"
            if self.source.kind == "demo" and not self.activity
            else "本機 Gateway 已記錄活動"
            if source
            else "未接入實際用量來源",
            "as_of": None,
            "fetched_at": stamp(self.clock()),
            "basis": "local_recorded_generation_attempts_not_provider_billing",
        }
        result: dict[str, Any] = {
            **POLICY,
            "source": metadata,
            "filters": filters,
            "rows": None,
            "provider_usage": provider_usage,
            "task_metadata": {
                "state": "unavailable",
                "basis": "historical_task_labels_not_recorded",
            },
            "coverage": {
                "state": "unavailable",
                "time_basis": "dispatched_at",
                "interval": "from_inclusive_to_exclusive",
            },
        }
        if source is None:
            return result
        try:
            history = source.usage_history(filters)
            if (
                not isinstance(history, dict)
                or type(history.get("task_types_recorded")) is not bool
            ):
                raise ValueError
            typed = history["task_types_recorded"]
            rows = _rows(history.get("rows"))
            if filters["task"] is not None and not typed:
                raise ValueError
            for row in rows:
                if (
                    any(
                        not isinstance(row.get(key), str)
                        or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", row[key])
                        for key in ("provider", "model")
                    )
                    or _number(row.get("requests")) is None
                    or (filters["provider"] is not None and row["provider"] != filters["provider"])
                    or (filters["model"] is not None and row["model"] != filters["model"])
                    or (typed and row.get("task_type") not in TASK_TYPES)
                    or (filters["task"] is not None and row.get("task_type") != filters["task"])
                ):
                    raise ValueError
            result["rows"] = [
                {
                    **_project(row, USAGE_FIELDS),
                    "task_type": row["task_type"] if typed else "unknown",
                }
                for row in rows
            ]
            if typed:
                result["task_metadata"] = {
                    "state": "available_for_new_records",
                    "basis": "validated_capability_for_new_records_legacy_unknown",
                }
            result["coverage"]["state"] = "complete_for_local_recorded_attempts"
            metadata.update(state="available", as_of=stamp(self.clock()))
        except Exception:  # noqa: BLE001 - unavailable is unknown, never a fabricated empty report
            return result
        return result

    def advice(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("mode") == "match":
            from .task_matching import match_models, validate_match

            try:
                validate_match(raw)
                if "route_task" in raw:
                    validate_task(raw["route_task"], check_registry_constraints=False)
            except (ValueError, TypeError, AttributeError, GatewayError):
                raise DashboardError("invalid_task") from None
            snapshot = self.snapshot()
            task = raw.get("route_task")
            configured = task and any(
                task.get("provider") in (None, t["provider"])
                and task.get("model") in (None, t["model"])
                for t in snapshot["targets"]
            )
            checked = self.advice(task) if configured and isinstance(task, dict) else None
            return match_models(raw, snapshot, checked)
        # Task validation before contacting upstream; the Gateway remains the route authority.
        try:
            # Validate the common task schema/envelope locally; the actual Gateway
            # remains authoritative for its instance-local Registry constraints.
            validate_task(raw, check_registry_constraints=False)
        except (GatewayError, ValueError, TypeError, KeyError):
            raise DashboardError("invalid_task") from None
        try:
            catalog, diag, as_of = self._metadata()
            # Shared text routes use the same message contract as ProjectClient.
            # Leave an instance containing only legacy/custom providers intact.
            if (
                raw.get("capability") == "text_generation"
                and isinstance(raw.get("input"), str)
                and any(
                    row.get("provider") in {"openai", "openrouter"}
                    and row.get("enabled") is not False
                    and raw.get("provider") in (None, row.get("provider"))
                    and raw.get("model") in (None, row.get("model"))
                    for row in catalog
                )
            ):
                raw = {
                    **raw,
                    "input": {"messages": [{"role": "user", "content": raw["input"]}]},
                }
                validate_task(raw, check_registry_constraints=False)
            plan = self.source.explain(raw)
            if not isinstance(plan, dict):
                raise DashboardError("source_unavailable")
            by_id = {r["target_id"]: r for r in catalog}
            candidates = []
            for candidate in _rows(plan.get("candidates")):
                identity = candidate.get("target_id")
                if (
                    not isinstance(identity, str)
                    or identity not in by_id
                    or any(
                        candidate.get(key) != by_id[identity].get(key)
                        for key in ("provider", "model")
                    )
                ):
                    raise DashboardError("source_unavailable")
                target = self._target(by_id[identity], diag[identity])
                reasons = [
                    "billing_state_unknown"
                    if reason == "billing_enabled" and target["billing_state"] == "unknown"
                    else reason
                    for reason in _strings(candidate.get("reasons"))
                ]
                if target["state"] != "ready" or target["reasons"]:
                    reasons.append("diagnostics_not_ready")
                if candidate.get("eligible") is not True:
                    reasons.append("gateway_ineligible")
                credentials = target["credentials"]
                if credentials["state"] != "present" or not _current(
                    credentials["verified_at"], credentials["expires_at"], self.clock()
                ):
                    reasons.append("credential_inventory_not_current")
                if target["billing_state"] != "disabled":
                    reasons.append(
                        "billing_enabled" if target["billing_enabled"] else "billing_state_unknown"
                    )
                if not target["free_eligible"] or not _current(
                    target["free_verified_at"], target["free_evidence_expires_at"], self.clock()
                ):
                    reasons.append("free_evidence_not_current")
                observations = [
                    *_rows(candidate.get("quota_observations")),
                    *_rows(diag[identity].get("quota_observations")),
                ]
                required = {
                    (q["metric"], q["window"])
                    for q in (target["required_capacity"] or target["local_caps"])
                }
                required.update(
                    (q.get("metric"), q.get("window"))
                    for q in observations
                    if _current(q.get("as_of"), q.get("valid_until"), self.clock())
                )
                if not required:
                    reasons.append("quota_evidence_missing")
                input_cost = _number(plan.get("estimated_input_tokens"))
                output_cost = _number(
                    candidate.get("request_limits", {}).get("requested_output_tokens")
                )
                if output_cost is not None and output_cost > 65536:
                    output_cost = None
                neuron_cost = _number(candidate.get("effective_neuron_bound"))
                estimate = candidate.get("neuron_estimate")
                if (
                    not isinstance(estimate, dict)
                    or not _current(
                        estimate.get("verified_at"), estimate.get("expires_at"), self.clock()
                    )
                    or input_cost is None
                    or output_cost is None
                    or _number(estimate.get("amount")) is None
                    or neuron_cost is None
                    or neuron_cost < estimate["amount"]
                    or input_cost > (_number(estimate.get("max_input_tokens")) or 0)
                    or output_cost > (_number(estimate.get("max_output_tokens")) or 0)
                ):
                    neuron_cost = None
                costs = {
                    "requests": 1,
                    "input_tokens": input_cost,
                    "output_tokens": output_cost,
                    "total_tokens": input_cost + output_cost
                    if input_cost is not None and output_cost is not None
                    else None,
                    "neurons": neuron_cost,
                    # This is the Gateway's conservative reservation bound, not
                    # provider-reported dollar usage or an account balance.
                    "credits_usd_micros": _number(
                        (candidate.get("resource_cost_bounds") or {}).get("credits_usd_micros")
                    ),
                }
                for metric, window in sorted(required):
                    normalized = "total_tokens" if metric == "tokens" else metric
                    cost = costs.get(normalized)
                    matches = [
                        q
                        for q in observations
                        if ("total_tokens" if q.get("metric") == "tokens" else q.get("metric"))
                        == normalized
                        and q.get("window") == window
                    ]
                    valid = [
                        q
                        for q in matches
                        if _current(q.get("as_of"), q.get("valid_until"), self.clock())
                        and q.get("provenance") in {"official", "observed"}
                        and q.get("confidence")
                        in {"provider_reported", "administrator_verified", "configured_evidence"}
                        and _number(q.get("effective_remaining")) is not None
                    ]
                    if cost is None:
                        reasons.append("quota_cost_unknown:" + str(normalized))
                    elif not valid or len(valid) != len(matches):
                        reasons.append(
                            "quota_evidence_not_current:" + str(normalized) + ":" + str(window)
                        )
                    elif any(q["effective_remaining"] < cost for q in valid):
                        reasons.append("quota_insufficient:" + str(normalized) + ":" + str(window))
                local_caps = (
                    by_id[identity].get("local_safety_caps") or by_id[identity].get("quotas") or []
                )
                if not target["required_capacity"] and any(
                    q.get("window") in {"day", "month"} and q.get("timezone", "UTC") != "UTC"
                    for q in local_caps
                ):
                    reasons.append("quota_window_alignment_unknown")
                candidates.append(
                    {
                        "target_id": target["target_id"],
                        "provider": target["provider"],
                        "model": target["model"],
                        "safe": not reasons,
                        "reasons": list(dict.fromkeys(reasons)),
                        "remaining": target["observations"],
                        "ranking": {
                            "policy": "verified_expiry_then_headroom_health",
                            "value_loss_seconds": _number(
                                candidate.get("ranking_factors", {}).get("value_loss_seconds")
                            ),
                            "value_loss_basis": candidate.get("ranking_factors", {}).get(
                                "value_loss_basis"
                            )
                            if candidate.get("ranking_factors", {}).get("value_loss_basis")
                            in {"credit_expiry", "quota_reset"}
                            else None,
                            "observed_headroom": _fraction(
                                candidate.get("ranking_factors", {}).get("observed_headroom")
                            ),
                        },
                    }
                )
            if len(candidates) != len(catalog) or len({r["target_id"] for r in candidates}) != len(
                candidates
            ):
                raise DashboardError("source_unavailable")
            selected = next((r for r in candidates if r["safe"]), None)
            recheck = (
                _text(plan.get("next_retry_at"))
                if selected is None
                and plan.get("temporary") is True
                and _time(plan.get("next_retry_at"))
                else None
            )
            return {
                **POLICY,
                "source": self._source(as_of),
                "decision": "recommend" if selected else "ask_user",
                "selected_target": selected,
                "candidates": candidates,
                "reasons": ["current_authoritative_free_capacity"]
                if selected
                else ["no_confirmed_safe_target"],
                "recheck_at": recheck,
                "recheck_is_estimate": bool(recheck),
            }
        except GatewayError as error:
            raise DashboardError(
                "invalid_task" if error.code == "invalid_request" else "source_unavailable"
            ) from None
        except DashboardError as error:
            raise DashboardError(
                "invalid_task" if str(error) == "invalid_task" else "source_unavailable"
            ) from None
        except Exception:  # noqa: BLE001 - sanitized public boundary
            raise DashboardError("source_unavailable") from None
