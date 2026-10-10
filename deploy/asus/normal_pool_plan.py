"""1.0 normal-pool limits only; preserve admission, identity, and r2 receipts."""

import copy
from datetime import datetime

from quota_broker.catalog import MODELS
from quota_broker.config import CF_NEURON_FORMULA, cloudflare_neuron_upper_bound

# Output/request and input/minute are local operating policy, not account quota.
PROFILES = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": ("nvidia", 4096, 65536),
    "gemini-3.5-flash-lite": ("google", 8192, 131328),
    "ministral-3b-latest": ("mistral", 4096, 65536),
    "@cf/meta/llama-3.2-1b-instruct": ("cloudflare", 2048, 32768),
    "liquid/lfm-2.5-2.6b:free": ("openrouter", 4096, 32768),
    "openai/gpt-oss-20b": ("groq", 4096, 32768),
    "ocr.space/engine2": ("ocrspace", 1, 0),
}


def config(existing: dict) -> dict:
    """No renewed Free evidence, provider enablement, new scope, or ledger IO."""
    if set(existing) != {"targets"} or not isinstance(existing["targets"], list):
        raise ValueError("normal_pool_config_invalid")
    result = copy.deepcopy(existing)
    for target in result["targets"]:
        model = target["model"]
        provider, output, input_cap = PROFILES[model]
        spec = MODELS[model]
        if provider != target["provider"] or target["billing_enabled"]:
            raise ValueError("normal_pool_scope_invalid")
        target["max_output_tokens"] = min(output, spec.max_output_tokens)
        input_caps = [q for q in target["local_safety_caps"] if q["metric"] == "input_tokens"]
        for cap in input_caps:
            cap["limit"] = min(input_cap, spec.context_tokens - target["max_output_tokens"])
        if provider != "ocrspace" and not input_caps:
            raise ValueError("normal_pool_input_cap_missing")
        if provider == "cloudflare":
            # Bound the same input/output envelope admitted by local caps.
            bound = min(cap["limit"] for cap in input_caps)
            estimate = target.get("neuron_estimate")
            if target["enabled"] and not estimate:
                raise ValueError("normal_pool_neuron_estimate_missing")
            if estimate:
                if estimate["source"] != CF_NEURON_FORMULA:
                    raise ValueError("normal_pool_neuron_source_invalid")
                datetime.fromisoformat(estimate["expires_at"])
                estimate.update(
                    amount=cloudflare_neuron_upper_bound(bound, target["max_output_tokens"]) + 1,
                    max_input_tokens=bound,
                    max_output_tokens=target["max_output_tokens"],
                )
    return result
