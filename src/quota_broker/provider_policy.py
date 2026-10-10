"""Official inference hosts shared by public evidence and buffered transports."""

import re

_MODEL_SECRET = re.compile(
    r"(?:gsk_|sk-[A-Za-z0-9_-]{12}|dp\.st\.|Bearer\s|AIza[A-Za-z0-9_-]{12}|"
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----)",
    re.IGNORECASE,
)


def catalog_model_id_reason(value: object) -> str | None:
    """Validate an opaque catalog/JSON-body identity, without treating it as a URL path."""
    if not isinstance(value, str):
        return "type"
    if not value.strip():
        return "empty"
    if len(value) > 256:
        return "length"
    if not value.isprintable():
        return "characters"
    if "://" in value:
        return "url"
    if _MODEL_SECRET.search(value):
        return "secret_pattern"
    return None


ENDPOINT_HOSTS = {
    "nvidia": {"integrate.api.nvidia.com", "ai.api.nvidia.com"},
    "google": {"generativelanguage.googleapis.com"},
    "cloudflare": {"api.cloudflare.com"},
    "groq": {"api.groq.com"},
    "mistral": {"api.mistral.ai"},
    "openrouter": {"openrouter.ai"},
    "ocrspace": {"api.ocr.space"},
}
