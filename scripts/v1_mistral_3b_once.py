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
CANONICAL_MODEL = "ministral-3b-2512"


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


def candidate_details(data, key):
    # Public family/date identifiers only. Exclude ft/user identifiers, arbitrary
    # strings and exact credential reflections; no model descriptions/accounts.
    public_pattern = r"(?:mistral-(?:small|medium|large)|ministral-(?:3b|8b|14b))-(?:latest|2[3-6](?:0[1-9]|1[0-2]))"

    def public(value):
        return (
            isinstance(value, str)
            and re.fullmatch(public_pattern, value)
            and key.casefold() not in value.casefold()
        )

    models = data.get("data") if isinstance(data, dict) else None
    models = models if isinstance(models, list) else []
    candidates = [
        model
        for model in models
        if isinstance(model, dict)
        and public(model.get("id"))
        and (
            model.get("id") in (MODEL, CANONICAL_MODEL)
            or isinstance(model.get("aliases"), list)
            and MODEL in model["aliases"]
        )
    ]
    exact = [m for m in candidates if m.get("id") == MODEL]
    dated = [m for m in candidates if m.get("id") == CANONICAL_MODEL]
    aliases = [
        m for m in candidates if isinstance(m.get("aliases"), list) and MODEL in m["aliases"]
    ]
    candidate_consistent = bool(candidates) and all(
        m.get("id") in (MODEL, CANONICAL_MODEL) for m in candidates
    )
    # An exact listing is this model's route evidence. Old aliases can have
    # distinct lifecycle/access metadata and cannot veto a valid exact entry.
    evidence = exact or candidates
    consistent = bool(evidence) and all(m.get("id") in (MODEL, CANONICAL_MODEL) for m in evidence)
    preferred = (exact or dated or [None])[0]
    allowed = (
        consistent
        and all(
            not any(m.get(f) is True for f in ("requires_payment", "paid_only", "billing_enabled"))
            and not any(m.get(f) is False for f in ("free_eligible", "free_tier"))
            for m in evidence
        )
        and isinstance(data, dict)
        and data.get("billing_enabled") is not True
    )
    chat = consistent and all(
        m.get("active") is not False
        and m.get("archived") is not True
        and isinstance(m.get("capabilities"), dict)
        and m["capabilities"].get("completion_chat") is True
        for m in evidence
    )
    metadata = {}
    for model in models:
        if not isinstance(model, dict) or not public(model.get("id")):
            continue
        mid = model["id"]
        safe_aliases = (
            {a for a in model.get("aliases", []) if public(a)}
            if isinstance(model.get("aliases"), list)
            else set()
        )
        completion_chat = (
            isinstance(model.get("capabilities"), dict)
            and model["capabilities"].get("completion_chat") is True
        )
        if mid in metadata:
            safe_aliases.update(metadata[mid]["aliases"])
            completion_chat &= metadata[mid]["completion_chat"]
        metadata[mid] = {
            "id": mid,
            "aliases": sorted(safe_aliases)[:8],
            "completion_chat": completion_chat,
        }
    details = {
        "candidate_count": min(len(candidates), 1000),
        "exact_id_count": min(len(exact), 1000),
        "canonical_id_count": min(len(dated), 1000),
        "alias_count": min(len(aliases), 1000),
        "distinct_candidate_id_count": len(
            {m["id"] for m in candidates if m.get("id") in (MODEL, CANONICAL_MODEL)}
        ),
        "candidate_ids_consistent": candidate_consistent,
        "route_evidence_consistent": consistent,
        "route_evidence_basis": "exact_id"
        if exact
        else "canonical_or_alias"
        if evidence
        else "none",
        "fixed_3b_candidate_visible": bool(candidates),
        "free_access_not_contradicted": bool(allowed),
        "free_access_basis": "human_attested_free_no_card_included_usage",
        "matched_model_id": preferred["id"] if preferred and public(preferred["id"]) else None,
        "public_model_metadata": [metadata[mid] for mid in sorted(metadata)[:64]],
        "public_model_metadata_truncated": len(metadata) > 64,
    }
    return bool(allowed and chat and len(evidence) <= 64), details


def get_candidate(resolver, db):
    dispatched = None
    status = None
    ready = False
    details = {}
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO diagnostic_gets VALUES('mistral', 'preparing', NULL, NULL)")
    try:
        key = resolver("MISTRAL_API_KEY")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", key):
            raise ValueError("credential rejected")
        dispatched = now()
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE diagnostic_gets SET state='dispatched',dispatched_at=?", (dispatched,)
            )
        status, headers, raw, timing = model_http("mistral", {"Authorization": "Bearer " + key})
        details = safe_response_diagnostics(
            "mistral", status or 0, headers, raw, sensitive_values=(key,)
        )
        details.update(safe_transport_diagnostics(timing))
        data = json.loads(raw) if raw else {}
        ready, gate = candidate_details(data, key)
        details.update(gate)
        ready &= status == 200 and timing.get("transport_code") == "ok"
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
    with sqlite3.connect(db) as con:
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
    return run_once(DB, KEY)


def run_once(db, request_key):
    if db.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    check_prior()
    if not Path("/usr/bin/curl").is_file() or db.parent.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("existing private state directory and curl required")
    targets = tuple(
        replace(target, id="mistral-3b-bounded-check", model=MODEL, max_output_tokens=MAX_OUTPUT)
        for target in runtime_targets(ROOT / "gateway.example.json", ("mistral",))
    )
    binary = cli()
    if "MISTRAL_API_KEY" not in metadata_names(binary):
        raise RuntimeError("required secret name absent")
    descriptor = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(db) as con:
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
    if not get_candidate(resolver, db):
        return 2
    time.sleep(3)
    if time.monotonic() - started > 300 - 33 - 30:
        raise RuntimeError("service_token_expiry_guard")
    gateway = Gateway(db, targets, secrets.token_bytes(32), resolver)
    try:
        result = gateway.run(
            {
                "request_key": request_key,
                "provider": "mistral",
                "model": MODEL,
                "capability": "text_generation",
                "input": PROMPT,
                "max_output_tokens": MAX_OUTPUT,
            }
        )
    except GatewayError:
        result = gateway.status(request_key)
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
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO acceptance_receipt VALUES(?,?)", (request_key, json.dumps(safe)))
    print(json.dumps(safe, sort_keys=True))
    return 0 if safe["full_answer_verified"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        print("mistral_3b_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
