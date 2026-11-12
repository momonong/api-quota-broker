"""Offline shared-instance configuration from explicit, timestamped evidence.

This builder never fetches secrets, observes an account, enables billing, or
relabels a historical balance as current. The conservative credit allowance
deducts the original independent probe's full reserved bound, not its guessed
actual charge. Account monitoring retains the owner's unmodified observation.
"""

import copy
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

OPENAI_MODEL = "gpt-6-luna"
OR_MODEL = "liquid/lfm-2.5-2.6b:free"
OPENAI_ID, OR_ID = "openai-personal-luna", "openrouter-personal-liquid"
CREDIT_METRIC = "credits_usd_micros"
INPUT_BYTES, OUTPUT_TOKENS, CREDIT_BOUND = 4096, 512, 3000


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp timezone required")
    return result.astimezone(UTC)


def build(
    base_registry, base_gateway, owner, openrouter, now, *, existing_eligibility_deadline=None
):
    """Return config and a non-secret derivation report; do not write files."""
    if now.tzinfo is None:
        raise ValueError("clock timezone required")
    observed = instant(owner["received_at"])
    valid = observed + timedelta(hours=24)
    if not observed <= now < valid:
        raise ValueError("owner observation is not current")
    if (
        owner["provider"] != "openai"
        or owner["source_type"] != "account_owner_pasted_billing_page"
        or owner["billing_state"] != "not_started_no_payment_method"
        or owner["plan_label"] != "Free trial"
        or owner["recurrence"] != "unknown"
        or owner["expires"]["precision"] != "date"
    ):
        raise ValueError("unsupported account evidence")
    # Earliest possible UTC instant for a date with an undisclosed timezone.
    deadline = instant(owner["expires"]["value"] + "T00:00:00+14:00")
    valid = min(valid, deadline)
    if now >= valid:
        raise ValueError("credit grant expired")
    balance = Decimal(owner["balance_usd"]) * 1_000_000
    if balance != balance.to_integral_value() or not 1000 < balance <= 10**12:
        raise ValueError("invalid account balance")
    checked = instant(openrouter["checked_at"])
    if not checked <= now < checked + timedelta(minutes=15):
        raise ValueError("OpenRouter observation is not current")
    router_eligibility_end = checked + timedelta(days=30)
    if existing_eligibility_deadline is not None:
        existing = (
            instant(existing_eligibility_deadline)
            if isinstance(existing_eligibility_deadline, str)
            else existing_eligibility_deadline
        )
        if not isinstance(existing, datetime) or existing.tzinfo is None:
            raise ValueError("existing eligibility deadline requires timezone")
        router_eligibility_end = min(router_eligibility_end, existing.astimezone(UTC))
    if now >= router_eligibility_end:
        raise ValueError("reviewed OpenRouter eligibility expired")
    daily = openrouter["free_model_daily_requests"]
    if (
        openrouter.get("is_free_tier") is not True
        or not isinstance(daily, dict)
        or set(daily) != {"used", "remaining", "limit"}
        or any(type(v) is not int or v < 0 for v in daily.values())
        or daily["limit"] != 50
        or daily["remaining"] != max(0, daily["limit"] - daily["used"])
    ):
        raise ValueError("unverified free request allowance")
    model = openrouter["model"]
    if model["id"] != OR_MODEL or any(
        Decimal(str(model["pricing"].get(key, 0))) != 0
        for key in ("prompt", "completion", "request", "image", "web_search", "internal_reasoning")
    ):
        raise ValueError("zero-price model required")
    if not {"prompt", "completion"} <= model["pricing"].keys():
        raise ValueError("price evidence missing")
    if (
        any(
            "text" not in model["architecture"][kind]
            for kind in ("input_modalities", "output_modalities")
        )
        or model["context_length"] < 32768
    ):
        raise ValueError("text model envelope unverified")
    registry, gateway = copy.deepcopy(base_registry), copy.deepcopy(base_gateway)
    gateway["targets"] = [
        row
        for row in gateway["targets"]
        if not (row["provider"] == "openrouter" and row["model"] == OR_MODEL)
    ]
    for row in gateway["targets"]:
        row["enabled"] = False
        row["billing_evidence_state"] = "unknown"
    for provider, profile, origin, endpoint, model_id, context, source in (
        (
            "openai",
            "openai_responses",
            "https://api.openai.com",
            "/v1/responses",
            OPENAI_MODEL,
            65536,
            "https://developers.openai.com/api/docs/models/gpt-6-luna",
        ),
        (
            "openrouter",
            "openai_inference",
            "https://openrouter.ai",
            "/api/v1/chat/completions",
            OR_MODEL,
            32768,
            "https://openrouter.ai/api/v1/models?output_modalities=all",
        ),
    ):
        if any(p["id"] == provider for p in registry["providers"]):
            raise ValueError("shared provider already configured")
        registry["providers"].append(
            {"id": provider, "adapter": profile, "origin": origin, "endpoint": origin + endpoint}
        )
        registry["models"].append(
            {
                "provider": provider,
                "model": model_id,
                "capability": "text_generation",
                "context_tokens": context,
                "max_output_tokens": OUTPUT_TOKENS,
                "features": ["text", "text_generation"],
                "source": source,
                "verified_at": now.date().isoformat(),
                "free_kind": "account_credit_grant" if provider == "openai" else "free_variant",
                "use_restrictions": "Broker text-only local envelope; no tools, streaming or paid fallback.",
                **({"replace_builtin": True} if provider == "openrouter" else {}),
            }
        )
    observations = []
    for provider, identity, model_id, metric, window, as_of, until, remaining, limit in (
        (
            "openai",
            OPENAI_ID,
            OPENAI_MODEL,
            CREDIT_METRIC,
            "budget",
            observed,
            valid,
            int(balance) - 1000,
            int(balance),
        ),
        (
            "openrouter",
            OR_ID,
            OR_MODEL,
            "requests",
            "day",
            checked,
            min(
                checked + timedelta(minutes=15),
                (checked + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
            ),
            daily["remaining"],
            daily["limit"],
        ),
    ):
        source = owner["source"] if provider == "openai" else "https://openrouter.ai/api/v1/key"
        scope = provider + ":personal-shared"
        eligibility_end = valid if provider == "openai" else router_eligibility_end
        # OpenRouter still requires a fresh 15-minute authenticated free counter.
        # A successful refresh never extends this reviewed configuration deadline.
        capacity = {
            "kind": "one_time_gift" if provider == "openai" else "short_renewable",
            "refresh_seconds": None if provider == "openai" else 86400,
            "as_of": as_of.isoformat(),
            "source": source,
            "scope": "account",
            "expires_at": eligibility_end.isoformat(),
        }
        if provider == "openai":
            capacity["credit_expires_at"] = deadline.isoformat()
        gateway["targets"].append(
            {
                "id": identity,
                "provider": provider,
                "model": model_id,
                "account_id": "personal-shared",
                "enabled": True,
                "free_eligible": True,
                "billing_enabled": False,
                "billing_evidence_state": "disabled",
                "verified_at": as_of.isoformat(),
                "expires_at": eligibility_end.isoformat(),
                "source": source,
                "priority": 0,
                "concurrency_limit": 1,
                "shared_concurrency_scope": scope,
                "shared_concurrency_limit": 1,
                "max_output_tokens": OUTPUT_TOKENS,
                "secret_ref": provider.upper() + "_API_KEY",
                "local_safety_caps": [
                    {
                        "bucket": scope + ":minute",
                        "metric": "requests",
                        "limit": 10,
                        "window": "rolling_minute",
                    },
                    {
                        "bucket": scope + ":day",
                        "metric": "requests",
                        "limit": 100 if provider == "openai" else 50,
                        "window": "day",
                    },
                ],
                "provider_quota_facts": [],
                "capacity": capacity,
                "required_capacity": [{"metric": metric, "window": window}],
                "resource_estimates": [
                    {
                        "metric": CREDIT_METRIC,
                        "amount": CREDIT_BOUND,
                        "max_input_bytes": INPUT_BYTES,
                        "max_output_tokens": OUTPUT_TOKENS,
                        "source": "trusted_operator",
                        "verified_at": observed.isoformat(),
                        "expires_at": valid.isoformat(),
                    }
                ]
                if provider == "openai"
                else [],
            }
        )
        observations.append(
            {
                "target_id": identity,
                "observation": {
                    "metric": metric,
                    "window": window,
                    "remaining": remaining,
                    "limit": limit,
                    "as_of": as_of.isoformat(),
                    "valid_until": until.isoformat(),
                    "reset_at": None
                    if provider == "openai"
                    else (checked + timedelta(days=1))
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .isoformat(),
                    "provenance": "observed" if provider == "openai" else "official",
                    "source": "trusted_operator"
                    if provider == "openai"
                    else "adapter:openrouter_key_account",
                    "confidence": "administrator_verified"
                    if provider == "openai"
                    else "provider_reported",
                },
            }
        )
    return {
        "registry": registry,
        "gateway": gateway,
        "observations": observations,
        "derivation": {
            "owner_balance_usd": owner["balance_usd"],
            "owner_observed_at": owner["received_at"],
            "provider_current_balance_usd": None,
            "prior_independent_probe_reserved_micros": 1000,
            "per_request_credit_bound_micros": CREDIT_BOUND,
            "cost_source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
            "cost_basis": "(4*4096+256)*0.125 + 512*0.50 = 2336 USD micros; rounded upward to 3000",
            "actual_cost_known": False,
            "historical_gap": True,
        },
    }


def validate_runtime_configuration(documents, *, now=None):
    """Purely offline cross-file admission; no live DB, key or provider access.

    Load the same parsers as the non-root service using an ephemeral private
    directory. Observation validation uses only a disposable SQLite database.
    Crypto/state-file bindings are deliberately left to the fixed live launcher.
    """
    from quota_broker.access import AccessPolicyFile
    from quota_broker.config import load_gateway_config, load_secret_inventory
    from quota_broker.registry import Registry
    from quota_broker.routing import RoutingState

    names = {
        "registry.json",
        "gateway.json",
        "observations.json",
        "secret-inventory.json",
        "access.json",
    }
    if not isinstance(documents, dict) or set(documents) != names:
        raise ValueError("runtime_configuration_files")
    provider_refs = {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "google": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
        "cloudflare": "CLOUDFLARE_API_TOKEN",
        "ocrspace": "OCRSPACE_API_KEY",
        "sambanova": "SAMBANOVA_API_KEY",
        "huggingface": "HUGGINGFACE_API_TOKEN",
        "cerebras": "CEREBRAS_API_KEY",
    }
    legacy_refs = frozenset({"OPENAI_API_KEY", "OPENROUTER_API_KEY"})
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("validation clock requires timezone")
    with tempfile.TemporaryDirectory(prefix="quota-shared-preflight-") as directory:
        private = Path(directory)
        for name, document in documents.items():
            raw = json.dumps(document, allow_nan=False).encode()
            if not 0 < len(raw) <= 262144:
                raise ValueError("runtime_configuration_bound")
            fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
        registry = Registry.from_manifest(documents["registry.json"])
        targets = load_gateway_config(private / "gateway.json", registry=registry)
        inventory = load_secret_inventory(
            private / "secret-inventory.json", "api-quota-broker", "dev"
        )
        required_names = set()
        principals = AccessPolicyFile(private / "access.json").read()
        for target in targets:
            if not target.enabled:
                continue
            if (
                target.provider not in provider_refs
                or target.secret_ref != provider_refs[target.provider]
                or not target.required_capacity
                or (target.provider == "openai" and (CREDIT_METRIC, "budget") not in target.required_capacity)
                or (target.provider == "openrouter" and ("requests", "day") not in target.required_capacity)
                or (target.provider == "cloudflare" and ("neurons", "day") not in target.required_capacity)
                or (target.account_id_ref != ("CLOUDFLARE_ACCOUNT_ID" if target.provider == "cloudflare" else None))
            ):
                raise ValueError("enabled_target_requires_capacity_and_credential_binding")
            required_names.add(target.secret_ref)
            if target.account_id_ref:
                required_names.add(target.account_id_ref)
            if (
                target.verified_at is None
                or target.expires_at is None
                or not target.verified_at < target.expires_at <= inventory.expires_at
            ):
                raise ValueError("eligibility_exceeds_inventory_validity")
        if not (
            inventory.names == frozenset(required_names)
            or (inventory.names == legacy_refs and required_names <= set(legacy_refs))
        ):
            raise ValueError("inventory_scope")
        rows = documents["observations.json"]
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError("observation_count")
        routing = RoutingState(str(private / "observations.sqlite3"))
        target_ids, seen = {target.id for target in targets}, set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"target_id", "observation"}
                or row["target_id"] not in target_ids
                or not isinstance(row["observation"], dict)
            ):
                raise ValueError("observation_target_binding")
            observation = row["observation"]
            identity = (row["target_id"], observation.get("metric"), observation.get("window"))
            if identity in seen:
                raise ValueError("observation_duplicate")
            seen.add(identity)
            routing.observe(row["target_id"], observation, current)
    return {
        "targets": len(targets),
        "enabled_targets": sum(target.enabled for target in targets),
        "principals": len(principals),
    }
