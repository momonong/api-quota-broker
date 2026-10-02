"""Bounded curl subprocess and response parsing; secrets and bodies stay in pipes.

Uses the existing curl binary, not a shell, with its default identity and TLS
verification. No retries, redirects, config files, logs, or on-disk response.
"""

import json
import math
import os
import re
import selectors
import subprocess
import time

from .gateway_providers import (
    MAX_RESPONSE_BYTES,
    ProviderError,
    ProviderPhaseTimeout,
    official_request,
)

MARKER = b"\nquota-broker-curl-metrics:"
TIMINGS = ("time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total")
OUTPUT_BOUND = MAX_RESPONSE_BYTES + 16_384
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-20b"


def groq_http(
    url: str, headers: dict[str, str], payload: dict[str, object], timeout: float
) -> tuple[int, dict[str, str], bytes]:
    """The official Groq POST only; fixed contract and no alternate transport fallback."""
    if url != GROQ_URL or set(headers) != {"Authorization"}:
        raise ProviderError("unapproved Groq transport")
    auth = headers["Authorization"]
    if not re.fullmatch(r"Bearer [A-Za-z0-9._~-]{8,256}", auth):
        raise ProviderError("credential_format_rejected")
    messages = payload.get("messages")
    first = messages[0] if isinstance(messages, list) and len(messages) == 1 else None
    content = first.get("content") if isinstance(first, dict) else None
    output = payload.get("max_completion_tokens")
    if (
        not isinstance(content, str)
        or not 0 < len(content.encode("utf-8")) <= 32_768
        or type(output) is not int
        or not 1 <= output <= 4096
        or not 0 < timeout <= 30
    ):
        raise ProviderError("unapproved Groq request bound")
    expected = official_request(
        "groq", GROQ_MODEL, "fixture", "fixture-key", content, output, None, None
    )[2]
    if payload != expected:
        raise ProviderError("unapproved Groq payload")
    options = [
        "url = " + _quote(GROQ_URL),
        'request = "POST"',
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        "connect-timeout = " + _quote(str(min(10, timeout))),
        "max-time = " + _quote(str(timeout)),
        "include",
        "suppress-connect-headers",
        'header = "Content-Type: application/json"',
        'header = "Accept: application/json"',
        "header = " + _quote("Authorization: " + auth),
        "data-binary = " + _quote(json.dumps(payload, separators=(",", ":"), ensure_ascii=True)),
        "write-out = " + _quote(metrics_template()),
    ]
    code, output_bytes = run_curl(("\n".join(options) + "\n").encode(), timeout)
    status, received, raw, timing = parse_curl_result(code, output_bytes, timeout + 3)
    if status is None or timing["transport_code"] != "ok":
        raise ProviderPhaseTimeout("groq_transport_" + str(timing["transport_code"]))
    return status, received, raw


def _quote(value: str) -> str:
    # curl config quoting is not shell/JSON quoting. Reject controls, then escape
    # the only two config delimiters. JSON payload already contains escaped LF.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderError("invalid curl config value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def metrics_template() -> str:
    fields = {"http_code": "%{http_code}", "exitcode": "%{exitcode}"}
    fields.update({name: "%{" + name + "}" for name in TIMINGS})
    template = (
        MARKER.decode().replace("\n", "\\n")
        + "{"
        + ",".join(json.dumps(name) + ":" + value for name, value in fields.items())
        + "}"
    )
    return template.replace('"http_code":%{http_code}', '"http_code":"%{http_code}"')


def run_curl(config: bytes, timeout: float) -> tuple[int, bytes]:
    """Bound output while draining, and kill on deadline. stderr is discarded."""
    with subprocess.Popen(
        ["/usr/bin/curl", "--disable", "--silent", "--config", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(config)
            process.stdin.close()
            buffer = bytearray()
            deadline = time.monotonic() + timeout + 3
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderPhaseTimeout("curl_process_deadline")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer.extend(chunk)
                        if len(buffer) > OUTPUT_BOUND:
                            raise ProviderError("curl_output_bound")
            try:
                return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(buffer)
            except subprocess.TimeoutExpired:
                raise ProviderPhaseTimeout("curl_process_deadline") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def parse_curl_result(
    code: int, output: bytes, max_time: float
) -> tuple[int | None, dict[str, str], bytes, dict[str, int | str | None]]:
    head_body, sep, metrics_raw = output.rpartition(MARKER)
    if not sep or len(metrics_raw) > 2048:
        raise ProviderError("curl_metrics_missing")
    try:
        metrics = json.loads(metrics_raw)
        diagnostics: dict[str, int | str | None] = {}
        for name in TIMINGS:
            value = metrics[name]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= max_time
            ):
                raise ValueError("invalid timing")
            diagnostics[name + "_ms"] = round(value * 1000)
        status_text = metrics["http_code"]
        if (
            not isinstance(status_text, str)
            or not status_text.isascii()
            or not status_text.isdecimal()
        ):
            raise ValueError("invalid status")
        status = int(status_text)
        if status != 0 and not 100 <= status <= 599:
            raise ValueError("invalid status")
        if metrics["exitcode"] != code:
            raise ValueError("exit code mismatch")
    except (ValueError, TypeError, KeyError):
        raise ProviderError("curl_metrics_invalid") from None
    diagnostics["transport_code"] = {
        0: "ok",
        5: "proxy_dns_failed",
        6: "dns_failed",
        7: "connect_failed",
        28: "timeout",
        35: "tls_failed",
        60: "tls_verification_failed",
    }.get(code, "other_transport_error")
    if code == 28:
        diagnostics["timeout_phase"] = (
            "response_body"
            if status
            else "after_tls_before_headers"
            if metrics["time_appconnect"] > 0
            else "tls_or_proxy_tunnel"
            if metrics["time_connect"] > 0
            else "tcp_connect"
            if metrics["time_namelookup"] > 0
            else "dns_or_connect"
        )
    response_headers: dict[str, str] = {}
    body = head_body
    while body.startswith(b"HTTP/"):
        header_block, delimiter, body = body.partition(b"\r\n\r\n")
        if not delimiter or len(header_block) > 8192:
            raise ProviderError("curl_headers_invalid")
        for line in header_block.split(b"\r\n")[1:]:
            header_name, colon, header_value = line.partition(b":")
            if colon:
                response_headers[header_name.decode("ascii", errors="ignore").lower()] = (
                    header_value.strip().decode("latin-1")
                )
        # Skip informational responses only; never reinterpret HTTP-like content.
        status_line = header_block.split(b"\r\n", 1)[0].split(b" ", 2)
        if len(status_line) < 2:
            raise ProviderError("curl_headers_invalid")
        if not status_line[1].startswith(b"1"):
            if status_line[1] != str(status).encode():
                raise ProviderError("curl_status_mismatch")
            break
        response_headers.clear()
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderError("curl_body_bound")
    # A partial body after curl timeout is diagnostic only, never a completed answer.
    return status or None, response_headers, body if code == 0 else b"", diagnostics
