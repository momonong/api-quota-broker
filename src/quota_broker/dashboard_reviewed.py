"""Private manually reviewed metadata; no credentials, network or execution permission."""

import copy
import json
import math
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ConfigError, Target, load_gateway_config
from .core import utcnow
from .dashboard import GatewaySource
from .gateway import Gateway
from .registry import Registry

KIND = "manually_reviewed_account_snapshot"
PRECISION = "session_clock_not_per_provider_observation"
REVIEW_DAYS = 14
DERIVED_TTL = timedelta(minutes=15)
SCOPES = {"account", "project", "public_plan", "public_model", "public_free_key", "account_ip"}
BASES = {"account", "public_plan"}
RESETS = {"pacific_midnight_project", "utc_midnight", "calendar_month_day_1", "unknown"}
DISPLAY_KINDS = {
    "requests_per_second",
    "monthly_allowance_usd",
    "used_allowance_usd",
    "credits_usd",
    "historical_requests",
    "historical_tokens",
    "historical_cost_usd",
    "purchases",
}
PERIODS = {
    "per_second",
    "calendar_month",
    "this_month",
    "last_month",
    "28_days",
    "account",
    "current_day",
}
SOURCE_HOSTS = {
    "build.nvidia.com",
    "docs.api.nvidia.com",
    "aistudio.google.com",
    "ai.google.dev",
    "console.groq.com",
    "admin.mistral.ai",
    "docs.mistral.ai",
    "dash.cloudflare.com",
    "developers.cloudflare.com",
    "openrouter.ai",
    "ocr.space",
    "huggingface.co",
    "cloud.sambanova.ai",
    "cloud.cerebras.ai",
    "inference-docs.cerebras.ai",
}
TARGET_FIELDS = {
    "id",
    "provider",
    "model",
    "account_id",
    "account_id_ref",
    "enabled",
    "free_eligible",
    "billing_enabled",
    "verified_at",
    "expires_at",
    "source",
    "priority",
    "concurrency_limit",
    "shared_concurrency_scope",
    "shared_concurrency_limit",
    "max_output_tokens",
    "local_safety_caps",
    "provider_quota_facts",
    "capacity",
    "secret_ref",
}


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("reviewed snapshot forbids credential resolution and provider transport")


def _object(raw: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError
    return raw


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _private_json(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise TypeError
    return value


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result.astimezone(UTC)


def _safe_text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(c) < 32 for c in value)
        or re.search(
            r"(?i)bearer|api[_ -]?key|token[=:]|password|secret[=:]|@|<|>|[A-Za-z0-9_/-]{48,}",
            value,
        )
    ):
        raise ValueError
    return value


