"""Approved Groq-only diagnosis: one authenticated GET, then at most one POST.

Default is offline. Fixed new receipt refuses replay. Secrets stay in memory and
curl stdin; its natural identity, TLS validation, no retries or redirects apply.
"""

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bounded_curl import _quote, metrics_template, parse_curl_result, run_curl
from v1_remaining_once import ALL_PRIOR, ROOT, record
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import (
    ProviderError,
    ProviderPhaseTimeout,
    official_request,
    safe_response_diagnostics,
)
from quota_broker.nvidia import doppler_resolver_from_token

DB = ROOT / ".state" / "v1-groq-diagnose-2026-10-02.sqlite"
MODEL = "openai/gpt-oss-20b"
GET_URL = "https://api.groq.com/openai/v1/models"
POST_URL = "https://api.groq.com/openai/v1/chat/completions"
KEY = "v1-groq-diagnose-2026-10-02"
PROMPT = "Groq diagnosis: say READY."


def now():
    return datetime.now(UTC).isoformat()


def check_prior():
    count = 0
    for path in ALL_PRIOR:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            count += con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE provider='groq' AND dispatched_at IS NOT NULL"
            ).fetchone()[0]
    if count != 1:
        raise RuntimeError("prior Groq count mismatch")


def authorization(secret):
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", secret):
        raise ProviderError("credential_format_rejected")
    return "Bearer " + secret


def groq_http(url, headers, payload, timeout):
    if (
        timeout != 30
        or set(headers) != {"Authorization"}
        or not headers["Authorization"].startswith("Bearer ")
    ):
        raise ProviderError("unapproved Groq transport")
    authorization(headers["Authorization"].removeprefix("Bearer "))
    if url == GET_URL and payload is None:
        method = "GET"
    elif url == POST_URL:
        expected = official_request("groq", MODEL, "fixture", "fixture", PROMPT, 32, None, None)[2]
        if payload != expected:
            raise ProviderError("unapproved Groq payload")
        method = "POST"
    else:
        raise ProviderError("unapproved Groq URL")
    options = [
        "url = " + _quote(url),
        "request = " + _quote(method),
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        'connect-timeout = "10"',
        'max-time = "30"',
        "include",
        "suppress-connect-headers",
        'header = "Accept: application/json"',
        "header = " + _quote("Authorization: " + headers["Authorization"]),
        "write-out = " + _quote(metrics_template()),
    ]
    if payload is not None:
        options += [
            'header = "Content-Type: application/json"',
            "data-binary = " + _quote(json.dumps(payload, separators=(",", ":"))),
        ]
    code, output = run_curl(("\n".join(options) + "\n").encode(), 30)
    return parse_curl_result(code, output, 33)


def parsed(raw):
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def safe_id(value, sensitive, ray=False):
    pattern = (
        r"[0-9a-fA-F]{16}(?:-[A-Z]{3})?"
        if ray
        else r"(?:req_[A-Za-z0-9_-]{1,100}|chatcmpl-[A-Za-z0-9_-]{1,100}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
    )
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return None
    if any(secret and secret.casefold() in value.casefold() for secret in sensitive):
        return None
    return value


