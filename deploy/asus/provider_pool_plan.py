"""Offline ASUS pool proposal. Never reads credentials, calls APIs or enables targets."""

import argparse
import copy
import json
from datetime import UTC, datetime

NORMAL_LOCAL_CAPS = {
    # Configurable safety estimates, not observed account limits/remaining.
    "nvidia": (10, 500, 8192),
    "google": (3, 100, 4096),
    "mistral": (3, 100, 4096),
    "cloudflare": (20, 1000, 4096),
    "openrouter": (10, 40, 8192),
    "ocrspace": (10, 300, 0),
    "groq": (10, 500, 4096),
}

PROVIDERS = {
    "nvidia": ("nvidia/nemotron-3.5-lightning-30b-a3b", "NVIDIA_API_KEY", 64),
    "google": ("gemini-3.5-flash-lite", "GEMINI_API_KEY", 64),
    "mistral": ("ministral-3b-latest", "MISTRAL_API_KEY", 64),
    "cloudflare": ("@cf/meta/llama-3.2-1b-instruct", "CLOUDFLARE_API_TOKEN", 64),
    "openrouter": ("liquid/lfm-2.5-2.6b:free", "OPENROUTER_API_KEY", 256),
    "ocrspace": ("ocr.space/engine2", "OCRSPACE_API_KEY", 1),
    "groq": ("openai/gpt-oss-20b", "GROQ_API_KEY", 512),
}
UNKNOWN = {
    "value": None,
    "provenance": "unknown",
    "as_of": None,
    "source": None,
    "scope": None,
    "valid_until": None,
}


def disabled_config(*, normal: bool = False) -> dict:
    """Local caps are proposals; account eligibility and provider quota stay unknown.

    Groq retains its existing account and quota scope so historical usage/holds
    cannot be bypassed by introducing a second alias. Other providers have no
    existing ASUS execution scope. Nothing in this output grants admission.
    """
    targets = []
    for priority, (provider, (model, secret_ref, output)) in enumerate(PROVIDERS.items()):
        scope = f"{provider}:asus-dev-key"
        rpm, rpd, input_cap = NORMAL_LOCAL_CAPS[provider] if normal else (3, 10, 4096)
        dimensions = [
            ("requests", "rolling_minute", rpm, "UTC"),
            ("requests", "day", rpd, "America/Los_Angeles" if provider == "google" else "UTC"),
        ]
        if provider != "ocrspace":
            dimensions.append(("input_tokens", "rolling_minute", input_cap, "UTC"))
        if provider == "cloudflare":
            dimensions.append(("neurons", "day", 8000 if normal else 300, "UTC"))
        if provider == "ocrspace":
            # Conservative local calendar-month budget; not the account billing cycle.
            dimensions.append(("requests", "month", 20000 if normal else 100, "UTC"))
        target = {
            "id": f"asus-{provider}-pool",
            "provider": provider,
            "model": model,
            "account_id": f"asus-dev-{provider}-key",
            "secret_ref": secret_ref,
            "enabled": False,
            "free_eligible": False,
            "billing_enabled": False,
            "verified_at": None,
            "expires_at": None,
            "source": "pending scoped account evidence and ASUS provider acceptance",
            "local_safety_caps": [
                {
                    "bucket": f"{scope}:{metric}:{window}",
                    "metric": metric,
                    "window": window,
                    "limit": limit,
                    "timezone": zone,
                }
                for metric, window, limit, zone in dimensions
            ],
            "provider_quota_facts": [
                {
                    "metric": metric,
                    "window": window,
                    "limit": copy.deepcopy(UNKNOWN),
                    "remaining": copy.deepcopy(UNKNOWN),
                }
                for metric, window, _, _ in dimensions
            ],
            "capacity": {
                "kind": "unknown",
                "refresh_seconds": None,
                "as_of": None,
                "expires_at": None,
                "source": None,
                "scope": None,
            },
            "concurrency_limit": 1,
            "shared_concurrency_scope": scope + ":shared",
            "shared_concurrency_limit": 1,
            "max_output_tokens": (
                1 if provider == "ocrspace" else 256 if provider == "cloudflare" else 512
            )
            if normal
            else output,
            "priority": priority,
        }
        if provider == "cloudflare":
            target["account_id_ref"] = "CLOUDFLARE_ACCOUNT_ID"
            # No Neurons estimate until an operator attests its bounds/validity.
        targets.append(target)
    return {"targets": targets}


