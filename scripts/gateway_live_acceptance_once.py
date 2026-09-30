"""Run at most one Riva translation and one Lightning text task through the gateway.

Doppler Service Token, NVIDIA key, bearer token, prompts, and answers remain in
process memory. This script emits only metadata and creates one exclusive SQLite
ledger; it never retries an uncertain provider dispatch.
"""

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from quota_broker.client import _NoRedirect
from quota_broker.config import Target, load_gateway_config
from quota_broker.core import utcnow
from quota_broker.gateway import Gateway
from quota_broker.gateway_providers import RIVA
from quota_broker.gateway_server import make_gateway_server
from quota_broker.nvidia import doppler_resolver_from_token
from scripts.verify_doppler_executor_read import (
    CONFIG,
    PROJECT,
    checked_metadata,
    cli,
    create_service_token,
)

LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"
TASKS = (
    ("translation", RIVA, "Hello.", 16),
    ("text_generation", LIGHTNING, "Reply with OK.", 32),
)
OLD_RECEIPTS = (
    Path(".state/nvidia-smoke-once.json"),
    Path(".state/nvidia-riva-translate-once.json"),
)


def verify_targets(targets: tuple[Target, ...]) -> tuple[Target, Target]:
    enabled = [target for target in targets if target.enabled]
    if len(enabled) != 2 or {target.model for target in enabled} != {RIVA, LIGHTNING}:
        raise ValueError("exactly Riva and Lightning must be enabled")
    by_model = {target.model: target for target in enabled}
    for target in enabled:
        if (
            target.provider != "nvidia"
            or target.secret_ref != "NVIDIA_API_KEY"
            or not target.available(utcnow())
            or target.concurrency_limit != 1
            or target.shared_concurrency_limit != 1
            or target.max_output_tokens < {m: n for _, m, _, n in TASKS}[target.model]
            or not all(q.limit > 0 for q in target.quotas)
        ):
            raise ValueError("NVIDIA admission metadata is not verified")
        caps = {(q.metric, q.window): q for q in target.quotas}
        if (
            caps[("requests", "rolling_minute")].limit != 1
            or caps[("requests", "day")].limit != 2
            or caps[("input_tokens", "rolling_minute")].limit > 1024
        ):
            raise ValueError("bounded shared local safety caps required")
    a, b = enabled
    if a.shared_concurrency_scope != b.shared_concurrency_scope or {
        (q.metric, q.window): q.bucket for q in a.quotas
    } != {(q.metric, q.window): q.bucket for q in b.quotas}:
        raise ValueError("NVIDIA caps must share account buckets")
    return by_model[RIVA], by_model[LIGHTNING]


def receipt_hashes() -> dict[str, str | None]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for path in OLD_RECEIPTS
    }


def api(base: str, token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=95) as response:
        return json.load(response)


def cli_json(base: str, token: str, *args: str) -> Any:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "quota_broker.cli",
            "gateway",
            "--url",
            base,
            "--token-stdin",
            "--json",
            *args,
        ],
        input=(token + "\n").encode(),
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("authenticated CLI query failed")
    return json.loads(result.stdout)


