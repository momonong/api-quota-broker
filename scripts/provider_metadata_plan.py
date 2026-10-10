"""Print a reviewable GET plan or normalize an explicitly supplied JSON fixture.

This program has no execution mode, network client, credential resolver, env
inspection, or account lookup. A plan never authorizes its future execution.
"""

import argparse
import json
import re
import stat
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from quota_broker.discovery import DiscoveryError
from quota_broker.discovery_parsers import SOURCES, parse_models
from quota_broker.discovery_sources import MAX_SNAPSHOT_BYTES, validate_snapshot

PROVIDERS = tuple(SOURCES)
KEY_URL = "https://openrouter.ai/api/v1/key"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search"
SECRET_PATTERN = re.compile(
    r"(?:gsk_|sk-[A-Za-z0-9_-]{12}|dp\.st\.|Bearer\s|AIza[A-Za-z0-9_-]{12}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class PlanError(DiscoveryError):
    phase = "metadata_plan"

    def __init__(self, reason: str):
        super().__init__("invalid_request", "invalid metadata plan input")
        self.reason = (
            reason
            if reason
            in {
                "options",
                "fixture_path",
                "fixture_size",
                "fixture_schema",
                "secret_pattern",
                "timestamp",
                "key_schema",
                "quota_counter",
                "credits",
            }
            else "fixture_schema"
        )


def _request(
    purpose: str,
    url: str,
    credential: str | None = None,
    *,
    pages: int = 1,
    header: str = "Authorization",
    scheme: str = "Bearer",
) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "method": "GET",
        "url": url,
        "max_pages": pages,
        "max_requests": pages,
        "max_wall_seconds": pages * 20,
        "authentication": None
        if credential is None
        else {
            "credential_ref": credential,
            "header": header,
            "scheme": scheme,
        },
    }