def diagnostics(status, headers, raw, timings, sensitive):
    details = safe_response_diagnostics(
        "groq", status or 0, headers, raw, sensitive_values=sensitive
    )
    details.update(timings)
    normalized = {name.lower(): value for name, value in headers.items()}
    data = parsed(raw)
    request_id = (
        normalized.get("x-request-id")
        or normalized.get("x-groq-request-id")
        or normalized.get("request-id")
    )
    groq = data.get("x_groq")
    if request_id is None and isinstance(groq, dict):
        request_id = groq.get("id")
    details["request_id_present"] = request_id is not None
    details["request_id"] = safe_id(request_id, sensitive)
    details["completion_id"] = safe_id(data.get("id"), sensitive)
    details["cf_ray_present"] = "cf-ray" in normalized
    details["cf_ray"] = safe_id(normalized.get("cf-ray"), sensitive, ray=True)
    code = details.get("error_code")
    actions = {
        "groq_edge_browser_signature_blocked": "groq_site_owner",
        "groq_model_blocked_org": "organization_model_limits",
        "groq_model_blocked_project": "project_model_limits",
    }
    details["reason_basis"] = "fixed_body_code" if code in actions else "http_status"
    details["next_check"] = actions.get(
        code,
        {
            200: "completion_or_model_gate",
            401: "api_key_and_project",
            403: "groq_support_or_account_permissions",
            429: "model_rate_limits",
            400: "request_contract",
            404: "endpoint_and_model_access",
        }.get(status, "provider_or_transport_diagnosis"),
    )
    details["reason_category"] = (
        code
        if code in actions
        else {
            200: "http_200",
            401: "authentication_rejected",
            403: "forbidden_unclassified",
            429: "rate_limit_scope_unknown",
            400: "bad_request_unclassified",
            404: "not_found_unclassified",
        }.get(status, "provider_or_transport_error")
    )
    return details


