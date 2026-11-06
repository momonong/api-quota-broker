"""Read-only model-list and current-key evidence for the dashboard."""

import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .dashboard_accounts import _instant, _object, _unique
from .discovery_parsers import SOURCES
from .discovery_sources import PUBLIC_URLS
from .gateway_server import _json_depth
from .provider_policy import catalog_model_id_reason

KEY_URL = "https://openrouter.ai/api/v1/key"
MAX_REPORT_BYTES = 256 * 1024
MODEL_TTL = timedelta(days=1)
COUNTER_TTL = timedelta(minutes=15)
REFRESH_SCHEMA_VERSION = 2

# Capabilities, not promises of an account balance. No inferred quota authority.
REFRESH_SOURCES = {
    "openrouter": (
        "api",
        "https://openrouter.ai/settings/credits",
        "https://openrouter.ai/docs/api_reference/limits",
        "daily_counter",
    ),
    "openai": (
        "console",
        "https://platform.openai.com/settings/organization/billing/overview",
        "https://developers.openai.com/api/docs/guides/chatgpt-api-credits",
        "credit_grants_console",
    ),
    "google": (
        "permission_required",
        "https://aistudio.google.com/usage",
        "https://ai.google.dev/gemini-api/docs/rate-limits",
        "project_monitoring_required",
    ),
    "cloudflare": (
        "console",
        "https://dash.cloudflare.com/",
        "https://developers.cloudflare.com/workers-ai/platform/pricing/",
        "analytics_not_billing",
    ),
    "groq": (
        "console",
        "https://console.groq.com/settings/limits",
        "https://console.groq.com/docs/rate-limits",
        "headers_require_inference",
    ),
    "mistral": (
        "permission_required",
        "https://admin.mistral.ai/",
        "https://docs.mistral.ai/admin/admin-api/usage-metrics",
        "enterprise_admin_required",
    ),
    "nvidia": (
        "console",
        "https://build.nvidia.com/",
        "https://docs.api.nvidia.com/",
        "console_review",
    ),
    "ocrspace": (
        "console",
        "https://ocr.space/ocrapi",
        "https://ocr.space/ocrapi",
        "no_verified_counter_api",
    ),
    "huggingface": (
        "console",
        "https://huggingface.co/settings/billing",
        "https://huggingface.co/docs/inference-providers/en/pricing",
        "credit_grants_console",
    ),
    "cerebras": (
        "console",
        "https://cloud.cerebras.ai/",
        "https://inference-docs.cerebras.ai/support/rate-limits",
        "headers_require_inference",
    ),
    "sambanova": (
        "console",
        "https://cloud.sambanova.ai/plans/usage",
        "https://docs.sambanova.ai/",
        "console_review",
    ),
}
REFRESH_STATUSES = {
    "updated",
    "counter_unavailable",
    "credential_unavailable",
    "query_failed",
    "parse_failed",
    "unsupported_source",
    "console_required",
    "permission_required",
}
PROVIDER_REFRESH_OPTIONAL_FIELDS = {
    "queried",
    "checked_at",
    "credential_state",
    "model_probe_status",
    "verified_models",
    "balance_probe_status",
    "account_plan",
}
MODEL_PROBE_STATUSES = {
    "verified",
    "documented",
    "unverified",
    "credential_unavailable",
    "query_failed",
    "parse_failed",
    "not_applicable",
}
OPENROUTER_USAGE_FIELDS = (
    "total_usd",
    "daily_usd",
    "weekly_usd",
    "monthly_usd",
    "byok_total_usd",
    "byok_daily_usd",
    "byok_weekly_usd",
    "byok_monthly_usd",
)


def model_sources(provider: str) -> set[str]:
    return {SOURCES[provider]} | {url for (name, _), url in PUBLIC_URLS.items() if name == provider}


