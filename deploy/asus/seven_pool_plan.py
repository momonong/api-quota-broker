"""Pure bounded admission/config/probe plan. No credentials, provider or host IO."""

import copy
from datetime import UTC, datetime

PROVIDERS = {
    "nvidia": ("nvidia/nemotron-3.5-lightning-30b-a3b", "NVIDIA_API_KEY", 64),
    "google": ("gemini-3.5-flash-lite", "GEMINI_API_KEY", 64),
    "mistral": ("ministral-3b-latest", "MISTRAL_API_KEY", 64),
    "cloudflare": ("@cf/meta/llama-3.2-1b-instruct", "CLOUDFLARE_API_TOKEN", 64),
    "openrouter": ("liquid/lfm-2.5-2.6b:free", "OPENROUTER_API_KEY", 64),
    "ocrspace": ("ocr.space/engine2", "OCRSPACE_API_KEY", 1),
    "groq": ("openai/gpt-oss-20b", "GROQ_API_KEY", 64),
}
KEY_PREFIX = "asus-seven-pool-2026-10-08-r2-"
AUTO_KEY = KEY_PREFIX + "auto-a1"
OFFICIAL = {
    "nvidia": "https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b/build",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
    "mistral": "https://docs.mistral.ai/admin/billing-usage/usage-limits",
    "cloudflare": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
    "openrouter": "https://openrouter.ai/liquid/lfm-2.5-2.6b:free",
    "ocrspace": "https://ocr.space/ocrapi",
    "groq": "https://console.groq.com/docs/rate-limits",
}
NORMAL_CAPS = {
    "nvidia": (10, 500, 8192),
    "google": (3, 100, 4096),
    "mistral": (3, 100, 4096),
    "cloudflare": (20, 1000, 4096),
    "openrouter": (10, 40, 8192),
    "ocrspace": (10, 300, 0),
    "groq": (10, 500, 4096),
}


class Invalid(ValueError):
    pass


def need(ok):
    if not ok:
        raise Invalid("admission_policy_invalid")


def instant(value):
    result = datetime.fromisoformat(value)
    need(result.tzinfo is not None)
    return result


def task_key(provider):
    need(provider in PROVIDERS and provider != "groq")
    return KEY_PREFIX + provider + "-a1"


def evidence_valid(provider, evidence, now):
    """Credential expiry is not evidence of account Free eligibility."""
    try:
        need(
            set(evidence)
            == {
                "model",
                "key_ref",
                "account_scope",
                "human_free_no_payment_details",
                "account_basis",
                "account_reviewed_at",
                "account_valid_until",
                "official_url",
                "official_reviewed_at",
                "official_valid_until",
            }
        )
        need(
            evidence["model"] == PROVIDERS[provider][0]
            and evidence["key_ref"] == PROVIDERS[provider][1]
            and evidence["account_scope"] == "asus-dev-" + provider + "-key"
            and evidence["human_free_no_payment_details"] is True
            and evidence["account_basis"] == "existing_human_free_declaration_same_key_slot"
            and evidence["official_url"] == OFFICIAL[provider]
        )
        for prefix in ("account", "official"):
            start, end = (
                instant(evidence[prefix + "_reviewed_at"]),
                instant(evidence[prefix + "_valid_until"]),
            )
            need(start <= now < end and 0 < (end - start).total_seconds() <= 14 * 86400)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def config(base_plan, admitted, evidence, credential_expiry, *, normal=False, now=None):
    now = now or datetime.now(UTC)
    end = instant(credential_expiry)
    need(end > now)
    result = copy.deepcopy(base_plan.disabled_config(normal=normal))
    for target in result["targets"]:
        provider = target["provider"]
        target["model"], target["secret_ref"], target["max_output_tokens"] = PROVIDERS[provider]
        target["enabled"] = target["free_eligible"] = False
        target["billing_enabled"] = False
        ev = evidence.get(provider)
        valid = bool(provider in admitted and ev and evidence_valid(provider, ev, now))
        expiry = (
            min(end, instant(ev["account_valid_until"]), instant(ev["official_valid_until"]))
            if valid
            else None
        )
        target.update(
            enabled=valid,
            free_eligible=valid,
            verified_at=now.isoformat() if valid else None,
            expires_at=expiry.isoformat() if expiry else None,
            source="same key-slot human Free declaration plus independent current official model evidence; local validity"
            if valid
            else "not admitted: account/model evidence or ASUS acceptance missing",
        )
        # Stable IDs/buckets/scopes retain every old Groq charge and other hold.
        rpm, rpd, tokens = NORMAL_CAPS[provider] if normal else (3, 10, 4096)
        for cap in target["local_safety_caps"]:
            cap["limit"] = (
                rpm
                if cap["window"] == "rolling_minute" and cap["metric"] == "requests"
                else (
                    rpd
                    if cap["window"] == "day" and cap["metric"] == "requests"
                    else tokens
                    if cap["metric"] == "input_tokens"
                    else 8000
                    if normal and cap["metric"] == "neurons"
                    else 300
                    if cap["metric"] == "neurons"
                    else 20000
                    if normal
                    else 100
                )
            )
        # Only documented short periodic Free windows get refresh rank. This
        # does not claim observed account remaining or provider limits.
        if valid and provider in {"groq", "google", "cloudflare", "openrouter"}:
            target["capacity"] = {
                "kind": "short_renewable",
                "refresh_seconds": 60 if provider == "groq" else 86400,
                "as_of": ev["official_reviewed_at"],
                "expires_at": expiry.isoformat(),
                "source": "documented Free periodic window; account remaining unknown",
                "scope": target["shared_concurrency_scope"],
            }
        if provider == "cloudflare" and valid:
            from quota_broker.config import CF_NEURON_FORMULA, cloudflare_neuron_upper_bound

            target["neuron_estimate"] = {
                "source": CF_NEURON_FORMULA,
                "amount": cloudflare_neuron_upper_bound(4096, 64) + 1,
                "max_input_tokens": 4096,
                "max_output_tokens": 64,
                "verified_at": now.isoformat(),
                "expires_at": expiry.isoformat(),
            }
    return result


def tasks(synthetic_png):
    return {
        provider: {
            "request_key": task_key(provider),
            "provider": provider,
            "model": model,
            "capability": "ocr" if provider == "ocrspace" else "text_generation",
            "input": synthetic_png if provider == "ocrspace" else "Reply with exactly READY.",
            "max_output_tokens": output,
            "max_attempts": 1,
            "wait_policy": "reject",
        }
        for provider, (model, _key, output) in PROVIDERS.items()
        if provider != "groq"
    }


def auto_task():
    return {
        "request_key": AUTO_KEY,
        "capability": "text_generation",
        "input": "Reply with exactly READY.",
        "max_output_tokens": 64,
        "max_attempts": 1,
        "wait_policy": "reject",
    }


if __name__ == "__main__":
    print(
        '{"mode":"seven_pool_bounded_plan","provider_posts_max":7,"groq_new_posts":0,"model_GET":0,"host_changes":0}'
    )
