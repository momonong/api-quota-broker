"""One normal Ministral 3B POST to verify a complete answer; offline by default.

Zero GET, no transport injection/retry/token renewal. Fixed independent receipt;
previous partial execution and Small unknown reservations remain unchanged.
"""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from v1_mistral_3b_gate_once import DB as GATE_DB
from v1_mistral_isolated_once import DB as ISOLATED_DB
from v1_nvidia_mistral_once import DB as FIRST_STAGE_DB
from v1_remaining_once import ALL_PRIOR, ROOT
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.gateway import Gateway, GatewayError
from quota_broker.nvidia import doppler_resolver_from_token

DB = ROOT / ".state" / "v1-mistral-formal-2026-10-02.sqlite"
KEY = "v1-mistral-formal-2026-10-02"
MODEL = "ministral-3b-latest"
PROMPT = "Output only READY. Do not include explanations."
MAX_OUTPUT = 512


def check_prior():
    count = 0
    for path in (*ALL_PRIOR, FIRST_STAGE_DB, ISOLATED_DB, GATE_DB):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            count += con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE provider='mistral' AND dispatched_at IS NOT NULL"
            ).fetchone()[0]
    if count != 5:
        raise RuntimeError("prior Mistral count mismatch")
    with sqlite3.connect(GATE_DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        gets = con.execute("SELECT details_json FROM diagnostic_gets").fetchall()
        posts = con.execute("SELECT details_json FROM acceptance_receipt").fetchall()
        if len(gets) != 1 or len(posts) != 1:
            raise RuntimeError("one prior verified gate and partial POST required")
        get = json.loads(gets[0][0])
        prior = json.loads(posts[0][0])
        if (
            get.get("fixed_chat_model_visible") is not True
            or prior.get("model") != MODEL
            or prior.get("http_status") != 200
            or prior.get("state") != "completed"
            or prior.get("ledger_state") != "completed"
            or prior.get("finish_reason") != "length"
            or prior.get("response_truncated") is not True
        ):
            raise RuntimeError("prior partial execution evidence required; no replay")
        if (datetime.now(UTC) - datetime.fromisoformat(prior["completed_at"])).total_seconds() < 60:
            raise RuntimeError("isolated request gap required; no automatic wait or retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: normal Mistral Ministral 3B maximum 1 POST; zero GET; 512 output tokens, 30 seconds; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or DB.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("existing private state directory and curl required")
    targets = tuple(
        replace(target, id="mistral-3b-formal-check", model=MODEL, max_output_tokens=MAX_OUTPUT)
        for target in runtime_targets(ROOT / "gateway.example.json", ("mistral",))
    )
    binary = cli()
    if "MISTRAL_API_KEY" not in metadata_names(binary):
        raise RuntimeError("required secret name absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(DB) as con:
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

    gateway = Gateway(DB, targets, secrets.token_bytes(32), resolver)
    if time.monotonic() - started > 300 - 33 - 30:
        raise RuntimeError("service_token_expiry_guard")
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
        print("mistral_formal_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
