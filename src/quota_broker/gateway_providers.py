"""Fixed official provider requests for the unified gateway. No persistence here."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from .catalog import MODELS, endpoint
from .client import _NoRedirect
from .retry import parse_retry_after

MAX_RESPONSE_BYTES = 65_536
RIVA = "nvidia/riva-translate-4b-instruct-v2"


class ProviderError(ValueError):
    pass


class ProviderPhaseTimeout(ProviderError):
    """Bounded timing phase only; before_headers includes connect and TTFB."""

    def __init__(self, code: str, diagnostics: dict[str, str | int | None] | None = None):
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics or {}


class ProviderHeaders(dict[str, str]):
    """HTTP headers plus in-process curl timing; metadata is never an HTTP header."""

    def __init__(self, headers: dict[str, str], diagnostics: dict[str, str | int | None]):
        super().__init__(headers)
        self.diagnostics = diagnostics


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
    if (
        provider in {"groq", "mistral"} or model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
    ) and not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", secret):
        raise ProviderError("credential_format_rejected")
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
            if provider == "mistral" and model_id == "mistral-small-latest":
                payload["reasoning_effort"] = "none"
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
    if url == "https://api.groq.com/openai/v1/chat/completions":
        # Import lazily: the packaged curl primitives use the provider error/contract types.
        from .bounded_curl import groq_http

        return groq_http(url, headers, payload, timeout)
    if url == "https://api.mistral.ai/v1/chat/completions" or (
        url == "https://integrate.api.nvidia.com/v1/chat/completions"
        and payload.get("model") == "nvidia/nemotron-3.5-lightning-30b-a3b"
    ):
        from .bounded_curl import chat_http

        return chat_http(url, headers, payload, timeout)
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
    except TimeoutError as exc:
        raise ProviderPhaseTimeout("timeout_before_headers") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderPhaseTimeout("timeout_before_headers") from exc
        raise
    with response:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except TimeoutError as exc:
            raise ProviderPhaseTimeout("timeout_response_body") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider response too large")
        return response.status, dict(response.headers), raw


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return None


def chat_completion_metadata(status: int | None, raw: bytes) -> tuple[str | None, bool | None]:
    """Fixed completion metadata; unrecognized provider values never enter SQLite."""
    if status != 200:
        return None, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, None
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    finish = first.get("finish_reason") if isinstance(first, dict) else None
    if finish in ("stop", "length", "content_filter"):
        return finish, finish == "length"
    return ("missing" if finish is None else "unclassified"), None


def safe_transport_diagnostics(details: dict[str, str | int | None]) -> dict[str, str | int | None]:
    result: dict[str, str | int | None] = {}
    for field in (
        "time_namelookup_ms",
        "time_connect_ms",
        "time_appconnect_ms",
        "time_starttransfer_ms",
        "time_total_ms",
    ):
        value = details.get(field)
        if type(value) is int and 0 <= value <= 123_000:
            result[field] = value
    for field, allowed in (
        (
            "transport_code",
            {
                "ok",
                "proxy_dns_failed",
                "dns_failed",
                "connect_failed",
                "timeout",
                "tls_failed",
                "tls_verification_failed",
                "other_transport_error",
            },
        ),
        (
            "timeout_phase",
            {
                "response_body",
                "after_tls_before_headers",
                "tls_or_proxy_tunnel",
                "tcp_connect",
                "dns_or_connect",
            },
        ),
    ):
        value = details.get(field)
        if isinstance(value, str) and value in allowed:
            result[field] = value
    return result


def safe_http_error_code(provider: str, status: int, raw: bytes) -> str:
    """Persist only fixed diagnostic codes and response shapes, never provider prose."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return (
            "mistral_non_json_error"
            if provider == "mistral" and status == 429
            else "provider_http_error"
        )
    if not isinstance(data, dict):
        return (
            "mistral_non_object_error"
            if provider == "mistral" and status == 429
            else "provider_http_error"
        )
    if (
        provider == "groq"
        and status == 403
        and data.get("error_code") == 1010
        and data.get("error_name") == "browser_signature_banned"
    ):
        return "groq_edge_browser_signature_blocked"
    if (
        provider == "mistral"
        and status == 429
        and data.get("object") == "error"
        and data.get("type") in ("rate_limit_error", "rate_limited", "rate_limit_exceeded")
    ):
        return "mistral_rate_limit_error"
    if provider == "mistral" and status == 429:
        if data.get("object") == "error":
            return "mistral_error_type_unclassified"
        if isinstance(data.get("error"), dict):
            return "mistral_nested_error_unclassified"
        return "mistral_error_envelope_unclassified"
    if not isinstance(data.get("error"), dict):
        return "provider_http_error"
    error = data["error"]
    if (
        provider == "google"
        and status == 404
        and error.get("code") == 404
        and error.get("status") == "NOT_FOUND"
    ):
        return "google_not_found"
    if provider == "google" and status == 404 and error.get("code") == "model_not_found":
        return "google_model_not_found"
    if provider == "groq" and status == 403 and error.get("type") == "permissions_error":
        if error.get("code") == "model_permission_blocked_org":
            return "groq_model_blocked_org"
        if error.get("code") == "model_permission_blocked_project":
            return "groq_model_blocked_project"
    return "provider_http_error"