def build_plan() -> dict[str, Any]:
    """Static names are declared in gateway.example.json, never resolved here."""
    google = _request(
        "models",
        SOURCES["google"] + "?pageSize=1000",
        "GEMINI_API_KEY",
        pages=5,
        header="x-goog-api-key",
        scheme="api_key",
    )
    google["pagination"] = {
        "request_parameter": "pageToken",
        "response_field": "nextPageToken",
        "token_storage": "memory_only",
        "tokens_in_output": False,
        "stop": "missing_next_page_token_or_five_pages",
        "complete_at_page_cap": False,
    }
    cloudflare = _request(
        "models",
        CF_URL + "?page=1&per_page=100&include_deprecated=true",
        "CLOUDFLARE_API_TOKEN",
        pages=5,
    )
    cloudflare["account_id_ref"] = "CLOUDFLARE_ACCOUNT_ID"
    cloudflare["pagination"] = {
        "request_parameter": "page",
        "first_page": 1,
        "last_page": 5,
        "per_page": 100,
        "per_page_is_client_choice": True,
        "server_max_per_page": None,
        "complete_requires_exhaustion_evidence": True,
        "complete_at_page_cap": False,
    }
    providers = [
        {
            "provider": "nvidia",
            "requests": [_request("models", SOURCES["nvidia"])],
            "gap": "model_list_does_not_prove_capability_or_free_access",
        },
        {
            "provider": "groq",
            "requests": [_request("models", SOURCES["groq"], "GROQ_API_KEY")],
            "gap": "model_list_does_not_prove_task_protocol_or_free_access",
        },
        {
            "provider": "mistral",
            "requests": [_request("models", SOURCES["mistral"], "MISTRAL_API_KEY")],
            "gap": "account_list_does_not_prove_free_eligibility",
        },
        {
            "provider": "google",
            "requests": [google],
            "gap": "five_page_cap_can_leave_listing_partial_and_free_access_unknown",
        },
        {
            "provider": "cloudflare",
            "requests": [cloudflare],
            "gap": "official_result_item_schema_and_exhaustion_need_validation",
        },
        {
            "provider": "openrouter",
            "requests": [
                _request("models", SOURCES["openrouter"] + "?output_modalities=all"),
                _request("current_key_metadata", KEY_URL, "OPENROUTER_API_KEY"),
            ],
            "gap": "daily_request_counter_is_separate_from_credits_and_account_access",
        },
        {
            "provider": "ocrspace",
            "requests": [],
            "docs": SOURCES["ocrspace"],
            "gap": "docs_only_no_approved_get_model_or_account_metadata_api",
        },
    ]
    return {
        "schema_version": 1,
        "mode": "dry_run",
        "execution_authorized": False,
        "credential_inventory": "declared_names_only_not_presence_checked",
        "declared_credential_refs": {
            "nvidia": "NVIDIA_API_KEY",
            "groq": "GROQ_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "google": "GEMINI_API_KEY",
            "cloudflare": "CLOUDFLARE_API_TOKEN",
            "openrouter": "OPENROUTER_API_KEY",
            "ocrspace": "OCRSPACE_API_KEY",
        },
        "max_requests": 15,
        "max_wall_seconds": 240,
        "theoretical_page_ceiling_seconds": 300,
        "expiry_reserve_seconds": 30,
        "page_cap_can_end_partial": True,
        "serial": True,
        "future_credential_steps": {
            "mechanism": "existing_doppler_service_token_and_resolver",
            "project": "api-quota-broker",
            "config": "dev",
            "access": "read_only_config",
            "token_ttl_seconds": 300,
            "token_count": 1,
            "renew": False,
            "minimum_remaining_seconds_before_batch": 270,
            "insufficient_token_lifetime": "stop_before_requests",
            "resolution": "cache_each_required_name_once",
            "secret_channels": ["memory", "stdin"],
            "persist_secrets": False,
            "output_secrets": False,
            "account_id_channels": ["memory"],
            "output_account_id": False,
            "account_metadata_scope": "opaque_local_profile_no_account_id",
            "executed_by_this_tool": False,
        },
        "future_output_allowlist": {
            "models": [
                "model",
                "capability",
                "hosting",
                "endpoint",
                "protocol",
                "free_eligibility",
                "free_source",
                "status",
                "context_tokens",
                "max_output_tokens",
                "features",
            ],
            "evidence": [
                "UTC_checked_at",
                "official_source",
                "complete",
                "fixed_phase",
                "fixed_reason",
                "request_count",
                "validated_identity",
            ],
            "account": ["opaque_scope", "free_model_daily_requests", "credits"],
            "raw_response": False,
            "description": False,
            "next_page_token": False,
            "account_id": False,
            "freeform_error": False,
        },
        "future_transport_contract": {
            "implementation": "quota_broker.bounded_curl.run_curl",
            "per_request_total_seconds": 20,
            "deadline_grace": 0,
            "max_response_bytes": MAX_SNAPSHOT_BYTES,
            "retry_count": 0,
            "follow_redirects": False,
            "custom_user_agent": False,
            "custom_ip": False,
            "custom_proxy": False,
            "tls_verification": True,
            "raw_response_storage": "memory_only",
            "stop_on_transport_or_validation_error": True,
        },
        "sources": {
            "nvidia": "https://docs.api.nvidia.com/nim/reference/models-1",
            "groq": "https://console.groq.com/docs/api-reference",
            "mistral": "https://docs.mistral.ai/api/endpoint/models",
            "google": "https://ai.google.dev/api/models",
            "cloudflare": "https://developers.cloudflare.com/api/resources/ai/subresources/models/methods/list/",
            "openrouter_models": "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties",
            "openrouter_key": "https://openrouter.ai/docs/api_reference/limits",
            "ocrspace": SOURCES["ocrspace"],
        },
        "providers": providers,
    }


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key.lower() in {
            "api_key",
            "key",
            "token",
            "secret",
            "password",
            "authorization",
            "access_token",
            "private_key",
        }:
            raise PlanError("secret_pattern")
        if key in result:
            raise PlanError("fixture_schema")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    raise PlanError("fixture_schema")


def read_fixture(path: Path) -> dict[str, Any]:
    """Only an explicit bounded JSON fixture, never implicit config/key files."""
    if (
        path.suffix.lower() != ".json"
        or any(part.lower() in {".ssh", ".aws", ".codex", ".state"} for part in path.parts)
        or re.search(r"(?:secret|credential|doppler|gateway)", path.name, re.IGNORECASE)
    ):
        raise PlanError("fixture_path")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PlanError("fixture_path")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise PlanError("fixture_size")
        with path.open("rb") as handle:
            data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise PlanError("fixture_size")
        text = data.decode("utf-8")
        if SECRET_PATTERN.search(text):
            raise PlanError("secret_pattern")
        raw = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if SECRET_PATTERN.search(json.dumps(raw)):
            raise PlanError("secret_pattern")
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError("fixture_schema") from None
    if not isinstance(raw, dict):
        raise PlanError("fixture_schema")
    return raw


