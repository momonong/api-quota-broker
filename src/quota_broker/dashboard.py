"""Allowlisted dashboard projection and conservative, advice-only admission."""

import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from http.client import HTTPConnection
from typing import Any, Protocol
from urllib.parse import urlsplit

from .core import stamp, utcnow
from .gateway import Gateway, GatewayError, validate_task
from .gateway_server import _bounded_json, _json_depth

MAX_RESPONSE = 2 * 1024 * 1024
POLICY = {"execution_allowed": False, "paid_allowed": False, "mode": "single_task_advice"}


class DashboardError(ValueError):
    """Safe fixed error code, never an upstream exception."""


class Source(Protocol):
    kind: str

    def catalog(self) -> Any: ...
    def diagnostics(self) -> Any: ...
    def usage(self) -> Any: ...
    def recent(self) -> Any: ...
    def explain(self, task: dict[str, Any]) -> Any: ...


class GatewaySource:
    kind = "demo"

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def catalog(self) -> Any:
        return self.gateway.catalog()

    def diagnostics(self) -> Any:
        return self.gateway.diagnostics()

    def usage(self) -> Any:
        return self.gateway.usage()

    def recent(self) -> Any:
        return self.gateway.recent(limit=6)

    def explain(self, task: dict[str, Any]) -> Any:
        return self.gateway.explain(task)


class UpstreamSource:
    """Only a literal IPv4 loopback Gateway; no proxy, redirects or generic proxy API."""

    kind = "gateway"
    ROUTES = frozenset(
        {
            ("GET", "/v1/catalog"),
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
        if (method, path) not in self.ROUTES:
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

    def catalog(self) -> Any:
        return self._get("GET", "/v1/catalog")

    def diagnostics(self) -> Any:
        return self._get("GET", "/v1/diagnostics")

    def usage(self) -> Any:
        return self._get("GET", "/v1/usage")

    def recent(self) -> Any:
        return self._get("GET", "/v1/tasks?limit=6")

    def explain(self, task: dict[str, Any]) -> Any:
        return self._get("POST", "/v1/routes/explain", task)


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


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result if result and result.tzinfo else None
    except ValueError:
        return None


def _current(start: Any, end: Any, now: datetime) -> bool:
    since, until = _time(start), _time(end)
    return bool(since and until and since <= now < until)


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
        if key not in {"provider", "model", "state", "created_at", "completed_at"}
        else _text(row.get(key))
        for key in fields
    }


class Dashboard:
    def __init__(self, source: Source, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.source, self.clock = source, clock

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
            "label": "合成示範資料・非帳號讀值"
            if self.source.kind == "demo"
            else "本機 Gateway 中繼資料",
            "as_of": as_of,
            "fetched_at": stamp(self.clock()),
            "freshness": "current_snapshot",
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
        return {
            "target_id": _text(item.get("target_id")),
            "provider": _text(item.get("provider")),
            "model": _text(item.get("model")),
            "state": diag.get("state")
            if diag.get("state") in {"ready", "blocked", "unknown"}
            else "unknown",
            "free_eligible": item.get("free_eligible") is True,
            "billing_enabled": item.get("billing_enabled") is not False,
            "free_verified_at": _text(item.get("account_verified_at")),
            "free_evidence_expires_at": _text(item.get("account_expires_at")),
            "credentials": {
                "state": credentials.get("state")
                if credentials.get("state") in {"present", "missing", "unknown"}
                else "unknown",
                "verified_at": _text(credentials.get("verified_at")),
                "expires_at": _text(credentials.get("expires_at")),
            },
            "reasons": _strings(diag.get("reasons")),
            "local_caps": caps,
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
                        )
                    },
                    **{
                        k: _number(q.get(k))
                        for k in (
                            "remaining",
                            "limit",
                            "effective_remaining",
                            "local_observation_hold",
                        )
                    },
                    "current": _current(q.get("as_of"), q.get("valid_until"), self.clock()),
                }
                for q in _rows(diag.get("quota_observations"))
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        try:
            catalog, diag, as_of = self._metadata()
            usage = _rows(self.source.usage())
            recent = self.source.recent()
            if not isinstance(recent, dict):
                raise DashboardError("source_unavailable")
            targets = [self._target(item, diag[item["target_id"]]) for item in catalog]
            return {
                **POLICY,
                "source": self._source(as_of),
                "targets": targets,
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
                ],
                "recent": [
                    _project(
                        r,
                        ("provider", "model", "state", "created_at", "completed_at", "latency_ms"),
                    )
                    for r in _rows(recent.get("tasks"))[:6]
                ],
            }
        except Exception:  # noqa: BLE001 - sanitized public boundary
            raise DashboardError("source_unavailable") from None

    def advice(self, raw: dict[str, Any]) -> dict[str, Any]:
        # Task validation before contacting upstream; the Gateway remains the route authority.
        try:
            # Validate the common task schema/envelope locally; the actual Gateway
            # remains authoritative for its instance-local Registry constraints.
            validate_task(raw, check_registry_constraints=False)
        except (GatewayError, ValueError, TypeError, KeyError):
            raise DashboardError("invalid_task") from None
        try:
            plan = self.source.explain(raw)
            catalog, diag, as_of = self._metadata()
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
                reasons = _strings(candidate.get("reasons"))
                if target["state"] != "ready" or target["reasons"]:
                    reasons.append("diagnostics_not_ready")
                if candidate.get("eligible") is not True:
                    reasons.append("gateway_ineligible")
                credentials = target["credentials"]
                if credentials["state"] != "present" or not _current(
                    credentials["verified_at"], credentials["expires_at"], self.clock()
                ):
                    reasons.append("credential_inventory_not_current")
                if target["billing_enabled"]:
                    reasons.append("billing_enabled")
                if not target["free_eligible"] or not _current(
                    target["free_verified_at"], target["free_evidence_expires_at"], self.clock()
                ):
                    reasons.append("free_evidence_not_current")
                observations = [
                    *_rows(candidate.get("quota_observations")),
                    *_rows(diag[identity].get("quota_observations")),
                ]
                required = {(q["metric"], q["window"]) for q in target["local_caps"]}
                required.update((q.get("metric"), q.get("window")) for q in observations)
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
                if any(
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