def emit(db, key, method, state, status, details):
    record(db, key, "groq", details)
    print(
        json.dumps(
            {
                "provider": "groq",
                "method": method,
                "state": state,
                "http_status": status,
                "diagnostics": details,
            },
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: Groq authenticated GET maximum 1; gate-passed POST maximum 1; 32 tokens, 30 seconds each; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or DB.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("existing private state directory and curl required")
    targets = tuple(
        replace(target, max_output_tokens=32)
        for target in runtime_targets(ROOT / "gateway.example.json", ("groq",))
    )
    binary = cli()
    if "GROQ_API_KEY" not in metadata_names(binary):
        raise RuntimeError("required secret name absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    gateway = Gateway(DB, targets, secrets.token_bytes(32), lambda _: "")
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE gateway_diagnostics(request_key TEXT PRIMARY KEY, provider TEXT NOT NULL, details_json TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE diagnostic_gets(id TEXT PRIMARY KEY, state TEXT NOT NULL, dispatched_at TEXT, completed_at TEXT, http_status INTEGER)"
        )
        con.execute("INSERT INTO diagnostic_gets VALUES('groq-models','preparing',NULL,NULL,NULL)")
    started = time.monotonic()
    token = service_token(binary)
    underlying = doppler_resolver_from_token(token, PROJECT, CONFIG)
    cache = {}

    def resolver(name):
        if name != "GROQ_API_KEY":
            raise ValueError("unapproved secret reference")
        if name not in cache:
            cache[name] = underlying(name)
        return cache[name]

    secret = resolver("GROQ_API_KEY")
    sensitive = (secret, token)
    try:
        headers = {"Authorization": authorization(secret)}
    except ProviderError:
        completed = now()
        with sqlite3.connect(DB) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='pre_send_failed', completed_at=?", (completed,)
            )
        emit(
            DB,
            "groq-models-get",
            "GET models",
            "pre_send_failed",
            None,
            {
                "completed_at": completed,
                "reason_category": "credential_format_rejected",
                "next_check": "doppler_key_format",
            },
        )
        return 2
    gateway.secret_resolver = resolver
    dispatched = now()
    with sqlite3.connect(DB) as con:
        con.execute("UPDATE diagnostic_gets SET state='dispatched', dispatched_at=?", (dispatched,))
    status = None
    ready = False
    try:
        status, received, raw, timings = groq_http(GET_URL, headers, None, 30)
        details = diagnostics(status, received, raw, timings, sensitive)
        models = parsed(raw).get("data")
        ready = (
            status == 200
            and timings.get("transport_code") == "ok"
            and isinstance(models, list)
            and any(
                isinstance(model, dict)
                and model.get("id") == MODEL
                and model.get("active") is not False
                for model in models
            )
        )
        state = "completed" if ready else "gate_failed"
        if status == 200 and not ready:
            details.update(
                reason_category="fixed_model_not_visible_or_list_invalid",
                next_check="key_project_and_model_access",
            )
    except (OSError, ValueError, ProviderError):
        details = {
            "reason_category": "transport_invalid_or_timeout",
            "next_check": "transport_diagnosis",
        }
        state = "unknown"
    details.update(dispatched_at=dispatched, completed_at=now(), fixed_model_visible=ready)
    with sqlite3.connect(DB) as con:
        con.execute(
            "UPDATE diagnostic_gets SET state=?, completed_at=?, http_status=?",
            (state, details["completed_at"], status),
        )
    emit(DB, "groq-models-get", "GET models", state, status, details)
    if not ready:
        print("post_skipped: model GET gate failed; no retry")
        return 2
    if time.monotonic() - started > 300 - 33 - 30:
        print("post_skipped: service_token_expiry_guard")
        return 2

    def transport(url, sent_headers, payload, timeout):
        sent_at = now()
        try:
            status, received, raw, timings = groq_http(url, sent_headers, payload, timeout)
            details = diagnostics(status, received, raw, timings, sensitive)
            data = parsed(raw)
            if status == 200 and timings.get("transport_code") == "ok":
                choices = data.get("choices")
                first = (
                    choices[0]
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                    else {}
                )
                finish = first.get("finish_reason")
                details["finish_reason"] = (
                    finish
                    if finish in ("stop", "length", "content_filter")
                    else "missing"
                    if finish is None
                    else "unclassified"
                )
                message = first.get("message")
                text = message.get("content") if isinstance(message, dict) else None
                details["visible_answer_present"] = isinstance(text, str) and bool(text.strip())
                usage = data.get("usage")
                details["provider_usage_complete"] = isinstance(usage, dict) and all(
                    type(usage.get(field)) is int and usage[field] >= 0
                    for field in ("prompt_tokens", "completion_tokens")
                )
                complete = (
                    finish == "stop"
                    and details["visible_answer_present"]
                    and details["provider_usage_complete"]
                )
                if not complete:
                    details.update(
                        reason_category="completion_incomplete",
                        next_check="completion_limit_or_response_contract",
                    )
                    data = {"usage": usage}
                # Prevent reflected secrets/arbitrary IDs from entering the Gateway ledger.
                for field in ("id", "requestId"):
                    if field in data and safe_id(data[field], sensitive) is None:
                        data.pop(field)
                raw = json.dumps(data).encode()
            elif data:
                for field in ("id", "requestId"):
                    if field in data and safe_id(data[field], sensitive) is None:
                        data.pop(field)
                raw = json.dumps(data).encode()
            details.update(dispatched_at=sent_at, completed_at=now())
            record(DB, KEY, "groq", details)
            if timings.get("transport_code") != "ok" or status is None:
                raise ProviderPhaseTimeout("groq_transport_incomplete")
            return status, received, raw
        except (OSError, ValueError, ProviderError):
            with sqlite3.connect(DB) as con:
                exists = con.execute(
                    "SELECT 1 FROM gateway_diagnostics WHERE request_key=?", (KEY,)
                ).fetchone()
            if not exists:
                record(
                    DB,
                    KEY,
                    "groq",
                    {
                        "dispatched_at": sent_at,
                        "completed_at": now(),
                        "reason_category": "transport_invalid_or_timeout",
                        "next_check": "transport_diagnosis",
                    },
                )
            raise

    gateway.transport = transport
    result = gateway.run(
        {
            "request_key": KEY,
            "provider": "groq",
            "model": MODEL,
            "capability": "text_generation",
            "input": PROMPT,
            "max_output_tokens": 32,
        }
    )
    with sqlite3.connect(DB) as con:
        row = con.execute(
            "SELECT details_json FROM gateway_diagnostics WHERE request_key=?", (KEY,)
        ).fetchone()
    safe = {
        name: result[name]
        for name in (
            "state",
            "http_status",
            "error_code",
            "reported_input_tokens",
            "reported_output_tokens",
            "usage_source",
        )
    }
    print(
        json.dumps(
            {
                "provider": "groq",
                "method": "POST inference",
                **safe,
                "diagnostics": json.loads(row[0]) if row else None,
            },
            sort_keys=True,
        )
    )
    return 0 if result["state"] == "completed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        print("groq_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