def qualified_config(
    providers: set[str], credential_expiry: str, attested_at: str, *, normal: bool = False
) -> dict:
    """For the reviewed operator only: scoped human evidence, no invented freshness.

    This function never reads a key or qualifies a live result. The operator must
    restrict normal providers to complete-response successes (Groq is reusable).
    Validity ends at the actual runtime credential expiry, not an arbitrary daily
    ATTEST deadline. This is a local admission validity bound, not a provider fact.
    """
    if not isinstance(providers, set) or not providers <= PROVIDERS.keys():
        raise ValueError("invalid provider set")
    start, expiry = datetime.fromisoformat(attested_at), datetime.fromisoformat(credential_expiry)
    if start.tzinfo is None or expiry.tzinfo is None or expiry <= start:
        raise ValueError("invalid evidence validity")
    config = disabled_config(normal=normal)
    config["targets"] = [target for target in config["targets"] if target["provider"] in providers]
    for target in config["targets"]:
        target.update(
            enabled=True,
            free_eligible=True,
            verified_at=start.isoformat(),
            expires_at=expiry.isoformat(),
            source="human Free/no-payment-details declaration; fixed free route; personal development/prototyping",
        )
        provider = target["provider"]
        if provider in {"groq", "google", "cloudflare"}:
            target["capacity"] = {
                "kind": "short_renewable",
                "refresh_seconds": 60 if provider == "groq" else 86400,
                "as_of": start.isoformat(),
                "expires_at": expiry.isoformat(),
                "source": "documented Free periodic limits; same key-slot human Free declaration",
                "scope": target["shared_concurrency_scope"],
            }
        if provider == "cloudflare":
            from quota_broker.config import CF_NEURON_FORMULA, cloudflare_neuron_upper_bound

            bound = 4096
            output = target["max_output_tokens"]
            target["neuron_estimate"] = {
                "source": CF_NEURON_FORMULA,
                "amount": cloudflare_neuron_upper_bound(bound, output) + 1,
                "max_input_tokens": bound,
                "max_output_tokens": output,
                "verified_at": datetime.now(UTC).isoformat(),
                "expires_at": expiry.isoformat(),
            }
    return config


def plan() -> dict:
    return {
        "mode": "offline_asus_provider_pool_proposal",
        "existing_groq_evidence_reused": True,
        "old_once_claim_replay": False,
        "new_token_creation": 0,
        "authenticated_metadata_gets": 0,
        "provider_calls": 0,
        "service_changes": 0,
        "default_enabled_targets": 0,
        "probes": [
            {
                "provider": provider,
                "model": model,
                "capability": "ocr" if provider == "ocrspace" else "text_generation",
                "initial_post_limit": 0 if provider == "groq" else 1,
                "phase_post_ceiling_after_evidenced_repairs": 3,
                "automatic_retry": False,
                "max_output_tokens": output,
                "output_bound_basis": "legacy_ocr_sentinel_not_provider_tokens"
                if provider == "ocrspace"
                else "reasoning_headroom"
                if provider in {"groq", "openrouter"}
                else "minimal_complete_text_probe",
                "synthetic_input": "generated_OK_png" if provider == "ocrspace" else "fixed_READY",
                "provider_timeout_seconds": 120 if provider == "nvidia" else 30,
                "account_evidence": "historical_human_free_no_payment_details_declaration",
                "reported_model_evidence": "pending_provider_response_metadata",
                "enable_requires": [
                    "current_account_free_evidence",
                    "billing_disabled",
                    "complete_response",
                    "ledger_verified",
                    "bounded_scope",
                ],
            }
            for provider, (model, _, output) in PROVIDERS.items()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disabled-config", action="store_true")
    parser.add_argument(
        "--normal", action="store_true", help="larger configurable safety estimates; still disabled"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            disabled_config(normal=args.normal) if args.disabled_config else plan(), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
