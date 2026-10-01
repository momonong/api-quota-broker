"""Fixed official provider requests for the unified gateway. No persistence here."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .catalog import MODELS, endpoint
from .client import _NoRedirect

MAX_RESPONSE_BYTES = 65_536
RIVA = "nvidia/riva-translate-4b-instruct-v2"


class ProviderError(ValueError):
    pass


def official_request(
    provider: str,
    model_id: str,
    account_id: str,
    secret: str,
    content: str,
    max_output_tokens: int,
    source_language: str | None,
    target_language: str | None,
) -> tuple[str, dict[str, str], dict[str, object]]:
    model = MODELS.get(model_id)
    if model is None or provider != model.provider:
        raise ProviderError("unrecognized provider/model")
    url = endpoint(model, account_id)
    if not url.startswith(model.origin + "/"):
        raise ProviderError("unapproved provider URL")
    if provider == "nvidia":
        messages: list[dict[str, str]] = []
        if model_id == RIVA:
            if source_language is None or target_language is None:
                raise ProviderError("translation languages required")
            messages.append({"role": "system", "content": f"{source_language}-{target_language}"})
        messages.append({"role": "user", "content": content})
        payload: dict[str, object] = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        if model_id == RIVA:
            payload["temperature"] = 0
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return url, {"Authorization": "Bearer " + secret}, payload
    if provider in {"groq", "mistral", "openrouter"}:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        }
        if provider == "groq":
            payload.update(
                {
                    "max_completion_tokens": max_output_tokens,
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                }
            )
        else:
            payload["max_tokens"] = max_output_tokens
        return url, {"Authorization": "Bearer " + secret}, payload
    if provider == "google":
        return (
            url,
            {"x-goog-api-key": secret},
            {
                "contents": [{"parts": [{"text": content}]}],
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            },
        )
    if provider == "cloudflare":
        return (
            url,
            {"Authorization": "Bearer " + secret},
            {
                "prompt": content,
                "max_tokens": max_output_tokens,
            },
        )
    if provider == "ocrspace":
        return (
            url,
            {"apikey": secret},
            {
                "base64Image": (
                    "data:image/jpeg;base64,"
                    if content.startswith("/9j/")
                    else "data:image/png;base64,"
                )
                + content,
                "OCREngine": "2",
                "isOverlayRequired": "false",
            },
        )
    raise ProviderError("unsupported provider")


def provider_http(
    url: str, headers: dict[str, str], payload: dict[str, object], timeout: float
) -> tuple[int, dict[str, str], bytes]:
    if not any(
        url == endpoint(model, "fixture")
        for model in MODELS.values()
        if model.provider != "cloudflare"
    ) and not re.fullmatch(
        r"https://api\.cloudflare\.com/client/v4/accounts/[A-Za-z0-9_-]+/ai/run/@cf/meta/llama-3\.2-1b-instruct",
        url,
    ):
        raise ProviderError("unapproved provider URL")
    ocr = url == "https://api.ocr.space/parse/image"
    request = urllib.request.Request(
        url,
        data=(
            urllib.parse.urlencode(payload).encode()
            if ocr
            else json.dumps(payload, separators=(",", ":")).encode()
        ),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded" if ocr else "application/json",
            "Accept": "application/json",
            **headers,
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider response too large")
        return response.status, dict(response.headers), raw


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return None


def explicit_quota_rejection(
    provider: str, status: int, headers: dict[str, str], raw: bytes
) -> bool:
    """Only documented, unambiguous non-execution responses permit another POST.

    A generic HTTP 429 is insufficient: Cloudflare also uses it for capacity
    exhaustion, and other providers may return incomplete intermediary errors.
    """
    if status != 429:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    if any(
        data.get(field) not in (None, {}, [], "")
        for field in (
            "usage",
            "usageMetadata",
            "choices",
            "output",
            "result",
            "candidates",
            "ParsedResults",
        )
    ):
        return False
    if provider == "cloudflare":
        errors = data.get("errors")
        return isinstance(errors, list) and any(
            isinstance(error, dict) and error.get("code") == 3036 for error in errors
        )
    error = data.get("error")
    if not isinstance(error, dict):
        return False
    if provider == "google":
        return (
            error.get("code") in {"quota_exceeded", "rate_limit_exceeded", "too_many_requests"}
            or error.get("status") == "RESOURCE_EXHAUSTED"
        )
    if provider == "groq":
        # Groq documents retry-after as present only for its rate-limit 429.
        return isinstance(headers.get("retry-after") or headers.get("Retry-After"), str)
    if provider == "openrouter":
        metadata = error.get("metadata")
        normalized = {name.lower(): value for name, value in headers.items()}
        return (
            error.get("code") == 429
            and isinstance(metadata, dict)
            and metadata.get("error_type") == "rate_limit_exceeded"
            and "provider_code" not in metadata
            and all(
                normalized.get(name)
                for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
            )
        )
    return False


def interpret(
    provider: str, status: int, raw: bytes
) -> tuple[str | None, int | None, int | None, int | None, str | None]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    answer: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    neurons: int | None = None
    request_id: str | None = None
    if provider == "ocrspace":
        parsed = data.get("ParsedResults")
        if data.get("OCRExitCode") in (1, "1") and isinstance(parsed, list) and len(parsed) == 1:
            page = parsed[0]
            if isinstance(page, dict) and page.get("FileParseExitCode") in (1, "1"):
                value = page.get("ParsedText")
                if isinstance(value, str):
                    answer = value
    elif provider in {"nvidia", "groq", "mistral", "openrouter"}:
        usage = data.get("usage")
        if isinstance(usage, dict):
            input_tokens = _nonnegative(usage.get("prompt_tokens"))
            output_tokens = _nonnegative(usage.get("completion_tokens"))
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                answer = message["content"]
        request_id = _request_id(data.get("id") or data.get("requestId"))
    elif provider == "google":
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            input_tokens = _nonnegative(usage.get("promptTokenCount"))
            output_tokens = _nonnegative(usage.get("candidatesTokenCount"))
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    texts = [
                        part["text"]
                        for part in parts
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ]
                    if texts:
                        answer = "".join(texts)
        request_id = _request_id(data.get("responseId"))
    elif provider == "cloudflare":
        usage = data.get("usage")
        if isinstance(usage, dict):
            input_tokens = _nonnegative(usage.get("prompt_tokens"))
            output_tokens = _nonnegative(usage.get("completion_tokens"))
            neurons = _nonnegative(usage.get("neurons"))
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("response"), str):
            answer = result["response"]
        request_id = _request_id(data.get("request_id"))
    return (answer if status == 200 else None, input_tokens, output_tokens, neurons, request_id)
