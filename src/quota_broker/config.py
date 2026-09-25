"""Human-confirmed account facts. Unknown or stale facts never admit traffic."""

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

    def available(self, now: datetime) -> bool:
        return bool(
            self.enabled
            and self.free_eligible
            and not self.billing_enabled
            and self.verified_at
            and self.expires_at
            and self.verified_at <= now < self.expires_at
            and self.quotas
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


def load_config(path: str | Path) -> tuple[Target, ...]:
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
            quotas = tuple(Quota(**q) for q in item["quotas"])
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
            )
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"invalid target: {exc}") from exc
        if not target.id or target.id in ids:
            raise ConfigError("duplicate or empty target id")
        ids.add(target.id)
        if not target.source or target.concurrency_limit < 1:
            raise ConfigError("source and positive concurrency limit required")
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
        if target.provider == "google":
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
        if target.provider == "cloudflare":
            required = {("neurons", "day"), ("requests", "rolling_minute")}
            if not required.issubset(dimensions):
                raise ConfigError("Cloudflare requires daily Neurons and request RPM")
            if any(
                q.metric == "neurons" and q.window == "day" and q.timezone != "UTC" for q in quotas
            ):
                raise ConfigError("Cloudflare daily Neurons reset at UTC midnight")
        targets.append(target)
    return tuple(targets)