def counter_fields(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    row = _object(value, "used limit remaining")
    if any(type(n) is not int or not 0 <= n <= 10**9 for n in row.values()):
        raise ValueError("invalid daily counter")
    if row["remaining"] != max(0, row["limit"] - row["used"]):
        raise ValueError("inconsistent daily counter")
    return row


def _validate_openrouter_usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or (
        set(value) - set(OPENROUTER_USAGE_FIELDS) - {"is_free_tier"}
    ):
        raise ValueError("invalid openrouter usage")
    for key in OPENROUTER_USAGE_FIELDS:
        item = value.get(key)
        if item is not None and (
            not isinstance(item, str) or not re.fullmatch(r"\d+(?:\.\d{1,12})?", item)
        ):
            raise ValueError("invalid openrouter usage amount")
    if value.get("is_free_tier") is not None and type(value["is_free_tier"]) is not bool:
        raise ValueError("invalid openrouter tier flag")
    return value


def _validate_provider_refresh(provider: str, item: Any, now: datetime) -> None:
    if not isinstance(item, dict) or "status" not in item:
        raise ValueError("invalid refresh item")
    if set(item) - ({"status"} | PROVIDER_REFRESH_OPTIONAL_FIELDS):
        raise ValueError("invalid refresh item fields")
    allowed = (
        {"updated", "counter_unavailable", "credential_unavailable", "query_failed", "parse_failed"}
        if provider == "openrouter"
        else {
            "permission_required",
            "console_required",
            "credential_unavailable",
            "query_failed",
            "parse_failed",
            "unsupported_source",
        }
    )
    if item["status"] not in allowed:
        raise ValueError("invalid refresh result")
    if "queried" in item and type(item["queried"]) is not bool:
        raise ValueError("invalid refresh queried flag")
    if (
        "checked_at" in item
        and item["checked_at"] is not None
        and _instant(item["checked_at"]) > now
    ):
        raise ValueError("future provider refresh")
    if "credential_state" in item and item["credential_state"] not in {
        "present",
        "missing",
        "unverified",
    }:
        raise ValueError("invalid refresh credential state")
    if "model_probe_status" in item and item["model_probe_status"] not in MODEL_PROBE_STATUSES:
        raise ValueError("invalid model probe status")
    if (
        "verified_models" in item
        and item["verified_models"] is not None
        and (type(item["verified_models"]) is not int or not 0 <= item["verified_models"] <= 10000)
    ):
        raise ValueError("invalid verified models count")
    if "balance_probe_status" in item and item["balance_probe_status"] not in REFRESH_STATUSES:
        raise ValueError("invalid balance probe status")
    if "account_plan" in item and item["account_plan"] not in {
        "free",
        "pro",
        "plus_subscription_grant",
        "unknown",
        None,
    }:
        raise ValueError("invalid account plan")


def validate_report(raw: bytes, now: datetime) -> dict[str, Any]:
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("provider report too large")
    _json_depth(raw, maximum=8)
    report = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(report, dict):
        raise TypeError("invalid provider report")
    _object(
        report,
        "schema_version kind models openrouter"
        + (" refresh" if report.get("schema_version") == 2 else ""),
    )
    if type(report["schema_version"]) is not int or report["schema_version"] not in {1, 2}:
        raise ValueError("invalid provider report version")
    if report["schema_version"] == 2:
        refresh = _object(report["refresh"], "attempted_at providers")
        if _instant(refresh["attempted_at"]) > now:
            raise ValueError("future refresh")
        providers = _object(refresh["providers"], " ".join(REFRESH_SOURCES))
        for provider, item in providers.items():
            _validate_provider_refresh(provider, item, now)
    if report["kind"] != "provider_metadata_observations":
        raise ValueError("invalid provider report kind")
    if not isinstance(report["models"], list) or len(report["models"]) > 256:
        raise ValueError("invalid model observations")
    seen = set()
    for item in report["models"]:
        row = _object(item, "provider model source checked_at status")
        provider = row["provider"]
        identity = provider, row["model"]
        if (
            provider not in SOURCES
            or catalog_model_id_reason(row["model"]) is not None
            or identity in seen
            or row["source"] not in model_sources(provider)
            or row["status"] not in {"listed", "inactive", "not_listed", "unknown", "documented"}
            or (provider == "ocrspace") != (row["status"] == "documented")
            or _instant(row["checked_at"]) > now
        ):
            raise ValueError("invalid model observation")
        seen.add(identity)
    if report["openrouter"] is not None:
        if not isinstance(report["openrouter"], dict):
            raise ValueError("invalid counter observation")
        allowed_or_keys = {"source", "checked_at", "daily", "usage"}
        if not {"source", "checked_at", "daily"} <= set(report["openrouter"]) <= allowed_or_keys:
            raise ValueError("invalid counter observation")
        row = report["openrouter"]
        if row["source"] != KEY_URL or _instant(row["checked_at"]) > now:
            raise ValueError("invalid counter observation")
        counter_fields(row["daily"])
        if "usage" in row:
            _validate_openrouter_usage(row["usage"])
    if (
        report.get("refresh")
        and report["refresh"]["providers"]["openrouter"]["status"] == "updated"
        and (report["openrouter"] is None or report["openrouter"]["daily"] is None)
    ):
        raise ValueError("updated_counter_missing")
    return report


def read_metadata(path: Path, now: datetime) -> dict[str, Any] | None:
    """Read on each request so atomic local refreshes do not require a restart."""
    try:
        if ".." in path.parts:
            raise ValueError("untrusted provider report path")
        for parent in path.absolute().parents:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError("untrusted provider report path")
        parent_stat = path.parent.lstat()
        if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
            raise ValueError("private provider report directory required")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("private provider report required")
            raw = stream.read(MAX_REPORT_BYTES + 1)
        return validate_report(raw, now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return None


def quota_projection(report: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    row = report["openrouter"]
    if row is None:
        return None
    checked = _instant(row["checked_at"])
    reset = (checked + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    until = min(checked + COUNTER_TTL, reset)
    state = "stale" if now >= until else "unknown" if row["daily"] is None else "current"
    refresh = report.get("refresh")
    if refresh and refresh["providers"]["openrouter"]["status"] != "updated":
        state = "stale" if now >= until else "unknown"
    projected: dict[str, Any] = {
        "scope": "queried_key_tier_policy",
        "source": row["source"],
        "checked_at": row["checked_at"],
        "valid_until": until.isoformat(),
        "reset_at": reset.isoformat(),
        "state": state,
        "daily": row["daily"] if state == "current" else None,
        "last_observed_daily": row["daily"],
    }
    if row.get("usage") is not None:
        projected["usage"] = row["usage"]
    return projected


def refresh_projection(report: dict[str, Any]) -> dict[str, Any]:
    refresh = report.get("refresh")
    providers: dict[str, Any] = {}
    for provider, (method, console, documentation, reason) in REFRESH_SOURCES.items():
        raw_item = refresh["providers"][provider] if refresh else {}
        entry: dict[str, Any] = {
            "method": method,
            "console": console,
            "documentation": documentation,
            "reason": reason,
            "status": raw_item.get("status", "not_checked"),
        }
        for key in PROVIDER_REFRESH_OPTIONAL_FIELDS:
            if key in raw_item:
                entry[key] = raw_item[key]
        providers[provider] = entry
    return {
        "attempted_at": refresh["attempted_at"] if refresh else None,
        "providers": providers,
    }
