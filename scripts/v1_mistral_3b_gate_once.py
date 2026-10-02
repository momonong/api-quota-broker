"""One new authenticated GET to verify the repaired 3B gate, then the unused POST.

Fixed independent receipt. Original GET-only receipt remains unchanged; no
automatic retry, model scanning, paid access or other-provider requests.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import v1_mistral_3b_once as stage

DB = stage.ROOT / ".state" / "v1-mistral-3b-gate-2026-10-02.sqlite"
KEY = "v1-mistral-3b-gate-2026-10-02"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(
            "plan_only: repaired fixed 3B gate; 1 new authenticated GET, at most the unused 1 POST, 32 output tokens, 30s, 3s gap; no credentials read"
        )
        return 0
    if args.db is None or args.db.resolve() != DB.resolve() or DB.exists():
        raise RuntimeError("fixed new receipt required; never replay")
    with sqlite3.connect(stage.DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        rows = con.execute(
            "SELECT details_json FROM diagnostic_gets WHERE dispatched_at IS NOT NULL"
        ).fetchall()
        if (
            len(rows) != 1
            or con.execute("SELECT count(*) FROM acceptance_receipt").fetchone()[0] != 0
        ):
            raise RuntimeError("one prior GET and zero prior POST required")
        attempts_table = con.execute(
            "SELECT name FROM sqlite_master WHERE name='gateway_attempts'"
        ).fetchone()
        if (
            attempts_table
            and con.execute(
                "SELECT count(*) FROM gateway_attempts WHERE dispatched_at IS NOT NULL"
            ).fetchone()[0]
        ):
            raise RuntimeError("one prior GET and zero prior POST required")
        prior = json.loads(rows[0][0])
        if prior.get("http_status") != 200 or prior.get("state") != "gate_failed":
            raise RuntimeError("prior unresolved model gate required")
    return stage.run_once(DB, KEY)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        print("mistral_3b_gate_setup_failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
