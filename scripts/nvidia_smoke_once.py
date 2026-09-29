"""One local NVIDIA inference attempt with a short-lived Doppler read credential.

Run as a module from the repository root. The non-secret receipt is deliberately
durable: an interrupted or uncertain dispatch must never be replayed.
"""

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quota_broker.nvidia import (
    MODEL,
    doppler_resolver_from_token,
    nvidia_transport,
)
from scripts.verify_doppler_executor_read import (
    CONFIG,
    PROJECT,
    SECRET_NAME,
    checked_metadata,
    cli,
    create_service_token,
)

PROMPT = "Reply with OK."
MAX_OUTPUT_TOKENS = 16
RECEIPT = Path(__file__).resolve().parents[1] / ".state" / "nvidia-smoke-once.json"
MODEL_PAGE = "https://build.nvidia.com/google/gemma-4-31b-it"


def _receipt_write(path: Path, data: dict[str, Any], *, new: bool = False) -> None:
    flags = os.O_WRONLY | os.O_NOFOLLOW
    flags |= os.O_CREAT | os.O_EXCL if new else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _claim_receipt(path: Path) -> dict[str, Any]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if path.parent.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError("Private receipt directory required")
    data: dict[str, Any] = {
        "model": MODEL,
        "state": "claimed",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _receipt_write(path, data, new=True)
    return data


def _mark(path: Path, data: dict[str, Any], state: str, **fields: Any) -> None:
    data.update(fields)
    data["state"] = state
    data["updated_at"] = datetime.now(UTC).isoformat()
    _receipt_write(path, data)


def _usage(response: dict[str, Any]) -> dict[str, int] | None:
    value = response.get("usage")
    if not isinstance(value, dict):
        return None
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if type(prompt) is int and prompt >= 0 and type(completion) is int and completion >= 0:
        return {"prompt_tokens": prompt, "completion_tokens": completion}
    return None


def _content(response: dict[str, Any]) -> str | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    value = message.get("content")
    return value if isinstance(value, str) else None


def _request_id(response: dict[str, Any]) -> str | None:
    value = response.get("requestId")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return None


def run_once(
    binary: str,
    receipt: Path = RECEIPT,
    transport: Callable[[str, str, int], tuple[int, dict[str, Any]]] = nvidia_transport,
) -> dict[str, Any]:
    if receipt.exists():
        raise RuntimeError("Smoke receipt already exists; never replay an uncertain request")
    if not checked_metadata(binary):
        raise RuntimeError("NVIDIA_API_KEY metadata name is missing")
    data = _claim_receipt(receipt)
    try:
        token = create_service_token(binary)
        resolver = doppler_resolver_from_token(token, PROJECT, CONFIG)
        key = resolver(SECRET_NAME)  # Exactly one read. Neither value is printed or persisted.
    except Exception:
        _mark(receipt, data, "preflight_failed")
        raise
    _mark(receipt, data, "dispatching")
    try:
        status, response = transport(key, PROMPT, MAX_OUTPUT_TOKENS)
    except Exception as exc:  # noqa: BLE001 - any post-dispatch failure is unknown
        _mark(receipt, data, "unknown", transport_error=type(exc).__name__)
        return {"state": "unknown", "transport_error": type(exc).__name__}

    usage = _usage(response)
    result: dict[str, Any] = {"http_status": status, "usage": usage}
    if status == 202:
        request_id = _request_id(response)
        _mark(receipt, data, "pending_unknown", http_status=status, request_id=request_id)
        return result | {"state": "pending_unknown", "request_id": request_id}
    if status != 200:
        _mark(receipt, data, "http_error", http_status=status)
        return result | {"state": "http_error"}
    content = _content(response)
    if not content or usage is None:
        _mark(receipt, data, "unknown", http_status=status, usage=usage)
        return result | {"state": "unknown", "text_present": bool(content)}
    _mark(receipt, data, "completed", http_status=status, usage=usage)
    # The fixed prompt contains no private data; output is reduced to safe evidence.
    return result | {
        "state": "completed",
        "text_present": True,
        "answer_is_ok": content.strip() == "OK." or content.strip() == "OK",
    }


def main() -> int:
    if not sys.stdin.isatty():
        print("interactive TTY required")
        return 1
    print("Model:", MODEL)
    print("Official free endpoint evidence:", MODEL_PAGE)
    print("One HTTPS POST; prompt:", PROMPT, "max output tokens:", MAX_OUTPUT_TOKENS)
    print("Doppler:", PROJECT + "/" + CONFIG + "/" + SECRET_NAME)
    print("Creates one config-scoped read-only Service Token with 5m expiry.")
    print("Token, provider key, prompt and response text are not saved in the receipt.")
    print("Account billing status: unknown; stop if any account evidence contradicts free use.")
    print("No retry; an uncertain dispatch remains blocked by the receipt.")
    if (
        input("Type NVIDIA-ONE-SHOT only if no contradictory billing evidence exists: ").strip()
        != "NVIDIA-ONE-SHOT"
    ):
        print("cancelled")
        return 1
    try:
        result = run_once(cli())
    except (RuntimeError, ValueError, OSError) as exc:
        print("smoke_preflight_failed:", type(exc).__name__)
        print("temporary_access_may_exist: check Doppler Access metadata before any new attempt")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
