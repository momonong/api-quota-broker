"""Validated public evidence and disabled candidates; never resolves credentials.

Only two fixed anonymous model-list endpoints can be fetched. Importing a
snapshot is a separate local administrator operation and performs no network IO.
"""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

from .bounded_curl import _quote, metrics_template, parse_curl_result, run_curl
from .config import Target
from .core import canonical, stamp, utcnow
from .discovery import ATTEST_FIELDS, MODEL_FIELDS, DiscoveryError
from .gateway_providers import ProviderError
from .provider_policy import ENDPOINT_HOSTS, catalog_model_id_reason
from .registry import Registry, RegistryError

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
MAX_MODELS = 10_000
MAX_ACCOUNT_VALIDITY = timedelta(days=30)
PROVIDERS = frozenset(
    {"nvidia", "google", "cloudflare", "groq", "mistral", "openrouter", "ocrspace"}
)
SOURCE_HOSTS = {
    "nvidia": {"docs.api.nvidia.com", "build.nvidia.com", "integrate.api.nvidia.com"},
    "google": {"ai.google.dev", "generativelanguage.googleapis.com"},
    "cloudflare": {"developers.cloudflare.com", "api.cloudflare.com"},
    "groq": {"console.groq.com", "api.groq.com"},
    "mistral": {"docs.mistral.ai", "api.mistral.ai"},
    "openrouter": {"openrouter.ai"},
    "ocrspace": {"ocr.space", "api.ocr.space"},
}
PUBLIC_URLS = {
    ("nvidia", "text"): "https://integrate.api.nvidia.com/v1/models",
    ("nvidia", "all"): "https://integrate.api.nvidia.com/v1/models",
    ("openrouter", "text"): "https://openrouter.ai/api/v1/models",
    ("openrouter", "all"): "https://openrouter.ai/api/v1/models?output_modalities=all",
}
_SECRET = re.compile(r"(?:gsk_|sk-[A-Za-z0-9]{12}|dp\.st\.|Bearer\s)", re.IGNORECASE)


class EvidenceError(DiscoveryError):
    def __init__(self, message: str, reason: str):
        super().__init__("invalid_request", message)
        self.phase = "validate_evidence"
        self.reason = reason


def _fail(message: str = "invalid discovery evidence") -> NoReturn:
    reasons = {
        "invalid discovery fields": "fields",
        "invalid discovery identifier": "identifier",
        "invalid discovery URL": "url",
        "invalid model token limits": "token_limits",
        "duplicate model capability": "duplicate_model",
    }
    raise EvidenceError(message, reasons.get(message, "evidence_schema"))


