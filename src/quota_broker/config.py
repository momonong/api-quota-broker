"""Account eligibility, provider evidence, and distinct local admission caps."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import MODELS, endpoint


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Quota:
    bucket: str
    metric: str
    limit: int
    window: str
    timezone: str = "UTC"


@dataclass(frozen=True)
class Evidence:
    value: int | None
    provenance: str
    as_of: datetime | None
    source: str | None
    scope: str | None
    valid_until: datetime | None

    def view(self) -> dict:
        return {
            "value": self.value,
            "provenance": self.provenance,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": self.source,
            "scope": self.scope,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


@dataclass(frozen=True)
class QuotaFact:
    metric: str
    window: str
    limit: Evidence
    remaining: Evidence

    def view(self) -> dict:
        return {
            "metric": self.metric,
            "window": self.window,
            "limit": self.limit.view(),
            "remaining": self.remaining.view(),
        }


@dataclass(frozen=True)
class Capacity:
    kind: str = "unknown"
    refresh_seconds: int | None = None
    as_of: datetime | None = None
    source: str | None = None
    scope: str | None = None
    expires_at: datetime | None = None

    def view(self) -> dict:
        return {
            "kind": self.kind,
            "refresh_seconds": self.refresh_seconds,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": self.source,
            "scope": self.scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class Target:
    id: str
    provider: str
    model: str
    account_id: str
    enabled: bool
    free_eligible: bool
    billing_enabled: bool
    verified_at: datetime | None
    expires_at: datetime | None
    quotas: tuple[Quota, ...]
    concurrency_limit: int
    max_output_tokens: int
    priority: int
    source: str
    shared_concurrency_scope: str | None = None
    shared_concurrency_limit: int | None = None
    quota_basis: str = "legacy_v1"
    provider_quota_facts: tuple[QuotaFact, ...] = ()
    capacity: Capacity = Capacity()
    secret_ref: str | None = None

    def available(self, now: datetime) -> bool:
        return bool(
            self.enabled
            and self.free_eligible
            and not self.billing_enabled
            and self.verified_at
            and self.expires_at
            and self.verified_at <= now < self.expires_at
            and self.quotas
            and all(
                not (
                    f.remaining.provenance == "official"
                    and f.remaining.value == 0
                    and f.remaining.as_of is not None
                    and f.remaining.as_of <= now
                    and f.remaining.valid_until is not None
                    and now < f.remaining.valid_until
                )
                for f in self.provider_quota_facts
            )
        )

    @property
    def endpoint(self) -> str:
        return endpoint(MODELS[self.model], self.account_id)


def _instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ConfigError("timestamps require timezone")
    return parsed.astimezone(UTC)


def parse_evidence(raw: dict) -> Evidence:
    if not isinstance(raw, dict) or set(raw) != {
        "value",
        "provenance",
        "as_of",
        "source",
        "scope",
        "valid_until",
    }:
        raise ConfigError("invalid quota evidence")
    value = raw["value"]
    kind = raw["provenance"]
    as_of = _instant(raw["as_of"])
    valid_until = _instant(raw["valid_until"])
    source, scope = raw["source"], raw["scope"]
    if kind not in {"official", "observed", "estimated", "unknown"}:
        raise ConfigError("invalid quota evidence provenance")
    if kind == "unknown":
        if any(item is not None for item in (value, as_of, source, scope, valid_until)):
            raise ConfigError("unknown quota evidence must have null fields")
    elif (
        type(value) is not int
        or value < 0
        or as_of is None
        or not isinstance(source, str)
        or not source
        or not isinstance(scope, str)
        or not scope
    ):
        raise ConfigError("known quota evidence needs value, time, source and scope")
    if valid_until is not None and (as_of is None or valid_until <= as_of):
        raise ConfigError("evidence validity must follow observation")
    return Evidence(value, kind, as_of, source, scope, valid_until)


def parse_quota_facts(raw: list) -> tuple[QuotaFact, ...]:
    if not isinstance(raw, list):
        raise ConfigError("provider_quota_facts must be an array")
    facts = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"metric", "window", "limit", "remaining"}:
            raise ConfigError("invalid provider quota fact")
        metric, window = item["metric"], item["window"]
        if metric not in {"requests", "input_tokens", "neurons"} or window not in {
            "rolling_minute",
            "day",
            "one_time",
            "unknown",
        }:
            raise ConfigError("invalid provider quota dimension")
        if (metric, window) in seen:
            raise ConfigError("duplicate provider quota fact")
        seen.add((metric, window))
        facts.append(
            QuotaFact(
                metric, window, parse_evidence(item["limit"]), parse_evidence(item["remaining"])
            )
        )
    return tuple(facts)


def parse_capacity(raw: dict) -> Capacity:
    if not isinstance(raw, dict) or set(raw) != {
        "kind",
        "refresh_seconds",
        "as_of",
        "source",
        "scope",
        "expires_at",
    }:
        raise ConfigError("invalid capacity evidence")
    kind = raw["kind"]
    seconds = raw["refresh_seconds"]
    as_of = _instant(raw["as_of"])
    expires = _instant(raw["expires_at"])
    source, scope = raw["source"], raw["scope"]
    if kind not in {"short_renewable", "unknown", "one_time_gift"}:
        raise ConfigError("invalid capacity kind")
    if kind == "short_renewable":
        if type(seconds) is not int or not 1 <= seconds <= 86_400:
            raise ConfigError("renewable capacity requires known short refresh")
    elif seconds is not None:
        raise ConfigError("unknown or gift capacity cannot claim refresh")
    if kind != "unknown" and (
        as_of is None
        or not isinstance(source, str)
        or not source
        or not isinstance(scope, str)
        or not scope
    ):
        raise ConfigError("known capacity requires time, source and scope")
    if kind == "unknown" and (as_of is not None or source is not None or scope is not None):
        raise ConfigError("unknown capacity cannot claim a source")
    return Capacity(kind, seconds, as_of, source, scope, expires)


def load_config(path: str | Path, *, allow_nvidia: bool = False) -> tuple[Target, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(raw) != {"targets"} or not isinstance(raw["targets"], list):
        raise ConfigError("expected targets array")
    targets = []
    ids = set()
    for item in raw["targets"]:
        try:
            model = MODELS[item["model"]]
            if item["provider"] != model.provider:
                raise ConfigError("provider/model mismatch")
            if model.provider == "nvidia" and not allow_nvidia:
                raise ConfigError("NVIDIA is available only through the authenticated executor")
            legacy = "quotas" in item
            if legacy == ("local_safety_caps" in item):
                raise ConfigError("use exactly one of quotas or local_safety_caps")
            quotas = tuple(Quota(**q) for q in item["quotas" if legacy else "local_safety_caps"])
            official_facts = () if legacy else parse_quota_facts(item["provider_quota_facts"])
            capacity = Capacity() if legacy else parse_capacity(item["capacity"])
            target = Target(
                id=item["id"],
                provider=item["provider"],
                model=item["model"],
                account_id=item["account_id"],
                enabled=item["enabled"],
                free_eligible=item["free_eligible"],
                billing_enabled=item["billing_enabled"],
                verified_at=_instant(item["verified_at"]),
                expires_at=_instant(item["expires_at"]),
                quotas=quotas,
                concurrency_limit=item["concurrency_limit"],
                max_output_tokens=item["max_output_tokens"],
                priority=item.get("priority", 0),
                source=item["source"],
                shared_concurrency_scope=item.get("shared_concurrency_scope"),
                shared_concurrency_limit=item.get("shared_concurrency_limit"),
                quota_basis="legacy_v1" if legacy else "local_safety_cap",
                provider_quota_facts=official_facts,
                capacity=capacity,
                secret_ref=item.get("secret_ref"),
            )
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"invalid target: {exc}") from exc
        if not target.id or target.id in ids:
            raise ConfigError("duplicate or empty target id")
        ids.add(target.id)
        if not target.source or target.concurrency_limit < 1:
            raise ConfigError("source and positive concurrency limit required")
        if target.secret_ref is not None and (
            not isinstance(target.secret_ref, str)
            or not target.secret_ref
            or len(target.secret_ref) > 128
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in target.secret_ref)
        ):
            raise ConfigError("invalid secret reference")
        if (target.shared_concurrency_scope is None) != (target.shared_concurrency_limit is None):
            raise ConfigError("shared concurrency scope and limit must be set together")
        if target.shared_concurrency_scope is not None and (
            not isinstance(target.shared_concurrency_scope, str)
            or not target.shared_concurrency_scope
            or type(target.shared_concurrency_limit) is not int
            or target.shared_concurrency_limit < 1
        ):
            raise ConfigError("invalid shared concurrency scope or limit")
        if not 1 <= target.max_output_tokens <= model.max_output_tokens:
            raise ConfigError("invalid max output")
        if target.expires_at and target.verified_at and target.expires_at <= target.verified_at:
            raise ConfigError("expiration must follow verification")
        _ = target.endpoint  # validate path substitutions before requests
        if not quotas or len({q.bucket for q in quotas}) != len(quotas):
            raise ConfigError("quotas required; bucket ids must be unique per target")
        for q in quotas:
            if not q.bucket or q.metric not in {"requests", "input_tokens", "neurons"}:
                raise ConfigError("unsupported quota")
            if q.limit < 1 or q.window not in {"rolling_minute", "day"}:
                raise ConfigError("invalid quota limit/window")
            try:
                ZoneInfo(q.timezone)
            except KeyError as exc:
                raise ConfigError("invalid timezone") from exc
        dimensions = {(q.metric, q.window) for q in quotas}
        if not legacy and not {("requests", "rolling_minute"), ("requests", "day")}.issubset(
            dimensions
        ):
            raise ConfigError("local safety caps require request minute and day limits")
        if legacy and target.provider == "google":
            required = {
                ("requests", "rolling_minute"),
                ("input_tokens", "rolling_minute"),
                ("requests", "day"),
            }
            if not required.issubset(dimensions):
                raise ConfigError("Google requires RPM, input TPM, and RPD")
            if any(
                q.metric == "requests" and q.window == "day" and q.timezone != "America/Los_Angeles"
                for q in quotas
            ):
                raise ConfigError("Google RPD resets at Pacific midnight")
        if legacy and target.provider == "cloudflare":
            required = {("neurons", "day"), ("requests", "rolling_minute")}
            if not required.issubset(dimensions):
                raise ConfigError("Cloudflare requires daily Neurons and request RPM")
            if any(
                q.metric == "neurons" and q.window == "day" and q.timezone != "UTC" for q in quotas
            ):
                raise ConfigError("Cloudflare daily Neurons reset at UTC midnight")
        targets.append(target)
    return tuple(targets)


def load_gateway_config(path: str | Path) -> tuple[Target, ...]:
    targets = load_config(path, allow_nvidia=True)
    if any(target.secret_ref is None for target in targets):
        raise ConfigError("gateway targets require secret_ref")
    if any(target.quota_basis != "local_safety_cap" for target in targets):
        raise ConfigError("gateway targets require local_safety_caps and provider_quota_facts")
    return targets
