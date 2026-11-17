"""Conservative interpretation of provider Retry-After metadata."""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        if len(value) > 10:
            return None
        return min(int(value), 86_400)
    try:
        instant = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if instant.tzinfo is None:
        return None
    seconds = math.ceil((instant.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
    return min(max(seconds, 0), 86_400)


def retry_delay(
    request_key: str, attempt: int, retry_after: int | None = None, *, base_seconds: float = 1
) -> float:
    """Stable per-operation jitter avoids synchronized retries after a restart."""
    import hashlib

    fraction = (
        int.from_bytes(hashlib.sha256(f"{request_key}:{attempt}".encode()).digest()[:2], "big")
        / 65535
    )
    exponential = min(30.0, base_seconds * 2.0 ** min(max(attempt, 0), 5))
    return max(float(retry_after or 0), round(exponential * (1 + fraction / 4), 3))


def complete_overload_refusal(
    provider: str,
    capability: str,
    status: int | None,
    raw: bytes,
    headers: dict[str, str],
    *,
    url: str,
    reject_if_busy: bool = False,
) -> bool:
    """Recognize complete synchronous refusal envelopes, not arbitrary 5xx.

    This proves a failed response, NOT zero internal tokens. Unknown quota units
    stay held. No async jobs, partial output, gateway timeout or transport error
    can reach this predicate as a complete accepted result.
    """
    import json
    from urllib.parse import urlsplit

    from .provider_policy import ENDPOINT_HOSTS

    if capability not in {"text_generation", "code_completion", "translation", "vision"}:
        return False
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ENDPOINT_HOSTS.get(provider, set()):
        return False
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if content_type.split(";")[0].strip().lower() != "application/json" or len(raw) > 65536:
        return False
    try:

        def unique(pairs: list) -> dict:
            result: dict = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError("duplicate field")
                result[k] = v
            return result

        body = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(body, dict):
        return False
    if provider == "cloudflare":
        return bool(
            status == 429
            and reject_if_busy
            and body.get("success") is False
            and set(body) <= {"success", "errors", "messages", "result"}
            and body.get("result") is None
            and isinstance(body.get("errors"), list)
            and len(body["errors"]) == 1
            and isinstance(body["errors"][0], dict)
            and body["errors"][0].get("code") == 3040
        )
    if status != 503 or set(body) != {"error"} or not isinstance(body["error"], dict):
        return False
    error = body["error"]
    if set(error) - {"message", "type", "code", "status", "param", "details"}:
        return False
    if not isinstance(error.get("message"), str) or not error["message"]:
        return False
    if provider == "google":
        return (
            error.get("code") == 503
            and error.get("status") == "UNAVAILABLE"
            and parsed.path.endswith(":generateContent")
        )
    if provider == "openai":
        return (
            error.get("type") == "service_unavailable_error"
            and error.get("code") == "server_is_overloaded"
        )
    if provider in {"nvidia", "groq", "mistral", "openrouter"} and parsed.path.endswith(
        "/chat/completions"
    ):
        # A strict, buffered OpenAI-compatible service-unavailable envelope.
        return error.get("code") in {
            503,
            "service_unavailable",
            "server_is_overloaded",
        } or error.get("type") in {"service_unavailable_error", "overloaded_error"}
    return False