def _mistral_reason(data: object, sensitive_values: tuple[str, ...]) -> dict[str, str | bool]:
    """Classify a reported hint, not a verified cause or non-execution guarantee."""
    result: dict[str, str | bool] = {
        "reason_category": "http_429_unclassified",
        "reason_basis": "status_only",
        "next_check": "limits_or_provider_support",
    }
    if not isinstance(data, dict):
        return result
    error = data.get("error") if isinstance(data.get("error"), dict) else data
    assert isinstance(error, dict)
    message = error.get("message")
    if message is None:
        detail = error.get("detail")
        message = detail.get("message") if isinstance(detail, dict) else detail
    if isinstance(message, str) and len(message) <= 4096:
        # Remove exact resolved secrets before interpreting even fixed phrases.
        # No part of this text is returned, hashed or persisted.
        redacted = False
        for secret in sensitive_values:
            if secret and re.search(re.escape(secret), message, re.IGNORECASE):
                message = re.sub(re.escape(secret), "[redacted]", message, flags=re.IGNORECASE)
                redacted = True
        result["message_secret_redacted"] = redacted
        sentence = " ".join(message.casefold().split()).split(".", 1)[0].strip()
        patterns = (
            (
                "service_capacity_reported",
                r"service tier capacity exceeded(?: for this model)?",
                "check_provider_capacity",
            ),
            (
                "workspace_budget_reported",
                r"workspace (?:monthly )?(?:spending limit|budget) (?:exceeded|reached)",
                "check_workspace_cap",
            ),
            (
                "organization_budget_reported",
                r"organization (?:monthly )?(?:spending limit|budget) (?:exceeded|reached)",
                "check_organization_cap",
            ),
            (
                "monthly_token_limit_reported",
                r"(?:monthly token quota|monthly token limit|tokens per month limit) (?:exceeded|reached|exhausted)",
                "check_monthly_token_limit",
            ),
            (
                "token_rate_reported",
                r"(?:tokens? per minute|tpm)(?: limit)? (?:exceeded|reached)",
                "check_model_token_rate",
            ),
            (
                "request_rate_reported",
                r"(?:requests? per (?:second|minute)|rps|rpm)(?: limit)? (?:exceeded|reached)",
                "check_model_request_rate",
            ),
            (
                "rate_limit_scope_unknown",
                r"rate limit exceeded|too many requests",
                "check_model_rate_and_monthly_limits",
            ),
        )
        for category, pattern, action in patterns:
            if re.fullmatch(pattern, sentence):
                result.update(
                    reason_category=category, reason_basis="message_pattern", next_check=action
                )
                return result
    elif isinstance(message, str):
        result["message_exceeds_bound"] = True
    if error.get("type") in ("rate_limit_error", "rate_limited", "rate_limit_exceeded"):
        result.update(
            reason_category="rate_limit_scope_unknown",
            reason_basis="fixed_type",
            next_check="check_model_rate_and_monthly_limits",
        )
    return result


