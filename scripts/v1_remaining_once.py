"""Offline plan by default; a proposed, separately approved remaining-provider check.

Live scope: one Mistral model GET, at most one Mistral POST and one NVIDIA
Nemotron general LLM POST. A separate authenticated Groq diagnosis needs approval.
No automatic retry, token renewal, model substitution or replay of old tasks.
"""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from bounded_curl import MODEL, PROMPT, nvidia_http
from v1_diagnose_once import PRIOR_DBS, ROOT
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_providers import (
    ProviderPhaseTimeout,
    provider_http,
    safe_response_diagnostics,
)
from quota_broker.nvidia import NoRedirect, doppler_resolver_from_token

DB = ROOT / ".state" / "v1-remaining-2026-10-02.sqlite"
ALL_PRIOR = (*PRIOR_DBS, ROOT / ".state" / "v1-diagnose-2026-10-02.sqlite")
MODEL_URL = "https://api.mistral.ai/v1/models"
PROVIDERS = ("mistral", "nvidia")


def check_prior() -> None:
    counts = dict.fromkeys(PROVIDERS, 0)
    for path in ALL_PRIOR:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            for (provider,) in con.execute(
                "SELECT provider FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
            ):
                if provider in counts:
                    counts[provider] += 1
    if any(count != 2 for count in counts.values()):
        raise RuntimeError("prior stage count mismatch")


def record(db: Path, key: str, provider: str, details: dict) -> None:
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO gateway_diagnostics(request_key,provider,details_json) VALUES(?,?,?)",
            (key, provider, json.dumps(details, sort_keys=True)),
        )


def model_ready(db: Path, resolver) -> bool:
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO diagnostic_gets VALUES('mistral-model', 'preparing', NULL, NULL)")
    try:
        key = resolver("MISTRAL_API_KEY")
        if not isinstance(key, str) or not key:
            raise ValueError("credential unavailable")
    except Exception:  # noqa: BLE001 - no resolver exception text may escape
        with sqlite3.connect(db) as con:
            con.execute("UPDATE diagnostic_gets SET state='pre_send_failed'")
        print(
            json.dumps({"provider": "mistral", "method": "GET model", "state": "pre_send_failed"})
        )
        return False
    request = urllib.request.Request(
        MODEL_URL, headers={"Authorization": "Bearer " + key, "Accept": "application/json"}
    )
    with sqlite3.connect(db) as con:
        con.execute("UPDATE diagnostic_gets SET state='dispatched', dispatched_at=datetime('now')")
    status = None
    ready = False
    details = {}
    try:
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.status
            raw = response.read(65_537)
            if len(raw) > 65_536:
                raise ValueError("model response bound")
            details = safe_response_diagnostics(
                "mistral", status, dict(response.headers), raw, sensitive_values=(key,)
            )
        data = json.loads(raw)
        models = data.get("data") if isinstance(data, dict) else None
        ready = bool(
            status == 200
            and isinstance(models, list)
            and any(
                isinstance(model, dict)
                and (
                    model.get("id") == "mistral-small-latest"
                    or isinstance(model.get("aliases"), list)
                    and "mistral-small-latest" in model["aliases"]
                )
                and isinstance(model.get("capabilities"), dict)
                and model["capabilities"].get("completion_chat") is True
                and model.get("archived") is not True
                for model in models
            )
        )
        state = "completed" if status == 200 else "unavailable"
    except (OSError, ValueError, TypeError):
        state = "unknown"
        details["error_code"] = "model_get_unknown"
    with sqlite3.connect(db) as con:
        con.execute("UPDATE diagnostic_gets SET state=?, http_status=?", (state, status))
    details["chat_available"] = ready
    record(db, "mistral-model-get", "mistral", details)
    print(
        json.dumps(
            {
                "provider": "mistral",
                "method": "GET model",
                "state": state,
                "http_status": status,
                "chat_available": ready,
                "diagnostics": details,
            }
        )
    )
    return ready


