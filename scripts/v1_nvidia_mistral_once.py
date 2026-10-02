"""One authenticated models GET and gated normal Gateway POST per remaining provider.

Offline by default. Fixed independent receipt; no retry, poll, second POST,
token renewal, old unknown replay, raw response/content/credential persistence.
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

from v1_remaining_once import ROOT, check_prior
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.bounded_curl import model_http
from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_providers import safe_response_diagnostics
from quota_broker.nvidia import doppler_resolver_from_token

DB = ROOT / ".state" / "v1-nvidia-mistral-2026-10-02.sqlite"
MODELS = {"mistral": "mistral-small-latest", "nvidia": "nvidia/nemotron-3.5-lightning-30b-a3b"}
PROMPTS = {
    "mistral": "Mistral formal check: reply with exactly READY.",
    "nvidia": "NVIDIA general LLM formal check: reply with exactly READY.",
}
SECRETS = {"mistral": "MISTRAL_API_KEY", "nvidia": "NVIDIA_API_KEY"}


def now():
    return datetime.now(UTC).isoformat()


def targets():
    result = runtime_targets(ROOT / "gateway.example.json", ("mistral", "nvidia"))
    profile = load_gateway_config(ROOT / "gateway.example.json")
    lightning = next(item for item in profile if item.model == MODELS["nvidia"])
    return tuple(
        replace(item, max_output_tokens=512, id=lightning.id, model=lightning.model)
        if item.provider == "nvidia"
        else replace(item, max_output_tokens=512)
        for item in result
    )


def get_model(provider, resolver):
    dispatched = None
    ready = False
    status = None
    with sqlite3.connect(DB) as con:
        con.execute("INSERT INTO diagnostic_gets VALUES(?, 'preparing', NULL, NULL)", (provider,))
    try:
        secret = resolver(SECRETS[provider])
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", secret):
            raise ValueError("credential rejected")
        dispatched = now()
        with sqlite3.connect(DB) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='dispatched', dispatched_at=? WHERE provider=?",
                (dispatched, provider),
            )
        status, headers, raw, timings = model_http(provider, {"Authorization": "Bearer " + secret})
        details = safe_response_diagnostics(
            provider, status or 0, headers, raw, sensitive_values=(secret,)
        )
        details.update(timings)
        data = json.loads(raw) if raw else {}
        models = data.get("data") if isinstance(data, dict) else None
        ready = (
            status == 200
            and timings["transport_code"] == "ok"
            and isinstance(models, list)
            and any(
                isinstance(model, dict)
                and (
                    model.get("id") == MODELS[provider]
                    or provider == "mistral"
                    and isinstance(model.get("aliases"), list)
                    and MODELS[provider] in model["aliases"]
                )
                and model.get("active") is not False
                and model.get("archived") is not True
                and (
                    provider == "nvidia"
                    or isinstance(model.get("capabilities"), dict)
                    and model["capabilities"].get("completion_chat") is True
                )
                for model in models
            )
        )
        state = "completed" if ready else "gate_failed"
    except (OSError, ValueError):
        details = {
            "reason_category": "transport_invalid_or_timeout"
            if dispatched
            else "credential_unavailable"
        }
        state = "unknown" if dispatched else "pre_send_failed"
    safe = {
        "provider": provider,
        "method": "GET models",
        "state": state,
        "http_status": status,
        "dispatched_at": dispatched,
        "completed_at": now(),
        "fixed_chat_model_visible": ready,
        "diagnostics": details,
    }
    with sqlite3.connect(DB) as con:
        con.execute(
            "UPDATE diagnostic_gets SET state=?, details_json=? WHERE provider=?",
            (state, json.dumps(safe), provider),
        )
    print(json.dumps(safe, sort_keys=True))
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: NVIDIA/Mistral each maximum 1 authenticated models GET + 1 gated normal Gateway POST; 512 output tokens; NVIDIA 120s, Mistral 30s; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or DB.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("private state directory and existing curl required")
    configured = targets()
    binary = cli()
    if not set(SECRETS.values()).issubset(metadata_names(binary)):
        raise RuntimeError("required secret names absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE diagnostic_gets(provider TEXT PRIMARY KEY, state TEXT, dispatched_at TEXT, details_json TEXT)"
        )
        con.execute(
            "CREATE TABLE acceptance_receipt(request_key TEXT PRIMARY KEY, details_json TEXT)"
        )
    started = time.monotonic()
    token = service_token(binary)
    underlying = doppler_resolver_from_token(token, PROJECT, CONFIG)
    cache = {}

    def resolver(name):
        if name not in SECRETS.values():
            raise ValueError("unapproved secret reference")
        if name not in cache:
            cache[name] = underlying(name)
        return cache[name]

    gateway = Gateway(DB, configured, secrets.token_bytes(32), resolver)
    unsuccessful = False
    for provider, model_id in MODELS.items():
        if time.monotonic() - started > 300 - 33 - 30:
            print("stopped: service_token_expiry_guard")
            return 2
        if not get_model(provider, resolver):
            unsuccessful = True
            continue
        limit = 123 if provider == "nvidia" else 33
        if time.monotonic() - started > 300 - limit - 30:
            print("stopped: service_token_expiry_guard")
            return 2
        key = "v1-formal-" + provider + "-2026-10-02"
        try:
            result = gateway.run(
                {
                    "request_key": key,
                    "provider": provider,
                    "model": model_id,
                    "capability": "text_generation",
                    "input": PROMPTS[provider],
                    "max_output_tokens": 512,
                }
            )
        except GatewayError as exc:
            print(json.dumps({"provider": provider, "state": "rejected", "error_code": exc.code}))
            unsuccessful = True
            continue
        safe = {
            name: result[name]
            for name in (
                "request_key",
                "provider",
                "model",
                "state",
                "http_status",
                "error_code",
                "finish_reason",
                "response_truncated",
                "provider_request_id",
                "dispatched_at",
                "completed_at",
                "latency_ms",
                "reported_input_tokens",
                "reported_output_tokens",
                "usage_source",
                "ledger_state",
                "ledger_basis",
                "diagnostics",
            )
        }
        safe["visible_answer_present"] = bool(result.get("answer", "").strip())
        safe["ready_exact_match"] = result.get("answer", "").strip() == "READY"
        safe["full_answer_verified"] = (
            result["state"] == "completed"
            and result["finish_reason"] == "stop"
            and safe["visible_answer_present"]
            and result["ledger_state"] == "completed"
        )
        with sqlite3.connect(DB) as con:
            con.execute(
                "INSERT INTO acceptance_receipt VALUES(?,?)",
                (key, json.dumps(safe, sort_keys=True)),
            )
        print(json.dumps(safe, sort_keys=True))
        unsuccessful |= not safe["full_answer_verified"]
    return 2 if unsuccessful else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - suppress all secret-bearing exception text
        print("remaining_formal_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