def _mistral_error_fields(
    data: dict[str, object], headers: dict[str, str], sensitive_values: tuple[str, ...]
) -> dict[str, str | int | bool | None]:
    """Keep bounded error evidence, never arbitrary text, identifiers or echoed content.

    Unknown code/type/param strings are not safe merely because they are short.
    Error prose is projected onto a fixed diagnostic vocabulary; all other words,
    numbers, URLs, addresses and credential-like strings are discarded.
    """
    result: dict[str, str | int | bool | None] = {}
    error = data.get("error") if isinstance(data.get("error"), dict) else data
    assert isinstance(error, dict)

    def reflected(value: str) -> bool:
        return any(secret and secret.casefold() in value.casefold() for secret in sensitive_values)

    for field, allowed in (
        ("object", {"error"}),
        (
            "type",
            {
                "invalid_request_error",
                "authentication_error",
                "rate_limit_error",
                "rate_limited",
                "rate_limit_exceeded",
                "server_error",
            },
        ),
        ("param", {"model", "max_tokens", "reasoning_effort", "messages", "stream"}),
        ("code", {"unknown_model"}),
    ):
        value = error.get(field)
        safe: str | int | None = None
        if isinstance(value, str) and not reflected(value):
            if value in allowed:
                safe = value
            elif field == "code" and re.fullmatch(r"[0-9]{1,5}", value):
                safe = int(value)
        elif (
            field == "code"
            and type(value) is int
            and 0 <= value <= 99_999
            and not reflected(str(value))
        ):
            safe = value
        result["provider_error_" + field] = safe
        result["provider_error_" + field + "_present"] = value is not None

    message = error.get("message")
    if message is None:
        detail = error.get("detail")
        message = detail.get("message") if isinstance(detail, dict) else detail
    if isinstance(message, str):
        # Do not cut a long word/identifier into an apparently allowlisted prefix.
        if len(message) > 4096:
            result["provider_error_message_safe"] = "[omitted: exceeds bound]"
            result["provider_error_message_redacted"] = True
        else:
            redacted = False
            for secret in sensitive_values:
                if secret and re.search(re.escape(secret), message, re.IGNORECASE):
                    message = re.sub(re.escape(secret), "[redacted]", message, flags=re.IGNORECASE)
                    redacted = True
            # Remove grouped credentials, quoted echoes and PII before word projection.
            message, changes = re.subn(
                r"\b(?:bearer|basic)\s+\S+|https?://\S+|\S+@\S+|[\"'][^\"']*[\"']",
                "[redacted]",
                message,
                flags=re.IGNORECASE,
            )
            redacted |= bool(changes)
            vocabulary = set(
                "a an the this for of to and or is are has have been be on in per "  # noqa: SIM905
                "rate limit limits exceeded reached exhausted too many requests request "
                "second seconds minute minutes month monthly tokens token quota quotas "
                "tpm rps rpm workspace organization budget spending service tier capacity "
                "insufficient unavailable temporarily please try again later maximum "
                "model models invalid unknown parameter parameters authentication "
                "unauthorized forbidden api key credits credit balance billing free account "
                "access denied disabled enabled completion completions server error "
                "not found supported required current available usage throughput concurrent".split()
            )
            words = []
            for word in message.casefold().split():
                normalized = word.strip(".,:;!?()")
                if normalized in vocabulary:
                    words.append(normalized)
                else:
                    redacted = True
                    if not words or words[-1] != "[redacted]":
                        words.append("[redacted]")
            projected = " ".join(words)
            result["provider_error_message_safe"] = projected[:256]
            result["provider_error_message_redacted"] = redacted or len(projected) > 256

    # Exact recognized names only. Values are bounded numeric counters/reset seconds,
    # not raw headers. Their presence is evidence, not proof of a particular bucket.
    names = tuple(f"x-ratelimit-{kind}" for kind in ("limit", "remaining", "reset")) + tuple(
        f"x-ratelimit-{kind}-{metric}{window}"
        for kind in ("limit", "remaining", "reset")
        for metric in ("req", "requests", "token", "tokens")
        for window in ("", "-second", "-10-second", "-minute", "-hour", "-day", "-month")
    )
    for name in names:
        value = headers.get(name)
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) and not reflected(value):
            result[name.replace("-", "_")] = int(value)
    for name, pattern in (
        (
            "mistral-correlation-id",
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        ),
        ("x-kong-request-id", r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
        ("cf-ray", r"[0-9a-fA-F]{16,32}-[A-Z]{3}"),
    ):
        value = headers.get(name)
        if isinstance(value, str) and re.fullmatch(pattern, value) and not reflected(value):
            result[name.replace("-", "_")] = value
    return result


def safe_response_diagnostics(
    provider: str,
    status: int,
    headers: dict[str, str],
    raw: bytes,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, str | int | bool | None]:
    """Bounded allowlisted diagnostics; Mistral error text uses a fixed vocabulary."""
    normalized = {name.lower(): value for name, value in headers.items()}
    mime = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    diagnostics: dict[str, str | int | bool | None] = {
        "content_type": mime
        if mime in {"application/json", "text/html", "text/event-stream"}
        else "other",
        "retry_after_seconds": parse_retry_after(normalized.get("retry-after"), datetime.now(UTC)),
    }
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        diagnostics["body_shape"] = "non_json"
        if provider == "mistral" and status >= 400:
            diagnostics.update(_mistral_error_fields({}, normalized, sensitive_values))
        if provider == "mistral" and status == 429:
            diagnostics.update(_mistral_reason(None, sensitive_values))
        return diagnostics
    if provider == "mistral" and status == 429:
        diagnostics.update(_mistral_reason(data, sensitive_values))
    if not isinstance(data, dict):
        diagnostics["body_shape"] = "non_object"
        return diagnostics
    if provider == "mistral" and status >= 400:
        diagnostics.update(_mistral_error_fields(data, normalized, sensitive_values))
    if provider == "nvidia" and status == 200:
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        first = first if isinstance(first, dict) else {}
        finish = first.get("finish_reason")
        diagnostics["finish_reason"] = (
            finish
            if isinstance(finish, str) and finish in {"stop", "length", "content_filter"}
            else "missing"
            if finish is None
            else "unclassified"
        )
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        diagnostics["visible_answer_present"] = isinstance(content, str) and bool(content.strip())
        usage = data.get("usage")
        diagnostics["provider_usage_complete"] = isinstance(usage, dict) and all(
            isinstance(usage.get(field), int)
            and not isinstance(usage.get(field), bool)
            and usage[field] >= 0
            for field in ("prompt_tokens", "completion_tokens")
        )
    error = data if data.get("object") == "error" else data.get("error")
    diagnostics["body_shape"] = (
        "top_level_error"
        if data.get("object") == "error"
        else "nested_error"
        if isinstance(error, dict)
        else "other_object"
    )
    if isinstance(error, dict):
        kind = error.get("type")
        diagnostics["error_type"] = (
            kind
            if isinstance(kind, str)
            and kind
            in {
                "invalid_request_error",
                "authentication_error",
                "rate_limit_error",
                "rate_limited",
                "rate_limit_exceeded",
                "server_error",
            }
            else "unclassified"
        )
        diagnostics["error_code_present"] = error.get("code") is not None
        diagnostics["error_param_is_model"] = error.get("param") == "model"
    if status != 200:
        diagnostics["error_code"] = safe_http_error_code(provider, status, raw)
    return diagnostics


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
    provider: str, status: int, raw: bytes, *, model_id: str | None = None
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
            if (
                provider in {"groq", "mistral"}
                or model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
            ) and (
                input_tokens is None
                or output_tokens is None
                or (
                    "total_tokens" in usage
                    and (
                        _nonnegative(usage["total_tokens"]) is None
                        or usage["total_tokens"] != input_tokens + output_tokens
                    )
                )
            ):
                # A missing/contradictory pair cannot settle a token hold.
                input_tokens = output_tokens = None
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if (
                isinstance(message, dict)
                and isinstance(message.get("content"), str)
                and message["content"].strip()
            ):
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
        result = data.get("result")
        # The Workers AI REST envelope puts model output, including optional
        # usage, under result. Some routes expose usage at the outer level.
        outer_usage = data.get("usage")
        nested_usage = result.get("usage") if isinstance(result, dict) else None
        usage = nested_usage if isinstance(nested_usage, dict) else outer_usage
        if (
            isinstance(outer_usage, dict)
            and isinstance(nested_usage, dict)
            and outer_usage != nested_usage
        ):
            usage = None
        if isinstance(usage, dict):
            input_tokens = _nonnegative(usage.get("prompt_tokens"))
            output_tokens = _nonnegative(usage.get("completion_tokens"))
            neurons = _nonnegative(usage.get("neurons"))
        if isinstance(result, dict) and isinstance(result.get("response"), str):
            answer = result["response"]
        request_id = _request_id(data.get("request_id"))
    return (answer if status == 200 else None, input_tokens, output_tokens, neurons, request_id)