def diagnostic_transport(db: Path, provider: str):
    key = "v1-remaining-" + provider + "-2026-10-02"

    def request(url, headers, payload, timeout):
        if provider == "nvidia":
            status, received_headers, body, details = nvidia_http(headers, payload, 120)
            record(db, key, provider, details)
            if details["transport_code"] != "ok":
                code = "curl_" + str(details.get("timeout_phase", details["transport_code"]))
                raise ProviderPhaseTimeout(code)
            if status is None:
                raise ProviderPhaseTimeout("curl_status_missing")
        else:
            status, received_headers, body = provider_http(url, headers, payload, timeout)
        details = safe_response_diagnostics(
            provider,
            status,
            received_headers,
            body,
            sensitive_values=tuple(value.removeprefix("Bearer ") for value in headers.values()),
        )
        if provider == "nvidia" and status == 200:
            complete = (
                details.get("finish_reason") == "stop"
                and details.get("visible_answer_present") is True
                and details.get("provider_usage_complete") is True
            )
            details["completion_complete"] = complete
            if not complete:
                details["reason_category"] = "llm_completion_incomplete"
                # Preserve HTTP and actual usage while the Gateway retains an unknown hold.
                try:
                    data = json.loads(body)
                except (ValueError, TypeError):
                    data = None
                usage = data.get("usage") if isinstance(data, dict) else None
                body = json.dumps({"usage": usage}).encode()
        # For NVIDIA merge fixed response structure with already-recorded timings.
        if provider == "nvidia":
            with sqlite3.connect(db) as con:
                previous = con.execute(
                    "SELECT details_json FROM gateway_diagnostics WHERE request_key=?", (key,)
                ).fetchone()
                details.update(json.loads(previous[0]))
                con.execute(
                    "UPDATE gateway_diagnostics SET details_json=? WHERE request_key=?",
                    (json.dumps(details, sort_keys=True), key),
                )
        else:
            record(db, key, provider, details)
        return status, received_headers, body

    def transport(url, headers, payload, timeout):
        try:
            return request(url, headers, payload, timeout)
        except (OSError, ValueError) as exc:
            with sqlite3.connect(db) as con:
                if not con.execute(
                    "SELECT 1 FROM gateway_diagnostics WHERE request_key=?", (key,)
                ).fetchone():
                    details = {"transport_code": "local_transport_error"}
                    if isinstance(exc, ProviderPhaseTimeout) and exc.code in {
                        "timeout_before_headers",
                        "timeout_response_body",
                        "curl_process_deadline",
                    }:
                        details["transport_code"] = exc.code
                    record(db, key, provider, details)
            raise

    return transport


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print("plan_only: 1 Mistral model GET; maximum 2 independent POSTs; no credentials read")
        print("mistral mistral-small-latest max_output_tokens=32 timeout=30; GET gate timeout=15")
        print("nvidia", MODEL, "text_generation max_output_tokens=32 timeout=120 connect=10")
        print(
            "groq: 0 calls; separate authenticated diagnosis needs approval; no identity/IP workaround"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve():
        parser.error("live requires the fixed new receipt DB")
    if DB.exists():
        raise RuntimeError("receipt exists; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file():
        raise RuntimeError("required existing curl unavailable")
    targets = runtime_targets(ROOT / "gateway.example.json", ("mistral",))
    profile = load_gateway_config(ROOT / "gateway.example.json")
    llm = next(
        target for target in profile if target.provider == "nvidia" and target.model == MODEL
    )
    template = runtime_targets(ROOT / "gateway.example.json", ("nvidia",))[0]
    targets += (replace(template, id=llm.id, model=MODEL),)
    targets = tuple(replace(target, max_output_tokens=32) for target in targets)
    binary = cli()
    if not {"MISTRAL_API_KEY", "NVIDIA_API_KEY"}.issubset(metadata_names(binary)):
        raise RuntimeError("required secret names absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    gateway = Gateway(DB, targets, secrets.token_bytes(32), lambda _: "")
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE diagnostic_gets(id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "dispatched_at TEXT, http_status INTEGER)"
        )
        con.execute(
            "CREATE TABLE gateway_diagnostics(request_key TEXT PRIMARY KEY, "
            "provider TEXT NOT NULL, details_json TEXT NOT NULL)"
        )
    started = time.monotonic()
    token = service_token(binary)
    underlying = doppler_resolver_from_token(token, PROJECT, CONFIG)
    cache = {}

    def resolver(name):
        if name not in {"MISTRAL_API_KEY", "NVIDIA_API_KEY"}:
            raise ValueError("unapproved secret reference")
        if name not in cache:
            cache[name] = underlying(name)
        return cache[name]

    gateway.secret_resolver = resolver
    ready = model_ready(DB, resolver)
    unsuccessful = not ready
    for provider in PROVIDERS:
        if provider == "mistral" and not ready:
            continue
        if time.monotonic() - started > 300 - (123 if provider == "nvidia" else 30) - 30:
            print(json.dumps({"state": "stopped", "reason": "service_token_expiry_guard"}))
            return 2
        gateway.transport = diagnostic_transport(DB, provider)
        task = {
            "request_key": "v1-remaining-" + provider + "-2026-10-02",
            "provider": provider,
            "model": MODEL if provider == "nvidia" else "mistral-small-latest",
            "capability": "text_generation",
            "input": PROMPT if provider == "nvidia" else "Reply with the word READY.",
            "max_output_tokens": 32,
        }
        try:
            result = gateway.run(task)
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
            with sqlite3.connect(DB) as con:
                row = con.execute(
                    "SELECT details_json FROM gateway_diagnostics WHERE request_key=?",
                    (task["request_key"],),
                ).fetchone()
            print(
                json.dumps(
                    {
                        "provider": provider,
                        "method": "POST inference",
                        **safe,
                        "diagnostics": json.loads(row[0]) if row else None,
                    }
                )
            )
            unsuccessful |= result["state"] != "completed"
        except GatewayError as exc:
            print(json.dumps({"provider": provider, "state": "rejected", "error_code": exc.code}))
            unsuccessful = True
    return 2 if unsuccessful else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - suppress all secret-bearing exception text
        print("remaining_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
