"""Pinned official origins and a deliberately small text-only catalog."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    provider: str
    model: str
    author: str
    host: str
    context_tokens: int
    max_output_tokens: int
    capability: str
    origin: str
    endpoint_template: str
    free_kind: str
    use_restrictions: str
    source: str
    verified_at: str


MODELS = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": Model(
        provider="nvidia",
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        author="NVIDIA",
        host="NVIDIA API Catalog",
        context_tokens=1_048_576,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://integrate.api.nvidia.com",
        endpoint_template="https://integrate.api.nvidia.com/v1/chat/completions",
        free_kind="developer_prototyping_subject_to_verified_account_limits",
        use_restrictions="Verify account eligibility and limits; catalog preview is for prototyping.",
        source="https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer",
        verified_at="2026-09-30",
    ),
    "nvidia/riva-translate-4b-instruct-v2": Model(
        provider="nvidia",
        model="nvidia/riva-translate-4b-instruct-v2",
        author="NVIDIA",
        host="NVIDIA API Catalog",
        context_tokens=8192,
        max_output_tokens=4096,
        capability="translation",
        origin="https://integrate.api.nvidia.com",
        endpoint_template="https://integrate.api.nvidia.com/v1/chat/completions",
        free_kind="developer_prototyping_subject_to_verified_account_limits",
        use_restrictions="Verify account eligibility and limits; catalog preview is for prototyping.",
        source="https://docs.api.nvidia.com/nim/reference/nvidia-riva-translate-4b-instruct-v2",
        verified_at="2026-09-30",
    ),
    "google/gemma-4-31b-it": Model(
        provider="nvidia",
        model="google/gemma-4-31b-it",
        author="Google",
        host="NVIDIA API Catalog",
        context_tokens=262_144,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://integrate.api.nvidia.com",
        endpoint_template="https://integrate.api.nvidia.com/v1/chat/completions",
        free_kind="developer_prototyping_subject_to_verified_account_limits",
        use_restrictions="Verify account eligibility and limits; catalog preview is for prototyping.",
        source="https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer",
        verified_at="2026-09-29",
    ),
    "gemini-2.5-flash-lite": Model(
        provider="google",
        model="gemini-2.5-flash-lite",
        author="Google",
        host="Google Gemini Developer API",
        context_tokens=1_048_576,
        max_output_tokens=65_536,
        capability="text_generation",
        origin="https://generativelanguage.googleapis.com",
        endpoint_template=(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent"
        ),
        free_kind="free_tier_for_eligible_projects",
        use_restrictions="Google currently limits 2.5 model access for some new projects; verify access.",
        source="https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite",
        verified_at="2026-09-25",
    ),
    "@cf/meta/llama-3.2-1b-instruct": Model(
        provider="cloudflare",
        model="@cf/meta/llama-3.2-1b-instruct",
        author="Meta",
        host="Cloudflare Workers AI",
        context_tokens=60_000,
        max_output_tokens=256,
        capability="text_generation",
        origin="https://api.cloudflare.com",
        endpoint_template=(
            "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"
            "@cf/meta/llama-3.2-1b-instruct"
        ),
        free_kind="daily_free_allocation_subject_to_account_plan",
        use_restrictions="Workers Paid can bill beyond the daily free allocation; this broker rejects it.",
        source="https://developers.cloudflare.com/workers-ai/models/llama-3.2-1b-instruct/",
        verified_at="2026-09-25",
    ),
    "openai/gpt-oss-20b": Model(
        provider="groq",
        model="openai/gpt-oss-20b",
        author="OpenAI",
        host="GroqCloud",
        context_tokens=131_072,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://api.groq.com",
        endpoint_template="https://api.groq.com/openai/v1/chat/completions",
        free_kind="free_plan_subject_to_verified_organization_limits",
        use_restrictions="Verify organization plan, model access, and billing before dispatch.",
        source="https://console.groq.com/docs/rate-limits",
        verified_at="2026-10-02",
    ),
    "mistral-small-latest": Model(
        provider="mistral",
        model="mistral-small-latest",
        author="Mistral AI",
        host="Mistral Studio API",
        context_tokens=262_144,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://api.mistral.ai",
        endpoint_template="https://api.mistral.ai/v1/chat/completions",
        free_kind="free_mode_subject_to_verified_organization_access",
        use_restrictions="Verify Free mode, API access, and pay-as-you-go disabled before dispatch.",
        source=(
            "https://docs.mistral.ai/getting-started/quickstarts/studio/"
            "activate-and-generate-api-key"
        ),
        verified_at="2026-10-02",
    ),
    "liquid/lfm-2.5-2.6b:free": Model(
        provider="openrouter",
        model="liquid/lfm-2.5-2.6b:free",
        author="Liquid AI",
        host="OpenRouter",
        context_tokens=65_536,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://openrouter.ai",
        endpoint_template="https://openrouter.ai/api/v1/chat/completions",
        free_kind="explicit_free_model_with_zero_prompt_and_completion_price",
        use_restrictions="Pin the :free model; recheck catalog price and account limits before dispatch.",
        source="https://openrouter.ai/api/v1/models",
        verified_at="2026-10-02",
    ),
    "ocr.space/engine2": Model(
        provider="ocrspace",
        model="ocr.space/engine2",
        author="OCR.space",
        host="OCR.space Free OCR API",
        context_tokens=1_000_000,
        max_output_tokens=1,
        capability="ocr",
        origin="https://api.ocr.space",
        endpoint_template="https://api.ocr.space/parse/image",
        free_kind="free_25000_monthly_conversions_and_500_daily_requests_per_ip",
        use_restrictions="Engine 2, one PNG/JPEG image; free API has a 1 MB file limit.",
        source="https://ocr.space/ocrapi",
        verified_at="2026-10-02",
    ),
}


def endpoint(model: Model, account_id: str) -> str:
    if model.provider == "cloudflare" and (
        not account_id or not all(c.isascii() and (c.isalnum() or c in "_-") for c in account_id)
    ):
        raise ValueError("invalid Cloudflare account id")
    return model.endpoint_template.format(account_id=account_id)
