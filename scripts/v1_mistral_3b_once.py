"""One fixed Ministral 3B model gate and normal Gateway POST; offline by default.

New hypothesis: another Free-mode model may work while Small is rate limited.
No model substitution, retry, token renewal, old replay or raw response logging.
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

from v1_mistral_isolated_once import DB as ISOLATED_DB
from v1_nvidia_mistral_once import DB as FIRST_STAGE_DB
from v1_remaining_once import ALL_PRIOR, ROOT
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.bounded_curl import model_http
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_providers import safe_response_diagnostics, safe_transport_diagnostics
from quota_broker.nvidia import doppler_resolver_from_token

DB = ROOT / ".state" / "v1-mistral-3b-2026-10-02.sqlite"
KEY = "v1-mistral-3b-2026-10-02"
MODEL = "ministral-3b-latest"
PROMPT = "Reply READY."
MAX_OUTPUT = 32


def now():
    return datetime.now(UTC).isoformat()


def check_prior():
    count = 0
    for path in (*ALL_PRIOR, FIRST_STAGE_DB, ISOLATED_DB):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            count += con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE provider='mistral' AND dispatched_at IS NOT NULL"
            ).fetchone()[0]
    if count != 4:
        raise RuntimeError("prior Mistral count mismatch")
    with sqlite3.connect(ISOLATED_DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        rows = con.execute("SELECT details_json FROM acceptance_receipt").fetchall()
        if len(rows) != 1:
            raise RuntimeError("one isolated Small receipt required")
        prior = json.loads(rows[0][0])
        if (
            prior.get("model") != "mistral-small-latest"
            or prior.get("http_status") != 429
            or prior.get("diagnostics", {}).get("provider_error_code") != 1300
        ):
            raise RuntimeError("prior reported rate limit required; no automatic probe")
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(prior["completed_at"])
        ).total_seconds()
        if elapsed < 60:
            raise RuntimeError("prior request gap required; no automatic wait or retry")


def get_candidate(resolver):
    dispatched = None
    status = None
    ready = False
    details = {}
    with sqlite3.connect(DB) as con:
        con.execute("INSERT INTO diagnostic_gets VALUES('mistral', 'preparing', NULL, NULL)")
    try:
        key = resolver("MISTRAL_API_KEY")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", key):
            raise ValueError("credential rejected")
        dispatched = now()
        with sqlite3.connect(DB) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='dispatched',dispatched_at=?", (dispatched,)
            )
        status, headers, raw, timing = model_http("mistral", {"Authorization": "Bearer " + key})
        details = safe_response_diagnostics(
            "mistral", status or 0, headers, raw, sensitive_values=(key,)
        )
        details.update(safe_transport_diagnostics(timing))
        data = json.loads(raw) if raw else {}
        models = data.get("data") if isinstance(data, dict) else None
        candidates = (
            [
                model
                for model in models
                if isinstance(model, dict)
                and (
                    model.get("id") == MODEL
                    or isinstance(model.get("aliases"), list)
                    and MODEL in model["aliases"]
                )
            ]
            if isinstance(models, list)
            else []
        )
        match = candidates[0] if len(candidates) == 1 else None
        # Model visibility is not billing proof. Carry forward the human-attested
        # Free/no-card scope; stop on any explicit paid-only contradiction.
        allowed = (
            match is not None
            and not any(
                match.get(field) is True
                for field in ("requires_payment", "paid_only", "billing_enabled")
            )
            and not any(match.get(field) is False for field in ("free_eligible", "free_tier"))
        )
        allowed &= isinstance(data, dict) and data.get("billing_enabled") is not True
        ready = (
            status == 200
            and timing.get("transport_code") == "ok"
            and allowed
            and match.get("active") is not False
            and match.get("archived") is not True
            and isinstance(match.get("capabilities"), dict)
            and match["capabilities"].get("completion_chat") is True
        )
        details["fixed_3b_candidate_visible"] = match is not None
        details["free_access_not_contradicted"] = bool(allowed)
        details["free_access_basis"] = "human_attested_free_no_card_included_usage"
        details["matched_model_id"] = (
            match.get("id") if match and match.get("id") in (MODEL, "ministral-3b-2512") else None
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
        "provider": "mistral",
        "method": "GET models",
        "state": state,
        "http_status": status,
        "dispatched_at": dispatched,
        "completed_at": now(),
        "fixed_chat_model_visible": ready,
        "diagnostics": details,
    }
    with sqlite3.connect(DB) as con:
        con.execute("UPDATE diagnostic_gets SET state=?,details_json=?", (state, json.dumps(safe)))
    print(json.dumps(safe, sort_keys=True))
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: Mistral maximum 1 authenticated models GET + 1 gated normal Ministral 3B POST; 32 output tokens, 30 seconds, 3-second gap; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or DB.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("existing private state directory and curl required")
    targets = tuple(
        replace(target, id="mistral-3b-bounded-check", model=MODEL, max_output_tokens=MAX_OUTPUT)
        for target in runtime_targets(ROOT / "gateway.example.json", ("mistral",))
    )
    binary = cli()
    if "MISTRAL_API_KEY" not in metadata_names(binary):
        raise RuntimeError("required secret name absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE diagnostic_gets(provider TEXT PRIMARY KEY,state TEXT,dispatched_at TEXT,details_json TEXT)"
        )
        con.execute(
            "CREATE TABLE acceptance_receipt(request_key TEXT PRIMARY KEY,details_json TEXT NOT NULL)"
        )
    started = time.monotonic()
    token = service_token(binary)
    underlying = doppler_resolver_from_token(token, PROJECT, CONFIG)
    cache = {}

    def resolver(name):
        if name != "MISTRAL_API_KEY":
            raise ValueError("unapproved secret reference")
        if name not in cache:
            cache[name] = underlying(name)
        return cache[name]

    if time.monotonic() - started > 300 - 33 - 30:
        raise RuntimeError("service_token_expiry_guard")
    if not get_candidate(resolver):
        return 2
    time.sleep(3)
    if time.monotonic() - started > 300 - 33 - 30:
        raise RuntimeError("service_token_expiry_guard")
    gateway = Gateway(DB, targets, secrets.token_bytes(32), resolver)
    try:
        result = gateway.run(
            {
                "request_key": KEY,
                "provider": "mistral",
                "model": MODEL,
                "capability": "text_generation",
                "input": PROMPT,
                "max_output_tokens": MAX_OUTPUT,
            }
        )
    except GatewayError:
        result = gateway.status(KEY)
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
        and result["response_truncated"] is False
        and safe["visible_answer_present"]
        and result["ledger_state"] == "completed"
    )
    with sqlite3.connect(DB) as con:
        con.execute("INSERT INTO acceptance_receipt VALUES(?,?)", (KEY, json.dumps(safe)))
    print(json.dumps(safe, sort_keys=True))
    return 0 if safe["full_answer_verified"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        print("mistral_3b_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
