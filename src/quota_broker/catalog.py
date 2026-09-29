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
    "meta/llama-3.1-8b-instruct": Model(
        provider="nvidia",
        model="meta/llama-3.1-8b-instruct",
        author="Meta",
        host="NVIDIA API Catalog",
        context_tokens=131_072,
        max_output_tokens=4096,
        capability="text_generation",
        origin="https://integrate.api.nvidia.com",
        endpoint_template="https://integrate.api.nvidia.com/v1/chat/completions",
        free_kind="developer_prototyping_subject_to_verified_account_limits",
        use_restrictions="Verify account eligibility and limits; catalog preview is for prototyping.",
        source="https://docs.api.nvidia.com/nim/reference/meta-llama-3_1-8b-infer",
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
}


def endpoint(model: Model, account_id: str) -> str:
    if model.provider == "cloudflare" and (
        not account_id or not all(c.isascii() and (c.isalnum() or c in "_-") for c in account_id)
    ):
        raise ValueError("invalid Cloudflare account id")
    return model.endpoint_template.format(account_id=account_id)
