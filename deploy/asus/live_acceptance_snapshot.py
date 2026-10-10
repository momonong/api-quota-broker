"""Fixed read-only SQLite snapshot after the first live queue request stalls HTTP."""

import ctypes
import json
import os
import re
import resource
import sqlite3
import time
from pathlib import Path

UNIT = "api-quota-broker-live-snapshot-r2.service"
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
KEYS = [
    "asus-normal-v1-2026-10-08-r1-" + s
    for s in ("queued-auto-a1", "google-long-a1", "cloudflare-long-a1")
]
STATES = {
    "queued",
    "waiting",
    "running",
    "completed",
    "completed_usage_unknown",
    "failed",
    "unknown",
    "cancelled",
    "expired",
    "quota_rejected",
    "quota_exhausted",
    "rejected",
    "preparing",
    "dispatched",
    "reserved",
    "settled",
    "released",
}


def read_snapshot(con):
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    deadline = time.monotonic() + 10
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    con.execute("BEGIN")
    out = {
        "queue": [],
        "tasks": [],
        "attempts": [],
        "reservations": [],
        "counts": {},
        "provider_posts": 0,
        "database_writes": 0,
        "query_only": True,
    }
    executions = []
    for key in KEYS:
        rows = con.execute(
            "SELECT request_key,state,attempt_count,max_attempts,run_started,execution_key,lease_until IS NOT NULL AS has_lease,execution_until IS NOT NULL AS has_execution_deadline,result IS NOT NULL AS has_result FROM queue_jobs WHERE request_key=?",
            (key,),
        ).fetchall()
        assert len(rows) <= 1
        for row in rows:
            value = dict(row)
            assert value["state"] in STATES
            execution = value["execution_key"]
            assert execution is None or re.fullmatch(r"q-[a-f0-9]{32}", execution)
            if execution:
                executions.append(execution)
            out["queue"].append(value)
    for key in KEYS + executions:
        for table, columns, target in (
            (
                "gateway_tasks",
                "request_key,state,provider,http_status,dispatched_at IS NOT NULL AS dispatched,completed_at IS NOT NULL AS completed",
                "tasks",
            ),
            (
                "gateway_attempts",
                "request_key,attempt_no,state,provider,http_status,dispatched_at IS NOT NULL AS dispatched,completed_at IS NOT NULL AS completed",
                "attempts",
            ),
            (
                "reservations",
                "request_key,state,dispatched_at IS NOT NULL AS dispatched",
                "reservations",
            ),
        ):
            condition = " WHERE request_key=?"
            params = (key,)
            if table == "reservations":
                condition = " WHERE request_key=? OR id IN (SELECT reservation_id FROM gateway_attempts WHERE request_key=?)"
                params = (key, key)
            rows = con.execute(
                "SELECT " + columns + " FROM " + table + condition, params
            ).fetchall()
            # Reservation keys may use an attempt suffix. Project only the fixed
            # parent/execution key whose join selected them.
            if table == "reservations":
                rows = [{**dict(row), "request_key": key} for row in rows]
            assert len(rows) <= 3
            for row in rows:
                value = dict(row)
                assert value["state"] in STATES
                assert value.get("provider") in {
                    None,
                    "nvidia",
                    "google",
                    "groq",
                    "mistral",
                    "cloudflare",
                    "openrouter",
                    "ocrspace",
                }
                out[target].append(value)
    for table in (
        "gateway_tasks",
        "gateway_attempts",
        "reservations",
        "charges",
        "execution_completion",
        "queue_jobs",
        "queue_attempts",
    ):
        out["counts"][table] = con.execute("SELECT count(*) FROM " + table).fetchone()[0]
    con.execute("ROLLBACK")
    return out


def main():
    assert os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + UNIT
    group = Path("/sys/fs/cgroup/system.slice") / UNIT
    assert (group / "memory.swap.max").read_text().strip() == "0"
    assert os.statvfs(DB).f_flag & os.ST_RDONLY
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    assert libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(39, 0, 0, 0, 0) == 1
    con = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        return read_snapshot(con)
    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True), flush=True)
