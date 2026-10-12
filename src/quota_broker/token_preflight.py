"""Metadata-only, one-shot Google PDF token estimation receipts.

Counts are estimates, not billed usage or mathematical upper bounds. Auxiliary
POSTs consume the same local request safety caps in a separate durable ledger.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .config import Quota
from .core import calendar_bounds, stamp
from .family_transport import FamilyRequest, json_request

MODEL = "gemini-3.5-flash-lite"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
TTL_SECONDS = 120
RESPONSE_BYTES = 65536


def initialize(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS gateway_token_counts (
        request_key TEXT PRIMARY KEY, target_id TEXT NOT NULL, scope TEXT NOT NULL,
        quota_scope TEXT NOT NULL,
        model TEXT NOT NULL, wire_hmac TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, completed_at TEXT, expires_at TEXT,
        counted_tokens INTEGER, estimated_tokens INTEGER, http_status INTEGER,
        provenance TEXT NOT NULL DEFAULT 'estimated'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS gateway_count_charges (
        request_key TEXT NOT NULL, bucket TEXT NOT NULL, at TEXT NOT NULL,
        day_start TEXT, PRIMARY KEY(request_key,bucket)
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS gateway_count_bucket_at ON gateway_count_charges(bucket,at)"
    )


def needs_count(data: dict[str, Any]) -> bool:
    return (
        data.get("provider") == "google"
        and data.get("model") == MODEL
        and data.get("capability") == "text_generation"
        and isinstance(data.get("input"), dict)
        and "input_tokens" not in data.get("resource_bounds", {})
        and any(
            part.get("type") == "document" and part.get("mime_type") == "application/pdf"
            for message in data["input"].get("messages", [])
            for part in message.get("content", [])
            if isinstance(part, dict)
        )
    )


def count_request(generation: FamilyRequest) -> FamilyRequest:
    if generation.url != ENDPOINT + ":generateContent":
        raise ValueError("unsupported count endpoint")
    payload = json.loads(generation.body)
    if not isinstance(payload, dict):
        raise TypeError("unsupported generation payload")
    return json_request(
        ENDPOINT + ":countTokens",
        generation.headers,
        {"generateContentRequest": {**payload, "model": "models/" + MODEL}},
    )


def estimate(response: bytes) -> tuple[int, int]:
    if len(response) > RESPONSE_BYTES:
        raise ValueError("invalid token count")
    body = json.loads(response)
    count = body.get("totalTokens") if isinstance(body, dict) else None
    if type(count) is not int or not 0 < count <= 1048576:
        raise ValueError("invalid token count")
    # 20% proportional allowance plus 256 tokens for fixed protocol overhead.
    # Official examples differ by one; this policy is intentionally an estimate.
    return count, (count * 6 + 4) // 5 + 256


def used(con: sqlite3.Connection, quota: Quota, now: datetime) -> tuple[int, str | None]:
    if quota.metric != "requests":
        return 0, None
    if quota.window in {"rolling_minute", "rolling_hour"}:
        seconds = 60 if quota.window == "rolling_minute" else 3600
        rows = con.execute(
            "SELECT at FROM gateway_count_charges WHERE bucket=? AND at>?",
            (quota.bucket, stamp(now - timedelta(seconds=seconds))),
        ).fetchall()
        return len(rows), (
            stamp(max(datetime.fromisoformat(row[0]) for row in rows) + timedelta(seconds=seconds))
            if rows
            else None
        )
    start, reset = calendar_bounds(now, quota)
    amount = con.execute(
        "SELECT count(*) FROM gateway_count_charges WHERE bucket=? AND day_start=?",
        (quota.bucket, stamp(start)),
    ).fetchone()[0]
    return amount, stamp(reset) if amount else None


def charge(con: sqlite3.Connection, key: str, quotas: tuple[Quota, ...], now: datetime) -> None:
    for quota in quotas:
        if quota.metric == "requests":
            day = (
                None
                if quota.window in {"rolling_minute", "rolling_hour"}
                else stamp(calendar_bounds(now, quota)[0])
            )
            con.execute(
                "INSERT INTO gateway_count_charges VALUES(?,?,?,?)",
                (key, quota.bucket, stamp(now), day),
            )
