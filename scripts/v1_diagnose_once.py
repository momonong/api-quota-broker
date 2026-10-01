"""Prepare or run one bounded follow-up diagnosis after separate human approval.

Plan mode is offline. Live mode is deliberately fixed to one new receipt name,
one Google model-list GET and four independent provider POSTs at most. It never
prints secrets, prompts, answers, or arbitrary provider response fields.
"""

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from v1_smoke_once import (
    CF_NEURON_BOUND,
    CONFIG,
    MODELS_BY_PROVIDER,
    PROJECT,
    cli,
    metadata_names,
    runtime_targets,
    service_token,
)

from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_providers import safe_http_error_code
from quota_broker.nvidia import NoRedirect, doppler_resolver_from_token

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
DIAGNOSE_DB = STATE / "v1-diagnose-2026-10-02.sqlite"
PRIOR_DBS = (
    STATE / "v1-smoke-2026-10-02.sqlite",
    STATE / "v1-smoke-2026-10-02-remaining.sqlite",
)
PROVIDERS = ("google", "mistral", "cloudflare", "nvidia")
GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
PROMPT = "Diagnostic 2026-10-02: reply with the single word READY."


def check_prior_receipts() -> None:
    counts = dict.fromkeys(PROVIDERS, 0)
    for path in PRIOR_DBS:
        if not path.is_file():
            raise RuntimeError("required prior receipt unavailable")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            for (provider,) in con.execute(
                "SELECT provider FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
            ):
                if provider in counts:
                    counts[provider] += 1
    if any(count != 1 for count in counts.values()):
        raise RuntimeError("prior dispatch count does not match the approved stage")


def google_model_visible(db: Path, resolver) -> bool:
    """One fixed GET, with a durable dispatch receipt before network I/O."""
    name = "google-3.5-model-list"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE diagnostic_gets (id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "state TEXT NOT NULL, dispatched_at TEXT, http_status INTEGER, "
            "model_visible INTEGER, error_code TEXT)"
        )
        con.execute(
            "INSERT INTO diagnostic_gets(id,provider,state) VALUES(?,?,?)",
            (name, "google", "preparing"),
        )
    try:
        key = resolver("GEMINI_API_KEY")
        if not isinstance(key, str) or not key:
            raise ValueError("credential unavailable")
    except Exception:  # noqa: BLE001 - secret resolver must fail closed
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='pre_send_failed',error_code='credential_unavailable' "
                "WHERE id=?",
                (name,),
            )
        print(
            json.dumps({"provider": "google", "method": "GET models", "state": "pre_send_failed"})
        )
        return False
    request = urllib.request.Request(
        GOOGLE_MODELS_URL,
        headers={"x-goog-api-key": key, "Accept": "application/json"},
    )
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE diagnostic_gets SET state='dispatched',dispatched_at=datetime('now') "
            "WHERE id=?",
            (name,),
        )
    status = None
    raw = b""
    try:
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.status
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("model catalog too large")
        data = json.loads(raw)
        models = data.get("models") if isinstance(data, dict) else None
        visible = bool(
            status == 200
            and isinstance(models, list)
            and any(
                isinstance(model, dict)
                and model.get("name") == "models/gemini-3.5-flash-lite"
                and isinstance(model.get("supportedGenerationMethods"), list)
                and "generateContent" in model.get("supportedGenerationMethods", [])
                for model in models
            )
        )
        complete = isinstance(data, dict) and not data.get("nextPageToken")
        state = "completed" if status == 200 and complete else "incomplete"
        error_code = (
            safe_http_error_code("google", status, raw)
            if status is not None and status >= 400
            else None
        )
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state=?,http_status=?,model_visible=?,error_code=? "
                "WHERE id=?",
                (state, status, int(visible) if state == "completed" else None, error_code, name),
            )
        print(
            json.dumps(
                {
                    "provider": "google",
                    "method": "GET models",
                    "state": state,
                    "http_status": status,
                    "model_visible": visible if state == "completed" else None,
                    "error_code": error_code,
                }
            )
        )
        return state == "completed" and visible
    except (OSError, ValueError, TypeError, KeyError):
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='unknown',http_status=?,error_code='get_unknown' "
                "WHERE id=?",
                (status, name),
            )
        print(json.dumps({"provider": "google", "method": "GET models", "state": "unknown"}))
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print("plan_only: 1 Google models GET; 4 provider POSTs maximum; no token or provider call")
        for provider in PROVIDERS:
            print(provider, MODELS_BY_PROVIDER[provider], "max_output_tokens=64")
        return 0
    if args.db is None or args.db.resolve() != DIAGNOSE_DB.resolve():
        parser.error("live requires the fixed new diagnostic DB path")
    if DIAGNOSE_DB.exists():
        raise RuntimeError("diagnostic receipt already exists; never replay")
    check_prior_receipts()
    targets = runtime_targets(ROOT / "gateway.example.json", PROVIDERS)
    binary = cli()
    names = {target.secret_ref for target in targets} | {"CLOUDFLARE_ACCOUNT_ID"}
    if not names.issubset(metadata_names(binary)):
        raise RuntimeError("required secret names absent")
    descriptor = os.open(DIAGNOSE_DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    gateway = Gateway(DIAGNOSE_DB, targets, secrets.token_bytes(32), lambda _: "")
    token_started = time.monotonic()
    token = service_token(binary)
    resolver = doppler_resolver_from_token(token, PROJECT, CONFIG)
    gateway.secret_resolver = resolver
    google_ready = google_model_visible(DIAGNOSE_DB, resolver)
    unsuccessful = not google_ready
    for provider in PROVIDERS:
        if provider == "google" and not google_ready:
            continue
        provider_timeout = 60 if provider == "nvidia" else 30
        if time.monotonic() - token_started > 300 - provider_timeout - 30:
            print(json.dumps({"state": "stopped", "reason": "service_token_expiry_guard"}))
            return 2
        task = {
            "request_key": "v1-diagnose-" + provider + "-2026-10-02",
            "capability": "text_generation",
            "input": PROMPT,
            "max_output_tokens": 64,
            "provider": provider,
            "model": MODELS_BY_PROVIDER[provider],
            "neuron_bound": CF_NEURON_BOUND if provider == "cloudflare" else None,
        }
        try:
            result = gateway.run(task)
            print(
                json.dumps(
                    {
                        "provider": provider,
                        "method": "POST inference",
                        "state": result["state"],
                        "http_status": result["http_status"],
                        "error_code": result["error_code"],
                        "attempts": len(result["attempts"]),
                        "reported_input_tokens": result["reported_input_tokens"],
                        "reported_output_tokens": result["reported_output_tokens"],
                        "reported_neurons": result["reported_neurons"],
                        "usage_source": result["usage_source"],
                    }
                )
            )
            unsuccessful |= result["state"] != "completed"
        except GatewayError as exc:
            print(
                json.dumps(
                    {
                        "provider": provider,
                        "method": "POST inference",
                        "state": "rejected",
                        "error_code": exc.code,
                    }
                )
            )
            unsuccessful = True
    return 2 if unsuccessful else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("diagnostic_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from exc
