"""One new Groq POST through the normal Gateway/provider_http route; default offline.

Fixed new receipt/request key refuses replay. No models GET, transport override,
retry, raw response, input/output logging, other providers or credential files.
"""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

from v1_groq_once import DB as DIAGNOSIS_DB
from v1_remaining_once import ALL_PRIOR, ROOT
from v1_smoke_once import CONFIG, PROJECT, cli, metadata_names, runtime_targets, service_token

from quota_broker.gateway import Gateway
from quota_broker.nvidia import doppler_resolver_from_token

DB = ROOT / ".state" / "v1-groq-formal-2026-10-02.sqlite"
KEY = "v1-groq-formal-2026-10-02"
MODEL = "openai/gpt-oss-20b"
PROMPT = "Reply with exactly READY."
MAX_OUTPUT = 512


def check_prior():
    count = 0
    for path in (*ALL_PRIOR, DIAGNOSIS_DB):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            count += con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE provider='groq' AND dispatched_at IS NOT NULL"
            ).fetchone()[0]
    if count != 2:
        raise RuntimeError("prior Groq count mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: normal Groq Gateway POST maximum 1; no GET; GPT-OSS 20B, 512 output tokens, low reasoning, 30 seconds; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or DB.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("existing private state directory and curl required")
    targets = tuple(
        replace(target, max_output_tokens=MAX_OUTPUT)
        for target in runtime_targets(ROOT / "gateway.example.json", ("groq",))
    )
    binary = cli()
    if "GROQ_API_KEY" not in metadata_names(binary):
        raise RuntimeError("required secret name absent")
    descriptor = os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
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

    # No injected transport: exactly the route used by ordinary CLI/HTTP Gateway tasks.
    gateway = Gateway(DB, targets, secrets.token_bytes(32), resolver)
    if time.monotonic() - started > 300 - 33 - 30:
        raise RuntimeError("service_token_expiry_guard")
    result = gateway.run(
        {
            "request_key": KEY,
            "provider": "groq",
            "model": MODEL,
            "capability": "text_generation",
            "input": PROMPT,
            "max_output_tokens": MAX_OUTPUT,
        }
    )
    complete = (
        result["state"] == "completed"
        and result["finish_reason"] == "stop"
        and result["response_truncated"] is False
        and bool(result.get("answer", "").strip())
        and result["ledger_state"] == "completed"
    )
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
        )
    }
    safe["visible_answer_present"] = bool(result.get("answer", "").strip())
    safe["ready_exact_match"] = result.get("answer", "").strip() == "READY"
    safe["full_answer_verified"] = complete
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE acceptance_receipt(request_key TEXT PRIMARY KEY, details_json TEXT NOT NULL)"
        )
        con.execute("INSERT INTO acceptance_receipt VALUES(?,?)", (KEY, json.dumps(safe)))
    print(json.dumps(safe, sort_keys=True))
    return 0 if complete else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        print("groq_formal_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
