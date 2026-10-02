"""One fixed NVIDIA POST, with phase timings; secrets and bodies stay in pipes.

Uses the existing curl binary, not a shell, with its default identity and TLS
verification. No retries, redirects, config files, logs, or on-disk response.
"""

import json
import math
import os
import selectors
import subprocess
import time

from quota_broker.gateway_providers import MAX_RESPONSE_BYTES, ProviderError, ProviderPhaseTimeout

URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MARKER = b"\nquota-broker-curl-metrics:"
TIMINGS = ("time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total")
OUTPUT_BOUND = MAX_RESPONSE_BYTES + 16_384


def _quote(value: str) -> str:
    # curl config quoting is not shell/JSON quoting. Reject controls, then escape
    # the only two config delimiters. JSON payload already contains escaped LF.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderError("invalid curl config value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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


def nvidia_http(
    headers: dict[str, str], payload: dict[str, object], timeout: float = 60
) -> tuple[int | None, dict[str, str], bytes, dict[str, int | str | None]]:
    if set(headers) != {"Authorization"} or not headers["Authorization"].startswith("Bearer "):
        raise ProviderError("unapproved curl headers")
    if (
        payload.get("model") != "nvidia/riva-translate-4b-instruct-v2"
        or payload.get("stream") is not False
    ):
        raise ProviderError("unapproved diagnostic model")
    if type(payload.get("max_tokens")) is not int or not 1 <= payload["max_tokens"] <= 32:
        raise ProviderError("unapproved diagnostic output bound")
    if payload != {
        "model": "nvidia/riva-translate-4b-instruct-v2",
        "messages": [
            {"role": "system", "content": "en-zh-cn"},
            {"role": "user", "content": "Good morning."},
        ],
        "max_tokens": payload["max_tokens"],
        "stream": False,
        "temperature": 0,
    }:
        raise ProviderError("unapproved diagnostic payload")
    if not 0 < timeout <= 60:
        raise ProviderError("unapproved diagnostic timeout")
    fields = {"http_code": "%{http_code}", "exitcode": "%{exitcode}"}
    fields.update({name: "%{" + name + "}" for name in TIMINGS})
    write_out = (
        MARKER.decode().replace("\n", "\\n")
        + "{"
        + ",".join(json.dumps(name) + ":" + value for name, value in fields.items())
        + "}"
    )
    # http_code is emitted as 000 without a response; quote it to remain valid JSON.
    write_out = write_out.replace('"http_code":%{http_code}', '"http_code":"%{http_code}"')
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
    head_body, sep, metrics_raw = output.rpartition(MARKER)
    if not sep or len(metrics_raw) > 2048:
        raise ProviderError("curl_metrics_missing")
    try:
        metrics = json.loads(metrics_raw)
        diagnostics: dict[str, int | str | None] = {}
        for name in TIMINGS:
            value = metrics[name]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 63:
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
            name, colon, value = line.partition(b":")
            if colon:
                response_headers[name.decode("ascii", errors="ignore").lower()] = (
                    value.strip().decode("latin-1")
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