def serve(gateway: Gateway, token: str) -> tuple[Any, threading.Thread, str]:
    server = make_gateway_server(gateway, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def stop(server: Any, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def once(config_path: Path, db: Path) -> dict[str, Any]:
    targets = load_gateway_config(config_path)
    riva, lightning = verify_targets(targets)
    before = receipt_hashes()
    db.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    digest_key = secrets.token_bytes(48)
    client_token = secrets.token_urlsafe(48)
    tasks = [
        {
            "request_key": f"gateway-live-{capability}-{uuid.uuid4().hex}",
            "capability": capability,
            "input": prompt,
            "max_output_tokens": max_output,
            "provider": "nvidia",
            "model": model,
            **(
                {"source_language": "en", "target_language": "zh-cn"}
                if capability == "translation"
                else {}
            ),
        }
        for capability, model, prompt, max_output in TASKS
    ]
    gateway = Gateway(db, (riva, lightning), digest_key, lambda _: "")
    if gateway.explain(tasks[0])["selected_target_id"] != riva.id:
        raise RuntimeError("translation preflight route unavailable")
    binary = cli()
    if not checked_metadata(binary):
        raise RuntimeError("Doppler secret name metadata unavailable")
    service_token = create_service_token(binary)
    token_started = time.monotonic()
    resolver = doppler_resolver_from_token(service_token, PROJECT, CONFIG)
    gateway = Gateway(db, (riva, lightning), digest_key, resolver)
    server, thread, base = serve(gateway, client_token)
    records: list[dict[str, Any]] = []
    first_started = time.monotonic()
    try:
        for index, task in enumerate(tasks):
            if index:
                if records[0]["status"]["state"] != "completed":
                    break
                remaining = 61.0 - (time.monotonic() - first_started)
                if remaining > 0:
                    print(json.dumps({"progress": "waiting_for_shared_rpm_window"}), flush=True)
                    time.sleep(remaining)
                if time.monotonic() - token_started > 240:
                    break
                if gateway.explain(task)["selected_target_id"] != lightning.id:
                    break
            print(
                json.dumps({"progress": "dispatch", "capability": task["capability"]}), flush=True
            )
            response = api(base, client_token, "/v1/tasks", task)
            status = api(base, client_token, "/v1/tasks/" + task["request_key"])
            answer = response.get("answer")
            records.append(
                {
                    "task": task,
                    "status": status,
                    "answer_present": isinstance(answer, str) and bool(answer),
                    "answer": answer,
                }
            )
            print(
                json.dumps(
                    {
                        "progress": "reported",
                        "capability": task["capability"],
                        "state": status["state"],
                    }
                ),
                flush=True,
            )
        usage = api(base, client_token, "/v1/usage?provider=nvidia")
    finally:
        stop(server, thread)

    def forbidden(*_args: Any) -> Any:
        raise RuntimeError("provider replay attempted")

    restarted = Gateway(db, (riva, lightning), digest_key, forbidden, forbidden)
    second_server, second_thread, second_base = serve(restarted, client_token)
    try:
        for record in records:
            task = record["task"]
            status = record["status"]
            after_restart = api(second_base, client_token, "/v1/tasks/" + task["request_key"])
            duplicate = api(second_base, client_token, "/v1/tasks", task)
            cli_status = cli_json(second_base, client_token, "status", task["request_key"])
            record["restart_status_matches"] = after_restart["state"] == status["state"]
            record["duplicate_metadata_only"] = "answer" not in duplicate
            record["cli_status_matches"] = cli_status["state"] == status["state"]
        cli_usage = cli_json(second_base, client_token, "usage", "--provider", "nvidia")
    finally:
        stop(second_server, second_thread)
    with sqlite3.connect(db) as con:
        task_count = con.execute("SELECT count(*) FROM gateway_tasks").fetchone()[0]
        reservation_count = con.execute("SELECT count(*) FROM reservations").fetchone()[0]
        report_count = con.execute("SELECT count(*) FROM reports").fetchone()[0]
    data = db.read_bytes()
    content_free = all(
        prompt.encode() not in data
        and (not isinstance(record["answer"], str) or record["answer"].encode() not in data)
        for record, (_, _, prompt, _) in zip(records, TASKS, strict=False)
    )
    return {
        "results": [
            {
                "request_key": record["task"]["request_key"],
                "capability": record["task"]["capability"],
                "model": record["status"]["model"],
                "state": record["status"]["state"],
                "http_status": record["status"]["http_status"],
                "answer_present": record["answer_present"],
                "provider_usage": {
                    "input_tokens": record["status"]["reported_input_tokens"],
                    "output_tokens": record["status"]["reported_output_tokens"],
                },
                "ledger_state": record["status"]["ledger_state"],
                "ledger_basis": record["status"]["ledger_basis"],
                "restart_status_matches": record["restart_status_matches"],
                "duplicate_metadata_only": record["duplicate_metadata_only"],
                "cli_status_matches": record["cli_status_matches"],
            }
            for record in records
        ],
        "api_usage": usage,
        "cli_usage": cli_usage,
        "sqlite_task_count": task_count,
        "sqlite_reservation_count": reservation_count,
        "sqlite_report_count": report_count,
        "sqlite_content_free": content_free,
        "old_receipts_unchanged": receipt_hashes() == before,
        "db": str(db),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = once(args.config, args.db)
    except Exception as exc:  # noqa: BLE001 - never expose token/provider exception text
        print(
            json.dumps(
                {"acceptance": "incomplete", "error_type": type(exc).__name__, "db": str(args.db)}
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if (
            len(result["results"]) == 2
            and all(row["state"] == "completed" for row in result["results"])
            and result["sqlite_content_free"]
            and result["old_receipts_unchanged"]
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
