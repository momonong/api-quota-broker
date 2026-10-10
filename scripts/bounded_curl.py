"""Fixed NVIDIA diagnostic wrapper over the packaged bounded curl primitives."""

import json

from quota_broker.bounded_curl import (
    MARKER,
    TIMINGS,
    _quote,
    metrics_template,
    parse_curl_result,
    run_curl,
)
from quota_broker.gateway_providers import ProviderError

URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
PROMPT = "Diagnostic stage v1 remaining: reply with the single word READY."
__all__ = ["MARKER", "TIMINGS", "nvidia_http", "run_curl"]


def nvidia_http(
    headers: dict[str, str], payload: dict[str, object], timeout: float = 120
) -> tuple[int | None, dict[str, str], bytes, dict[str, int | str | None]]:
    if set(headers) != {"Authorization"} or not headers["Authorization"].startswith("Bearer "):
        raise ProviderError("unapproved curl headers")
    if payload.get("model") != MODEL or payload.get("stream") is not False:
        raise ProviderError("unapproved diagnostic model")
    if type(payload.get("max_tokens")) is not int or not 1 <= payload["max_tokens"] <= 32:
        raise ProviderError("unapproved diagnostic output bound")
    if payload != {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": payload["max_tokens"],
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }:
        raise ProviderError("unapproved diagnostic payload")
    if not 0 < timeout <= 120:
        raise ProviderError("unapproved diagnostic timeout")
    write_out = metrics_template()
    options = [
        "url = " + _quote(URL),
        'request = "POST"',
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        'connect-timeout = "10"',
        "max-time = " + _quote(str(timeout)),
        "include",
        "suppress-connect-headers",
        'header = "Content-Type: application/json"',
        'header = "Accept: application/json"',
        "header = " + _quote("Authorization: " + headers["Authorization"]),
        "data-binary = " + _quote(json.dumps(payload, separators=(",", ":"), ensure_ascii=True)),
        "write-out = " + _quote(write_out),
    ]
    code, output = run_curl(("\n".join(options) + "\n").encode(), timeout)
    return parse_curl_result(code, output, 123)
