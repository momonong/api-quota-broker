"""One independent, bounded translation probe; never replays the Gemma smoke."""

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quota_broker.core import canonical
from quota_broker.nvidia import NoRedirect, doppler_resolver_from_token
from scripts.nvidia_smoke_once import _claim_receipt, _content, _mark, _request_id, _usage
from scripts.verify_doppler_executor_read import (
    CONFIG,
    PROJECT,
    SECRET_NAME,
    checked_metadata,
    cli,
    create_service_token,
)

MODEL = "nvidia/riva-translate-4b-instruct-v2"
URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL_PAGE = "https://build.nvidia.com/nvidia/riva-translate-4b-instruct-v2"
SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "zh-cn"
TEXT = "Hello."
MAX_OUTPUT_TOKENS = 16
TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 65_536
RECEIPT = Path(__file__).resolve().parents[1] / ".state" / "nvidia-riva-translate-once.json"


def _safe_id(value: str | None) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        return value
    return None


def _error_code(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return _safe_id(code) if isinstance(code, str) else None


def riva_transport(
    key: str, on_headers: Callable[[int, str | None], None]
) -> tuple[int, dict[str, Any]]:
    """Send exactly one fixed HTTPS POST with no redirect or provider retry."""
    body = canonical(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": f"{SOURCE_LANGUAGE}-{TARGET_LANGUAGE}"},
                {"role": "user", "content": TEXT},
            ],
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        headers = response.headers
        header_id = _safe_id(
            headers.get("NVCF-REQID") or headers.get("X-Request-ID") or headers.get("Request-ID")
        )
        on_headers(status, header_id)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("provider response too large")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return status, {}
    return status, parsed if isinstance(parsed, dict) else {}


def run_once(
    binary: str,
    receipt: Path = RECEIPT,
    transport: Callable[
        [str, Callable[[int, str | None], None]], tuple[int, dict[str, Any]]
    ] = riva_transport,
) -> dict[str, Any]:
    if receipt.exists():
        raise RuntimeError("Independent probe receipt already exists; never replay")
    if not checked_metadata(binary):
        raise RuntimeError("NVIDIA_API_KEY metadata name is missing")
    data = _claim_receipt(receipt, MODEL)
    try:
        token = create_service_token(binary)
        key = doppler_resolver_from_token(token, PROJECT, CONFIG)(SECRET_NAME)
    except Exception:
        _mark(receipt, data, "preflight_failed")
        raise

    _mark(receipt, data, "dispatching", transport_stage="opening_response")

    def on_headers(status: int, request_id: str | None) -> None:
        fields: dict[str, Any] = {"transport_stage": "reading_body", "http_status": status}
        if request_id is not None:
            fields["request_id"] = request_id
        _mark(receipt, data, "dispatching", **fields)

    try:
        status, response = transport(key, on_headers)
    except Exception as exc:  # noqa: BLE001 - outcome after dispatch is always uncertain
        error = type(exc).__name__
        _mark(receipt, data, "unknown", transport_error=error)
        return {
            "state": "unknown",
            "transport_error": error,
            "transport_stage": data["transport_stage"],
            "http_status": data.get("http_status"),
            "request_id": data.get("request_id"),
        }

    usage = _usage(response)
    request_id = _request_id(response) or data.get("request_id")
    if status == 202:
        _mark(receipt, data, "pending_unknown", http_status=status, request_id=request_id)
        return {"state": "pending_unknown", "http_status": status, "request_id": request_id}
    if status != 200:
        code = _error_code(response)
        _mark(receipt, data, "http_error", http_status=status, provider_error_code=code)
        return {"state": "http_error", "http_status": status, "provider_error_code": code}
    content = _content(response)
    if not content:
        _mark(receipt, data, "unknown", http_status=status, usage=usage)
        return {"state": "unknown", "http_status": status, "usage": usage}
    state = "completed" if usage is not None else "completed_usage_unknown"
    _mark(receipt, data, state, http_status=status, usage=usage, text_present=True)
    return {
        "state": state,
        "http_status": status,
        "usage": usage,
        "translation": content[:200],
        "text_truncated": len(content) > 200,
    }


def main() -> int:
    if not sys.stdin.isatty():
        print("interactive TTY required")
        return 1
    print("Independent translation probe:", MODEL)
    print("Official free endpoint evidence:", MODEL_PAGE)
    print("One HTTPS POST:", SOURCE_LANGUAGE, "to", TARGET_LANGUAGE, repr(TEXT))
    print(
        "max output tokens:", MAX_OUTPUT_TOKENS, "timeout per blocking operation:", TIMEOUT_SECONDS
    )
    print("Doppler:", PROJECT + "/" + CONFIG + "/" + SECRET_NAME)
    print("Creates one config-scoped read-only Service Token with 5m expiry.")
    print("The old Gemma unknown receipt remains untouched. No provider retry.")
    print("Token and provider key stay in process memory and are not printed or saved.")
    if input("Type RIVA-ONE-SHOT after approval: ").strip() != "RIVA-ONE-SHOT":
        print("cancelled")
        return 1
    try:
        result = run_once(cli())
    except (RuntimeError, ValueError, OSError) as exc:
        print("probe_preflight_failed:", type(exc).__name__)
        print("temporary_access_may_exist: check Doppler Access metadata before any new attempt")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] in {"completed", "completed_usage_unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
