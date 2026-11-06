"""Read-only model-list and current-key evidence for the dashboard."""

import json
import os
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


def validate_report(raw: bytes, now: datetime) -> dict[str, Any]:
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("provider report too large")
    _json_depth(raw, maximum=8)
    report = _object(
        json.loads(raw, object_pairs_hook=_unique), "schema_version kind models openrouter"
    )
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise ValueError("invalid provider report version")
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
        row = _object(report["openrouter"], "source checked_at daily")
        if row["source"] != KEY_URL or _instant(row["checked_at"]) > now:
            raise ValueError("invalid counter observation")
        counter_fields(row["daily"])
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
    return {
        "scope": "queried_key_tier_policy",
        "source": row["source"],
        "checked_at": row["checked_at"],
        "valid_until": until.isoformat(),
        "reset_at": reset.isoformat(),
        "state": state,
        "daily": row["daily"] if state == "current" else None,
    }