def _source(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in SOURCE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    return value


def _bounded_list(value: Any, maximum: int = 32) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    return value


def _number(value: Any) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 10**15:
        raise ValueError
    return value


def _metadata(raw: Any, target: Target, anchor: datetime, due: datetime) -> dict[str, Any]:
    row = _object(
        raw,
        {
            "target_id",
            "qualification_basis",
            "billing_state",
            "plan",
            "notes",
            "sources",
            "limit_context",
            "display_facts",
            "derived_remaining",
        },
    )
    if (
        row["target_id"] != target.id
        or row["qualification_basis"] not in {"account_plan", "public_conditions", "unverified"}
        or row["plan"] not in {"free", "unknown"}
    ):
        raise ValueError
    if row["billing_state"] not in {"disabled", "unknown"} or target.billing_enabled != (
        row["billing_state"] == "unknown"
    ):
        raise ValueError
    if (
        target.enabled
        or type(target.enabled) is not bool
        or type(target.free_eligible) is not bool
        or type(target.billing_enabled) is not bool
    ):
        raise ValueError
    qualified = row["qualification_basis"] != "unverified"
    if target.free_eligible != qualified or (
        qualified
        and (target.verified_at != anchor or target.expires_at != due or row["plan"] != "free")
    ):
        raise ValueError
    if not qualified and (target.verified_at is not None or target.expires_at is not None):
        raise ValueError
    if target.provider in {"sambanova", "huggingface", "cerebras"} and (
        qualified or target.provider_quota_facts or target.capacity.kind != "unknown"
    ):
        raise ValueError
    for text in _bounded_list(row["notes"]):
        _safe_text(text)
    for source in _bounded_list(row["sources"]):
        _source(source)
    contexts = {}
    for context in _bounded_list(row["limit_context"]):
        item = _object(context, {"metric", "window", "basis", "reset"})
        dimension = (item["metric"], item["window"])
        if dimension in contexts or item["basis"] not in BASES or item["reset"] not in RESETS:
            raise ValueError
        contexts[dimension] = item
    for fact in _bounded_list(row["display_facts"]):
        item = _object(fact, {"kind", "value", "period", "precision"})
        if (
            item["kind"] not in DISPLAY_KINDS
            or item["period"] not in PERIODS
            or item["precision"] not in {"exact", "rounded"}
        ):
            raise ValueError
        _number(item["value"])
        if item["kind"] == "requests_per_second" and item["period"] != "per_second":
            raise ValueError
        if (
            item["kind"] in {"monthly_allowance_usd", "used_allowance_usd"}
            and item["period"] != "calendar_month"
        ):
            raise ValueError
    known = set()
    derived_facts = []
    for fact in target.provider_quota_facts:
        limit, remaining = fact.limit, fact.remaining
        if limit.value is not None:
            known.add((fact.metric, fact.window))
            if (
                limit.as_of != anchor
                or limit.valid_until != due
                or limit.provenance not in {"official", "observed"}
                or limit.scope not in SCOPES
            ):
                raise ValueError
            _source(limit.source)
        if remaining.value is not None:
            derived_facts.append(fact)
            if (
                target.provider != "cloudflare"
                or (fact.metric, fact.window) != ("neurons", "day")
                or remaining.provenance != "estimated"
                or remaining.as_of != anchor
                or remaining.valid_until is None
                or not anchor < remaining.valid_until <= anchor + DERIVED_TTL
            ):
                raise ValueError
            _source(remaining.source)
        elif remaining.provenance != "unknown":
            raise ValueError
    if set(contexts) != known:
        raise ValueError
    derived = row["derived_remaining"]
    if derived is None:
        if derived_facts:
            raise ValueError
    else:
        item = _object(derived, {"metric", "window", "used", "limit", "reset_at", "note"})
        if (
            len(derived_facts) != 1
            or (item["metric"], item["window"]) != ("neurons", "day")
            or item["note"] != "derived_from_used_and_limit"
        ):
            raise ValueError
        fact = derived_facts[0]
        if (
            type(item["used"]) is not int
            or type(item["limit"]) is not int
            or not 0 <= item["used"] <= item["limit"]
            or item["limit"] != fact.limit.value
            or fact.remaining.value != item["limit"] - item["used"]
        ):
            raise ValueError
        reset = _instant(item["reset_at"])
        next_midnight = (anchor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if (
            reset != next_midnight
            or fact.remaining.valid_until is None
            or fact.remaining.valid_until > reset
        ):
            raise ValueError
    return copy.deepcopy(row)


class ReviewedSource(GatewaySource):
    kind = "reviewed"

    def __init__(
        self, gateway: Gateway, profile: dict[str, Any], rows: dict[str, dict[str, Any]]
    ) -> None:
        super().__init__(gateway)
        self.profile = profile
        self.rows = rows

    def source_metadata(self) -> dict[str, Any]:
        return {
            key: self.profile[key]
            for key in ("audit_session_at", "review_due_at", "time_precision")
        }

    def catalog(self) -> Any:
        rows = super().catalog()
        for item in rows:
            item["enabled"] = False
            item["reviewed_account"] = copy.deepcopy(self.rows[item["target_id"]])
        return rows

    def diagnostics(self) -> Any:
        result = super().diagnostics()
        for row in result["targets"]:
            derived = self.rows[row["target_id"]]["derived_remaining"]
            if derived:
                for observation in row["quota_observations"]:
                    if (observation["metric"], observation["window"]) == (
                        derived["metric"],
                        derived["window"],
                    ):
                        observation["reset_at"] = derived["reset_at"]
                        observation["remaining_basis"] = derived["note"]
        return result


def create_reviewed(
    directory: str | Path, config: str | Path, *, clock: Callable[[], datetime] = utcnow
) -> ReviewedSource:
    """Load three private sibling files; timestamps are evidence, never refreshed."""
    try:
        path = Path(config)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError
        profile = _object(
            _private_json(path),
            {
                "schema_version",
                "kind",
                "audit_session_at",
                "time_precision",
                "review_due_at",
                "targets",
            },
        )
        if (
            type(profile["schema_version"]) is not int
            or profile["schema_version"] != 1
            or profile["kind"] != KIND
            or profile["time_precision"] != PRECISION
        ):
            raise ValueError
        anchor, due = _instant(profile["audit_session_at"]), _instant(profile["review_due_at"])
        if due != anchor + timedelta(days=REVIEW_DAYS):
            raise ValueError
        document = _object(_private_json(path.parent / "gateway.json"), {"targets"})
        for item in _bounded_list(document["targets"]):
            if not isinstance(item, dict) or set(item) - TARGET_FIELDS:
                raise ValueError
        registry = Registry.from_manifest(_private_json(path.parent / "registry.json"))
        targets = load_gateway_config(path.parent / "gateway.json", registry=registry)
        raw_rows = _bounded_list(profile["targets"])
        if len(raw_rows) != len(targets) or not targets:
            raise ValueError
        by_id = {row["target_id"]: row for row in raw_rows}
        if len(by_id) != len(targets) or set(by_id) != {target.id for target in targets}:
            raise ValueError
        rows = {target.id: _metadata(by_id[target.id], target, anchor, due) for target in targets}
    except Exception:  # noqa: BLE001 - fixed boundary never reflects local metadata
        raise ConfigError("invalid reviewed profile") from None
    gateway = Gateway(
        Path(directory) / "dashboard-reviewed.db",
        targets,
        b"offline-reviewed-digest-material-00000",
        _forbidden,
        _forbidden,
        clock=clock,
        registry=registry,
        family_transport=_forbidden,
    )
    return ReviewedSource(gateway, profile, rows)