def _credits(value: object) -> str | None:
    if value is None:
        return None
    if type(value) not in {int, float, str}:
        raise PlanError("credits")
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise PlanError("credits")
    except InvalidOperation:
        raise PlanError("credits") from None
    return str(parsed)


def parse_openrouter_key(raw: dict[str, Any], checked_at: datetime) -> dict[str, Any]:
    """Official /key numeric allowlist, discarding labels, IDs, keys and messages.

    The daily counter describes tier policy, not proof that a request is allowed
    or that the limit applies to an exempt/BYOK account. Credits are never used
    as request counts or inferred from is_free_tier.
    """
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise PlanError("timestamp")
    data = raw.get("data")
    if not isinstance(data, dict) or raw.get("error"):
        raise PlanError("key_schema")
    daily = data.get("free_model_daily_requests")
    normalized_daily = None
    if daily is not None:
        if not isinstance(daily, dict) or set(daily) != {"used", "limit", "remaining"}:
            raise PlanError("quota_counter")
        if any(type(value) is not int or not 0 <= value <= 10**9 for value in daily.values()):
            raise PlanError("quota_counter")
        if daily["remaining"] != max(0, daily["limit"] - daily["used"]):
            raise PlanError("quota_counter")
        normalized_daily = {name: daily[name] for name in ("used", "limit", "remaining")}
    credits = {
        name: _credits(data.get(name))
        for name in (
            "limit",
            "limit_remaining",
            "usage",
            "usage_daily",
            "usage_weekly",
            "usage_monthly",
            "byok_usage",
            "byok_usage_daily",
            "byok_usage_weekly",
            "byok_usage_monthly",
        )
    }
    return {
        "schema_version": 1,
        "provider": "openrouter",
        "source": KEY_URL,
        "checked_at": checked_at.astimezone(UTC).isoformat(),
        "free_model_daily_requests": normalized_daily,
        "credits": credits,
        "account_availability": "unknown",
        "live_result": "unknown",
        "daily_counter_semantics": "reported_tier_policy_not_admission",
    }


def normalize_fixture(
    provider: str,
    path: Path,
    checked_at: datetime,
    *,
    kind: str = "models",
) -> dict[str, Any]:
    if provider not in PROVIDERS or kind not in {"models", "key"}:
        raise PlanError("options")
    if kind == "key" and provider != "openrouter":
        raise PlanError("options")
    raw = read_fixture(path)
    if kind == "key":
        return parse_openrouter_key(raw, checked_at)
    # All saved model evidence passes exactly the production snapshot policy.
    return validate_snapshot(
        parse_models(provider, raw, checked_at, output_modalities="all"), checked_at
    )


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default error reflects argv, including arbitrary path text.
        print(
            json.dumps({"error": "invalid_request", "phase": "metadata_plan", "reason": "options"})
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--fixture", nargs=2, metavar=("PROVIDER", "PATH"))
    parser.add_argument("--fixture-kind", choices=("models", "key"), default="models")
    parser.add_argument(
        "--checked-at", help="Aware ISO timestamp for a local fixture; default now UTC"
    )
    args = parser.parse_args(argv)
    try:
        if args.fixture is None:
            if args.checked_at is not None or args.fixture_kind != "models":
                raise PlanError("options")
            result = build_plan()
        else:
            try:
                instant = (
                    datetime.fromisoformat(args.checked_at)
                    if args.checked_at
                    else datetime.now(UTC)
                )
            except (ValueError, TypeError):
                raise PlanError("timestamp") from None
            if instant.tzinfo is None or instant.utcoffset() is None or instant > datetime.now(UTC):
                raise PlanError("timestamp")
            result = normalize_fixture(
                args.fixture[0], Path(args.fixture[1]), instant, kind=args.fixture_kind
            )
    except DiscoveryError as exc:
        print(
            json.dumps(
                {
                    "error": "invalid_request",
                    "phase": getattr(exc, "phase", "validate_snapshot"),
                    "reason": getattr(exc, "reason", "snapshot_policy"),
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