def _object(raw: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        _fail("invalid discovery fields")
    return raw


def _identifier(value: Any, *, model: bool = False) -> str:
    pattern = r"[A-Za-z0-9_@][A-Za-z0-9._~/@:+-]{0,255}" if model else r"[a-z][a-z0-9_:-]{0,79}"
    if (
        not isinstance(value, str)
        or not re.fullmatch(pattern, value)
        or ".." in value
        or "//" in value
        or _SECRET.search(value)
    ):
        _fail("invalid discovery identifier")
    return value


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in PROVIDERS:
        _fail("unrecognized discovery provider")
    return value


def _model_id(value: Any) -> str:
    reason = catalog_model_id_reason(value)
    if reason is not None:
        raise EvidenceError("invalid discovery model id", "model_id_" + reason)
    assert isinstance(value, str)
    return value


def _instant(value: Any, now: datetime, *, future: bool = False) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        _fail("invalid discovery timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        _fail("invalid discovery timestamp")
    if result.tzinfo is None or (not future and result > now):
        _fail("discovery timestamps must be aware and not in the future")
    return result.astimezone(UTC)


def _url(value: Any, provider: str, *, endpoint: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 1024 or _SECRET.search(value):
        _fail("invalid official discovery URL")
    try:
        parsed = urlsplit(value)
        hosts = ENDPOINT_HOSTS if endpoint else SOURCE_HOSTS
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts[provider]
            or parsed.netloc != parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/")
            or not re.fullmatch(r"/[A-Za-z0-9_/@.:{}+-]*", parsed.path)
            or ".." in parsed.path
            or "//" in parsed.path
        ):
            _fail("discovery URL is outside the official host policy")
        # The only permitted source query selects all documented output types.
        if parsed.query and not (
            not endpoint
            and value == PUBLIC_URLS[("openrouter", "all")]
            and provider == "openrouter"
        ):
            _fail("discovery URL query is not permitted")
    except ValueError:
        _fail("invalid official discovery URL")
    return value


def _tokens(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 100_000_000:
        _fail("invalid model token limit")
    return value


def validate_snapshot(raw: dict[str, Any], now: datetime) -> dict[str, Any]:
    data = _object(
        raw, {"schema_version", "provider", "source", "checked_at", "complete", "models"}
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("unsupported discovery schema")
    provider = _provider(data["provider"])
    source = _url(data["source"], provider)
    checked = _instant(data["checked_at"], now)
    if type(data["complete"]) is not bool:
        _fail("invalid discovery completeness")
    rows = data["models"]
    if not isinstance(rows, list) or len(rows) > MAX_MODELS or (data["complete"] and not rows):
        _fail("invalid discovery model count")
    normalized = []
    seen: set[tuple[str, str]] = set()
    for raw_row in rows:
        row = _object(raw_row, set(MODEL_FIELDS))
        model, capability = _model_id(row["model"]), _identifier(row["capability"])
        if (model, capability) in seen:
            _fail("duplicate discovery identity")
        seen.add((model, capability))
        if not isinstance(row["hosting"], str) or row["hosting"] not in {
            "hosted",
            "selfhost",
            "both",
            "unknown",
        }:
            _fail("invalid model hosting status")
        if not isinstance(row["free_eligibility"], str) or row["free_eligibility"] not in {
            "unknown",
            "free",
            "paid",
            "restricted",
        }:
            _fail("invalid model free eligibility")
        if not isinstance(row["status"], str) or row["status"] not in {"listed", "retired"}:
            _fail("invalid model listing status")
        endpoint = (
            _url(row["endpoint"], provider, endpoint=True) if row["endpoint"] is not None else None
        )
        protocol = _identifier(row["protocol"]) if row["protocol"] is not None else None
        free_source = _url(row["free_source"], provider) if row["free_source"] is not None else None
        if row["free_eligibility"] != "unknown" and free_source is None:
            _fail("free eligibility requires an official evidence source")
        context, output = _tokens(row["context_tokens"]), _tokens(row["max_output_tokens"])
        if context is not None and output is not None and output > context:
            _fail("model output limit exceeds its context")
        features = row["features"]
        if not isinstance(features, list) or len(features) > 64:
            _fail("invalid discovery features")
        normalized.append(
            {
                "model": model,
                "capability": capability,
                "hosting": row["hosting"],
                "endpoint": endpoint,
                "protocol": protocol,
                "free_eligibility": row["free_eligibility"],
                "free_source": free_source,
                "status": row["status"],
                "context_tokens": context,
                "max_output_tokens": output,
                "features": sorted({_identifier(item) for item in features}),
            }
        )
    result = {
        "schema_version": 1,
        "provider": provider,
        "source": source,
        "checked_at": stamp(checked),
        "complete": data["complete"],
        "models": normalized,
    }
    if len(canonical(result).encode()) > MAX_SNAPSHOT_BYTES:
        _fail("discovery snapshot exceeds the byte limit")
    return result


def validate_attestation(raw: dict[str, Any], now: datetime) -> dict[str, Any]:
    data = dict(_object(raw, set(ATTEST_FIELDS)))
    data["provider"] = _provider(data["provider"])
    data["model"] = _model_id(data["model"])
    data["capability"] = _identifier(data["capability"])
    data["account_scope"] = _identifier(data["account_scope"], model=True)
    if not isinstance(data["account_availability"], str) or data["account_availability"] not in {
        "unknown",
        "allowed",
        "blocked",
    }:
        _fail("invalid account availability")
    checked = _instant(data["account_checked_at"], now)
    until = _instant(data["account_valid_until"], now, future=True)
    if not checked < until <= checked + MAX_ACCOUNT_VALIDITY:
        _fail("account evidence requires a bounded validity period")
    if not isinstance(data["live_result"], str) or data["live_result"] not in {
        "unverified",
        "passed",
        "failed",
        "unknown",
    }:
        _fail("invalid live result")
    live_at = None if data["live_checked_at"] is None else _instant(data["live_checked_at"], now)
    if (data["live_result"] == "unverified") != (live_at is None):
        _fail("live result requires its own observation timestamp")
    if data["receipt_id"] is not None:
        data["receipt_id"] = _identifier(data["receipt_id"], model=True)
    if data["live_result"] != "unverified" and data["receipt_id"] is None:
        _fail("live evidence requires a nonsecret receipt reference")
    data.update(
        account_checked_at=stamp(checked),
        account_valid_until=stamp(until),
        live_checked_at=stamp(live_at) if live_at else None,
    )
    return data


def fetch_public_snapshot(
    provider: str, *, output_modalities: str = "text", clock: Callable[[], datetime] = utcnow
) -> dict[str, Any]:
    """Explicit anonymous fixed GET, 20s/5MiB, no retries or redirect following."""
    url = PUBLIC_URLS.get((provider, output_modalities))
    if url is None:
        _fail("anonymous model discovery is unavailable for this provider or modality")
    options = [
        "url = " + _quote(url),
        'request = "GET"',
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        'connect-timeout = "10"',
        'max-time = "20"',
        "include",
        "suppress-connect-headers",
        'header = "Accept: application/json"',
        "write-out = " + _quote(metrics_template()),
    ]
    try:
        code, output = run_curl(
            ("\n".join(options) + "\n").encode(),
            20,
            output_bound=MAX_SNAPSHOT_BYTES + 16_384,
            deadline_grace=0,
        )
        status, _, body, timing = parse_curl_result(
            code, output, 20, response_bound=MAX_SNAPSHOT_BYTES
        )
        if status != 200 or timing["transport_code"] != "ok":
            _fail("public model discovery did not return an accepted response")
        raw = json.loads(body)
    except (OSError, ValueError, ProviderError):
        # Discard URL/error text and raw provider body; never persist it.
        raise DiscoveryError("discovery_fetch_failed", "public discovery failed") from None
    from .discovery_parsers import parse_models

    checked_at = clock()
    return validate_snapshot(
        parse_models(provider, raw, checked_at, output_modalities=output_modalities), checked_at
    )


def candidate_bundle(
    records: list[dict[str, Any]], registry: Registry, targets: tuple[Target, ...] = ()
) -> dict[str, Any]:
    """Reviewable, disabled skeletons only. Does not bind or copy account quotas."""
    candidates: list[dict[str, Any]] = []
    proposals: dict[tuple[str, str], dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for row in records:
        provider, model, capability = row["provider"], row["model"], row["capability"]
        identity = {"provider": provider, "model": model, "capability": capability}
        reasons: list[str] = []
        registered = registry.models.get((provider, model))
        if row["status"] != "listed":
            reasons.append("current_listing_missing")
        if row["free_eligibility"] != "free":
            reasons.append("free_eligibility_unconfirmed")
        if row["account_availability"] != "allowed":
            reasons.append("account_access_unconfirmed")
        if registered is None:
            if row["adapter_support"] != "compatible_unregistered":
                reasons.append("registry_contract_incomplete")
            else:
                try:
                    declaration, proposal = registry.candidate_definition(row)
                except RegistryError:
                    reasons.append("registry_contract_incompatible")
                else:
                    if (
                        provider in providers
                        and providers[provider] != declaration
                        and not (registry.fixed_family_adapter(declaration["adapter"]))
                    ):
                        reasons.append("provider_endpoint_conflict")
                    else:
                        providers.setdefault(provider, declaration)
                        prior = proposals.get((provider, model))
                        if prior is None:
                            proposal["capabilities"] = [capability]
                            proposals[(provider, model)] = proposal
                        else:
                            prior["capabilities"] = sorted(
                                set(prior["capabilities"]) | {capability}
                            )
                            for limit in ("context_tokens", "max_output_tokens"):
                                applicable = [
                                    value for value in (prior[limit], proposal[limit]) if value > 0
                                ]
                                prior[limit] = min(applicable) if applicable else 0
                            prior["features"] = sorted(
                                set(prior["features"]) | set(proposal["features"])
                            )
                            if prior["adapter"] != proposal["adapter"]:
                                prior.setdefault("family_adapters", {})[capability] = proposal[
                                    "adapter"
                                ]
        elif not registry.supports_family(registered, capability):
            reasons.append("registry_family_mismatch")
        reference = hashlib.sha256(canonical(identity).encode()).hexdigest()[:16]
        candidates.append(
            {
                "id": "candidate-" + reference,
                **identity,
                "disabled": True,
                "free_eligible": False,
                "billing_enabled": False,
                "activation_allowed": False,
                "missing_bindings": [
                    "account_scope",
                    "secret_ref",
                    "quota_scope",
                    "local_caps",
                    "current_free_attestation",
                ],
                "observed_free_eligibility": row["free_eligibility"],
                "observed_account_availability": row["account_availability"],
                "configured_target_ids": list(row.get("configured_target_ids", [])),
                "blocking_reasons": reasons,
            }
        )
        if reasons:
            gaps.append({**identity, "reasons": reasons})
    return {
        "schema_version": 1,
        "activation_allowed": False,
        "registry_manifest": {
            "schema_version": 1,
            "providers": list(providers.values()),
            "models": list(proposals.values()),
        },
        "target_candidates": candidates,
        "gaps": gaps,
        "missing_bindings_required": True,
    }
