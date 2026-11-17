"""Fixed official provider requests for the unified gateway. No persistence here."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from .catalog import MODELS, endpoint
from .client import _NoRedirect
from .provider_policy import catalog_model_id_reason
from .retry import parse_retry_after

MAX_RESPONSE_BYTES = 262_144
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
        elif model_id == "z-ai/glm-5.3-flash":
            payload["chat_template_kwargs"] = {"clear_thinking": True}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return url, {"Authorization": "Bearer " + secret}, payload
    if provider in {"groq", "mistral", "openrouter"}:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        }
        if provider == "openrouter":
            from .provider_policy import openrouter_free_policy

            payload["provider"] = openrouter_free_policy()
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
                "options": {"rejectIfBusy": True},
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
    ) and not any(
        re.fullmatch(
            re.escape(model.endpoint_template).replace(
                re.escape("{account_id}"), r"[A-Za-z0-9_-]+"
            ),
            url,
        )
        for model in MODELS.values()
        if model.provider == "cloudflare"
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


def _reported_model_metadata(
    provider: str,
    status: int,
    data: dict,
    requested_model: str | None,
    sensitive_values: tuple[str, ...],
    *,
    duplicate_fields: bool = False,
) -> dict[str, str | None]:
    """Persist only recognized public identities from documented response fields.

    A requested identity never fills in a missing response field. Unknown aliases
    remain unknown rather than allowing arbitrary prose or identifiers into the DB.
    This is the provider's claim, not independent verification of model execution.
    """
    result: dict[str, str | None] = {
        "provider_reported_model": None,
        "provider_model_basis": "unknown",
        "provider_model_field": None,
        "provider_model_status": "unknown",
    }
    field = "modelVersion" if provider == "google" else "model"
    if provider not in {"nvidia", "groq", "mistral", "openrouter", "google"}:
        result["provider_model_status"] = "not_reported_by_contract"
        return result
    if status != 200:
        result["provider_model_status"] = "not_success"
        return result
    if duplicate_fields:
        result["provider_model_status"] = "ambiguous_json"
        return result
    if field not in data:
        result["provider_model_status"] = "missing"
        return result
    value = data[field]
    if catalog_model_id_reason(value) is not None:
        result["provider_model_status"] = "invalid"
        return result
    assert isinstance(value, str)
    if any(private and private.casefold() in value.casefold() for private in sensitive_values):
        result["provider_model_status"] = "redacted"
        return result
    known = {model.model for model in MODELS.values() if model.provider == provider}
    if provider == "groq":
        # Both GPT OSS identities are published in Groq's official model list.
        known.add("openai/gpt-oss-120b")
    if requested_model is not None and catalog_model_id_reason(requested_model) is None:
        known.add(requested_model)
        if provider == "openrouter" and requested_model.endswith(":free"):
            known.add(requested_model.removesuffix(":free"))
    recognized = value in known
    if not recognized and requested_model and provider in {"google", "mistral"}:
        stem = requested_model.removesuffix("-latest")
        # Dated aliases may add numeric version tags, never arbitrary words.
        recognized = bool(re.fullmatch(re.escape(stem) + r"(?:-\d{1,8}){1,4}", value))
        if provider == "mistral" and requested_model == "ministral-3b-latest":
            recognized |= bool(re.fullmatch(r"ministral-3-3b-\d{4}(?:-\d{2})?", value))
    if not recognized:
        result["provider_model_status"] = "unrecognized"
        return result
    result.update(
        provider_reported_model=value,
        provider_model_basis="provider_response",
        provider_model_field=field,
        provider_model_status="reported",
    )
    return result


def safe_response_diagnostics(
    provider: str,
    status: int,
    headers: dict[str, str],
    raw: bytes,
    *,
    sensitive_values: tuple[str, ...] = (),
    requested_model: str | None = None,
) -> dict[str, str | int | bool | None]:
    """Bounded allowlisted diagnostics; Mistral error text uses a fixed vocabulary."""
    normalized = {name.lower(): value for name, value in headers.items()}
    mime = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    diagnostics: dict[str, str | int | bool | None] = {
        "content_type": mime
        if mime in {"application/json", "text/html", "text/event-stream"}
        else "other",
        "retry_after_seconds": parse_retry_after(normalized.get("retry-after"), datetime.now(UTC)),
        "http_category": "success"
        if status == 200
        else "authentication_rejected"
        if status == 401
        else "access_rejected"
        if status == 403
        else "not_found"
        if status == 404
        else "rate_limited_unclassified"
        if status == 429
        else "server_error"
        if status >= 500
        else "other_response",
    }
    diagnostics.update(_reported_model_metadata(provider, status, {}, None, ()))
    duplicate_fields = False

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate_fields
        result = {}
        for key, value in items:
            duplicate_fields |= key in result
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, TypeError, RecursionError):
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
    diagnostics.update(
        _reported_model_metadata(
            provider,
            status,
            data,
            requested_model,
            sensitive_values,
            duplicate_fields=duplicate_fields,
        )
    )
    if provider == "google" and status == 200:
        candidates = data.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 else None
        finish = candidate.get("finishReason") if isinstance(candidate, dict) else None
        diagnostics["provider_finish_reason"] = (
            (finish if finish in {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER"} else None)
            if isinstance(finish, str) and not duplicate_fields
            else None
        )
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
    if provider == "cloudflare":
        diagnostics.update(_cloudflare_shape_diagnostics(data, duplicate_fields, requested_model))
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


CLOUDFLARE_DIRECT_CHAT_MODELS = frozenset({"@cf/google/gemma-4-26b-a4b-it"})


def cloudflare_text_envelope(
    body: dict[str, Any], model_id: str | None, *, require_success: bool = False
) -> dict[str, Any] | None:
    """Select one documented envelope; direct output is model-scoped, never fallback."""
    if "error" in body or body.get("errors") or body.get("success") is False:
        return None
    if "result" in body:
        if (require_success and body.get("success") is not True) or any(
            key in body for key in ("id", "choices", "response", "object", "created", "model")
        ):
            return None
        value = body["result"]
        if (
            not isinstance(value, dict)
            or "error" in value
            or value.get("errors")
            or value.get("success") is False
        ):
            return None
        return value
    if (
        model_id not in CLOUDFLARE_DIRECT_CHAT_MODELS
        or any(key in body for key in ("success", "errors", "response"))
        or _request_id(body.get("id")) is None
        or body.get("object") != "chat.completion"
        or type(body.get("created")) is not int
        or not 0 <= body["created"] <= 10**12
        or catalog_model_id_reason(body.get("model")) is not None
    ):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    message = choice.get("message")
    if (
        type(choice.get("index")) is not int
        or choice["index"] != 0
        or not isinstance(choice.get("finish_reason"), str)
        or choice.get("finish_reason") not in {"stop", "length"}
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
    ):
        return None
    return body


def _cloudflare_shape_diagnostics(
    body: dict[str, Any], duplicate_fields: bool, requested_model: str | None
) -> dict[str, str | bool]:
    """Fixed shape vocabulary only: never values, body keys, IDs, or error prose."""
    wrapped = "result" in body
    result = body.get("result") if wrapped else body
    item = result if isinstance(result, dict) else {}
    mixed = wrapped and any(
        key in body for key in ("id", "choices", "response", "object", "created", "model")
    )
    envelope = (
        "duplicate_fields"
        if duplicate_fields
        else "error"
        if "error" in body or body.get("errors") or body.get("success") is False
        else "mixed"
        if mixed
        else "result"
        if wrapped
        else "direct_chat_completion"
        if body.get("object") == "chat.completion"
        else "other_object"
    )
    choices = item.get("choices")
    choice_shape = (
        "missing"
        if "choices" not in item
        else "non_list"
        if not isinstance(choices, list)
        else "empty"
        if not choices
        else "one"
        if len(choices) == 1
        else "multiple"
    )
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, dict) else None
    message = message if isinstance(message, dict) else {}
    legacy = "response" in item and "choices" not in item
    content = item.get("response") if legacy else message.get("content")
    present = "response" in item if legacy else "content" in message
    content_shape = (
        "missing"
        if not present
        else "null"
        if content is None
        else "text"
        if isinstance(content, str) and content.strip()
        else "empty_text"
        if isinstance(content, str)
        else "other"
    )
    outer, nested = body.get("usage"), item.get("usage")
    usage_shape = (
        "conflicting"
        if isinstance(outer, dict) and isinstance(nested, dict) and outer != nested
        else "invalid"
        if any(v is not None and not isinstance(v, dict) for v in (outer, nested))
        else "object"
        if isinstance(outer, dict) or isinstance(nested, dict)
        else "missing"
    )
    candidate = cloudflare_text_envelope(body, requested_model, require_success=True)
    return {
        "cloudflare_text_schema_valid": not duplicate_fields
        and candidate is not None
        and cloudflare_result_text(candidate) is not None,
        "cloudflare_envelope": envelope,
        "cloudflare_choice_shape": choice_shape,
        "cloudflare_content_shape": content_shape,
        "cloudflare_nontext_message": bool(
            message.get("tool_calls")
            or message.get("function_call")
            or message.get("refusal") is not None
            or message.get("audio")
        ),
        "cloudflare_usage_shape": usage_shape,
    }


def cloudflare_result_text(result: object) -> str | None:
    """Two documented native text schemas, with no ambiguous mixed fallback."""
    if not isinstance(result, dict):
        return None
    if "choices" not in result:
        answer = result.get("response")
        return answer if isinstance(answer, str) else None
    if "response" in result:
        return None
    choices = result["choices"]
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        return None
    if (
        message.get("tool_calls")
        or message.get("function_call") is not None
        or message.get("refusal") is not None
        or message.get("audio") is not None
    ):
        return None
    answer = message.get("content")
    return answer if isinstance(answer, str) and answer.strip() else None


def cloudflare_result_usage(body: dict[str, Any], result: object) -> dict[str, Any] | None:
    """Only explicit, nonconflicting counters; absence is never zero usage."""
    outer = body.get("usage")
    nested = result.get("usage") if isinstance(result, dict) else None
    if isinstance(outer, dict) and isinstance(nested, dict) and outer != nested:
        return None
    value = nested if isinstance(nested, dict) else outer
    return value if isinstance(value, dict) else None


def reported_usage(provider: str, status: int, raw: bytes) -> dict[str, int]:
    """Read explicit counters only; a prompt/output pair never invents a total."""
    if status != 200 or provider not in {
        "google",
        "cloudflare",
        "nvidia",
        "groq",
        "mistral",
        "openrouter",
        "openai_chat",
    }:
        return {}
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("provider usage invalid")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProviderError("provider usage invalid")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ProviderError("provider usage invalid")

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (TypeError, ValueError, RecursionError):
        raise ProviderError("provider usage invalid") from None
    if not isinstance(data, dict):
        raise ProviderError("provider usage invalid")
    if provider == "cloudflare":
        nested = data.get("result")
        inner = nested.get("usage") if isinstance(nested, dict) else None
        outer = data.get("usage")
        if any(value is not None and not isinstance(value, dict) for value in (outer, inner)):
            raise ProviderError("provider usage invalid")
        usage = cloudflare_result_usage(data, nested)
        if usage is None and (inner is not None or outer is not None):
            raise ProviderError("provider usage invalid")
    else:
        usage = data.get("usageMetadata" if provider == "google" else "usage")
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        raise ProviderError("provider usage invalid")
    fields = (
        {
            "promptTokenCount": "input_tokens",
            "candidatesTokenCount": "output_tokens",
            "totalTokenCount": "total_tokens",
        }
        if provider == "google"
        else {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
            "total_tokens": "total_tokens",
            **({"neurons": "neurons"} if provider == "cloudflare" else {}),
        }
    )
    result = {}
    for field in (*fields, *(("thoughtsTokenCount",) if provider == "google" else ())):
        value = usage.get(field)
        if value is None:
            continue
        if type(value) is not int or not 0 <= value <= 10**12:
            raise ProviderError("provider usage invalid")
        if field in fields:
            result[fields[field]] = value
    if "total_tokens" in result:
        pair = (result.get("input_tokens"), result.get("output_tokens"))
        if provider == "google":
            # Google totals may also include thinking and other token categories.
            if result["total_tokens"] < sum(value for value in pair if value is not None) + (
                usage.get("thoughtsTokenCount") or 0
            ):
                raise ProviderError("provider usage invalid")
        elif any(value is None for value in pair) or result["total_tokens"] != sum(
            value for value in pair if value is not None
        ):
            raise ProviderError("provider usage invalid")
    return result


def interpret(
    provider: str, status: int, raw: bytes, *, model_id: str | None = None
) -> tuple[str | None, int | None, int | None, int | None, str | None]:
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("ambiguous provider JSON")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError("invalid provider JSON")

    try:
        data = (
            json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
            if provider == "cloudflare"
            else json.loads(raw)
        )
    except (ValueError, TypeError, RecursionError):
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
                or model_id in {"nvidia/nemotron-3.5-lightning-30b-a3b", "z-ai/glm-5.3-flash"}
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
        result = cloudflare_text_envelope(data, model_id)
        if result is None:
            return (None, None, None, None, None)
        # The Workers AI REST envelope puts model output, including optional
        # usage, under result. Some routes expose usage at the outer level.
        usage = cloudflare_result_usage(data, result)
        if isinstance(usage, dict):
            input_tokens = _nonnegative(usage.get("prompt_tokens"))
            output_tokens = _nonnegative(usage.get("completion_tokens"))
            neurons = _nonnegative(usage.get("neurons"))
            if "total_tokens" in usage and (
                input_tokens is None
                or output_tokens is None
                or _nonnegative(usage["total_tokens"]) != input_tokens + output_tokens
            ):
                input_tokens = output_tokens = None
        if data.get("success") is not False and not data.get("errors"):
            answer = cloudflare_result_text(result)
        request_id = _request_id(data.get("request_id"))
        if request_id is None and isinstance(result, dict):
            request_id = _request_id(result.get("id"))
    return (answer if status == 200 else None, input_tokens, output_tokens, neurons, request_id)
