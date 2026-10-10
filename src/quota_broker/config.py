"""Account eligibility, provider evidence, and distinct local admission caps."""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .catalog import MODELS, Model, endpoint

if TYPE_CHECKING:
    from .registry import ModelSpec, Registry


RESOURCE_METRICS = frozenset(
    {
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "audio_seconds",
        "images",
        "pages",
        "conversions",
        "neurons",
    }
)
QUOTA_WINDOWS = frozenset({"rolling_minute", "rolling_hour", "day", "month"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SecretInventory:
    """Expiring, scope-bound names only; never resolves or validates secret values."""

    names: frozenset[str]
    verified_at: datetime
    expires_at: datetime
    project: str
    config: str

    def current(self, now: datetime) -> bool:
        return self.verified_at <= now < self.expires_at


def load_secret_inventory(path: str | Path, project: str, config: str) -> SecretInventory:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {
        "names",
        "verified_at",
        "expires_at",
        "project",
        "config",
    }:
        raise ConfigError("invalid secret name inventory")
    names = raw["names"]
    if (
        not isinstance(names, list)
        or len(names) > 256
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            for name in names
        )
        or len(set(names)) != len(names)
        or raw["project"] != project
        or raw["config"] != config
    ):
        raise ConfigError("invalid or differently scoped secret name inventory")
    try:
        verified, expires = _instant(raw["verified_at"]), _instant(raw["expires_at"])
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid inventory validity") from exc
    if verified is None or expires is None or expires <= verified:
        raise ConfigError("inventory requires a bounded validity interval")
    return SecretInventory(frozenset(names), verified, expires, project, config)


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
class NeuronEstimate:
    amount: int
    max_input_tokens: int
    max_output_tokens: int
    source: str
    verified_at: datetime
    expires_at: datetime

    def view(self) -> dict:
        return {
            "basis": "estimated_per_request_upper_bound",
            "amount": self.amount,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "source": self.source,
            "verified_at": self.verified_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class ResourceEstimate:
    metric: str
    amount: int
    max_input_bytes: int
    max_output_tokens: int
    source: str
    verified_at: datetime
    expires_at: datetime

    def view(self) -> dict:
        return {
            "metric": self.metric,
            "amount": self.amount,
            "max_input_bytes": self.max_input_bytes,
            "max_output_tokens": self.max_output_tokens,
            "source": self.source,
            "verified_at": self.verified_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
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
    neuron_estimate: NeuronEstimate | None = None
    resource_estimates: tuple[ResourceEstimate, ...] = ()
    capacity: Capacity = Capacity()
    secret_ref: str | None = None
    account_id_ref: str | None = None
    model_info: Model | None = field(default=None, compare=False, repr=False)

    def neurons(
        self, input_bound: int, output: int, now: datetime, explicit: int | None = None
    ) -> int | None:
        estimate = self.neuron_estimate
        if (
            estimate
            and estimate.verified_at <= now < estimate.expires_at
            and input_bound <= estimate.max_input_tokens
            and output <= estimate.max_output_tokens
        ):
            return max(estimate.amount, explicit or 0)
        return explicit

    def resource_costs(
        self, bounds: dict[str, int], input_bytes: int, output: int, now: datetime
    ) -> dict[str, int]:
        if (
            not isinstance(bounds, dict)
            or set(bounds) - RESOURCE_METRICS
            or any(type(value) is not int or not 0 <= value <= 10**12 for value in bounds.values())
            or type(input_bytes) is not int
            or input_bytes < 0
            or type(output) is not int
            or output < 0
        ):
            raise ConfigError("invalid trusted resource bounds")
        result = dict(bounds)
        for estimate in self.resource_estimates:
            if (
                estimate.verified_at <= now < estimate.expires_at
                and input_bytes <= estimate.max_input_bytes
                and output <= estimate.max_output_tokens
            ):
                result[estimate.metric] = max(result.get(estimate.metric, 0), estimate.amount)
        return result

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
        return endpoint(self.model_info or MODELS[self.model], self.account_id)


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
        if metric not in RESOURCE_METRICS or window not in {
            "rolling_minute",
            "rolling_hour",
            "day",
            "month",
            "one_time",
            "unknown",
        }:
            raise ConfigError("invalid provider quota dimension")
        if (metric, window) in seen:
            raise ConfigError("duplicate provider quota fact")
        seen.add((metric, window))
        fact = QuotaFact(
            metric, window, parse_evidence(item["limit"]), parse_evidence(item["remaining"])
        )
        if (
            fact.limit.value is not None
            and fact.remaining.value is not None
            and fact.remaining.value > fact.limit.value
        ):
            raise ConfigError("contradictory provider quota evidence")
        facts.append(fact)
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


CF_NEURON_FORMULA = "cloudflare_llama_3_2_1b_formula"


def cloudflare_neuron_upper_bound(input_tokens: int, output_tokens: int) -> int:
    """Ceil the published per-million coefficients; never label this actual usage."""
    if any(
        type(value) is not int or not 0 <= value <= 10**12
        for value in (input_tokens, output_tokens)
    ):
        raise ConfigError("invalid Neurons formula bounds")
    return max(1, (input_tokens * 2457 + output_tokens * 18252 + 999999) // 1000000)


def parse_neuron_estimate(raw: object) -> NeuronEstimate | None:
    if raw is None:
        return None
    fields = {
        "amount",
        "max_input_tokens",
        "max_output_tokens",
        "source",
        "verified_at",
        "expires_at",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ConfigError("invalid Neurons estimate fields")
    if any(
        type(raw[name]) is not int or not 1 <= raw[name] <= 10**12
        for name in ("amount", "max_input_tokens", "max_output_tokens")
    ) or raw["source"] not in {"trusted_operator", CF_NEURON_FORMULA}:
        raise ConfigError("invalid Neurons estimate bounds or source")
    verified, expires = _instant(raw["verified_at"]), _instant(raw["expires_at"])
    if verified is None or expires is None or expires <= verified:
        raise ConfigError("Neurons estimate requires a validity interval")
    return NeuronEstimate(
        raw["amount"],
        raw["max_input_tokens"],
        raw["max_output_tokens"],
        raw["source"],
        verified,
        expires,
    )


def parse_resource_estimates(raw: object) -> tuple[ResourceEstimate, ...]:
    if not isinstance(raw, list) or len(raw) > len(RESOURCE_METRICS):
        raise ConfigError("resource estimates must be a bounded array")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "metric",
            "amount",
            "max_input_bytes",
            "max_output_tokens",
            "source",
            "verified_at",
            "expires_at",
        }:
            raise ConfigError("invalid resource estimate fields")
        metric = item["metric"]
        if not isinstance(metric, str) or metric not in RESOURCE_METRICS or metric in seen:
            raise ConfigError("invalid or duplicate resource estimate metric")
        if (
            any(
                type(item[name]) is not int or not 1 <= item[name] <= 10**12
                for name in ("amount", "max_input_bytes")
            )
            or type(item["max_output_tokens"]) is not int
            or not 0 <= item["max_output_tokens"] <= 10**12
            or item["source"] != "trusted_operator"
        ):
            raise ConfigError("invalid resource estimate bounds or source")
        verified, expires = _instant(item["verified_at"]), _instant(item["expires_at"])
        if verified is None or expires is None or expires <= verified:
            raise ConfigError("resource estimate requires a validity interval")
        seen.add(metric)
        result.append(
            ResourceEstimate(
                metric,
                item["amount"],
                item["max_input_bytes"],
                item["max_output_tokens"],
                item["source"],
                verified,
                expires,
            )
        )
    return tuple(result)


def model_has_no_output_tokens(registry: "Registry", model: "ModelSpec") -> bool:
    from .families import FamilyError, FamilyRegistry

    families = FamilyRegistry.builtin()
    try:
        return all(
            registry.supports_family(model, capability)
            and not families.uses_output_tokens(capability)
            for capability in (model.capabilities or (model.capability,))
        )
    except FamilyError:
        return False


def load_config(
    path: str | Path, *, allow_nvidia: bool = False, registry: "Registry | None" = None
) -> tuple[Target, ...]:
    from .registry import Registry, RegistryError

    registry = registry or Registry.builtin()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(raw) != {"targets"} or not isinstance(raw["targets"], list):
        raise ConfigError("expected targets array")
    targets = []
    ids = set()
    for item in raw["targets"]:
        try:
            model = registry.resolve(item["model"], item["provider"])
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
                account_id_ref=item.get("account_id_ref"),
                model_info=model,
                neuron_estimate=parse_neuron_estimate(item.get("neuron_estimate")),
                resource_estimates=parse_resource_estimates(item.get("resource_estimates", [])),
            )
        except (KeyError, TypeError, RegistryError) as exc:
            raise ConfigError(f"invalid target: {exc}") from exc
        if target.neuron_estimate is not None and not any(
            q.metric == "neurons" for q in target.quotas
        ):
            raise ConfigError("Neurons estimate requires a Neurons cap")
        estimate = target.neuron_estimate
        if (
            estimate is not None
            and estimate.source == CF_NEURON_FORMULA
            and (
                target.provider != "cloudflare"
                or target.model != "@cf/meta/llama-3.2-1b-instruct"
                or estimate.amount
                < cloudflare_neuron_upper_bound(
                    estimate.max_input_tokens, estimate.max_output_tokens
                )
            )
        ):
            raise ConfigError("Neurons formula requires the exact model and a conservative bound")
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
        if target.account_id_ref is not None and (
            target.provider != "cloudflare"
            or not isinstance(target.account_id_ref, str)
            or not target.account_id_ref
            or len(target.account_id_ref) > 128
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in target.account_id_ref)
        ):
            raise ConfigError("invalid account ID reference")
        if (target.shared_concurrency_scope is None) != (target.shared_concurrency_limit is None):
            raise ConfigError("shared concurrency scope and limit must be set together")
        if target.shared_concurrency_scope is not None and (
            not isinstance(target.shared_concurrency_scope, str)
            or not target.shared_concurrency_scope
            or type(target.shared_concurrency_limit) is not int
            or target.shared_concurrency_limit < 1
        ):
            raise ConfigError("invalid shared concurrency scope or limit")
        output_minimum = (
            0 if model.max_output_tokens == 0 and model_has_no_output_tokens(registry, model) else 1
        )
        if (
            type(target.max_output_tokens) is not int
            or not output_minimum <= target.max_output_tokens <= model.max_output_tokens
        ):
            raise ConfigError("invalid max output")
        if target.expires_at and target.verified_at and target.expires_at <= target.verified_at:
            raise ConfigError("expiration must follow verification")
        _ = target.endpoint  # validate path substitutions before requests
        if not quotas or len({q.bucket for q in quotas}) != len(quotas):
            raise ConfigError("quotas required; bucket ids must be unique per target")
        for q in quotas:
            if not q.bucket or q.metric not in RESOURCE_METRICS:
                raise ConfigError("unsupported quota")
            if type(q.limit) is not int or q.limit < 1 or q.window not in QUOTA_WINDOWS:
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


def load_gateway_config(
    path: str | Path, *, registry: "Registry | None" = None
) -> tuple[Target, ...]:
    targets = load_config(path, allow_nvidia=True, registry=registry)
    if any(target.secret_ref is None for target in targets):
        raise ConfigError("gateway targets require secret_ref")
    if any(target.quota_basis != "local_safety_cap" for target in targets):
        raise ConfigError("gateway targets require local_safety_caps and provider_quota_facts")
    return targets
