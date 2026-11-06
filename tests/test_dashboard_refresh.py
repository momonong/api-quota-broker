"""Refresh keeps original evidence and never turns metadata reads into inference."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_dashboard_accounts import account_document, write_accounts
from test_dashboard_reviewed import ANCHOR, write_bundle

from quota_broker.dashboard import Dashboard
from quota_broker.dashboard_reviewed import create_reviewed
from quota_broker.discovery_parsers import parse_models

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/dashboard_refresh.py"


def run_script(*args, input_text=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        input=input_text,
        text=True,
        check=False,
    )


def private_file(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_inspect_reports_stale_without_disclosing_balance_or_renewing_evidence(tmp_path):
    path = write_accounts(tmp_path)
    original = path.read_bytes()
    result = run_script("inspect", "--accounts-file", path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = report["accounts"][0]
    assert row["provider"] == "openai" and row["state"] == "stale"
    assert row["as_of"] == "2026-01-03T12:00:00+00:00"
    assert row["valid_until"] == "2026-01-03T12:15:00+00:00"
    assert row["balance_known"] is False
    assert row["sources"] == [
        "https://platform.openai.com/settings/organization/billing/credit-grants"
    ]
    assert "23.45" not in result.stdout
    assert path.read_bytes() == original


def test_account_import_is_dry_run_then_atomic_with_private_old_evidence(tmp_path):
    destination = write_accounts(tmp_path)
    old = destination.read_bytes()
    doc = account_document()
    doc["accounts"][0]["balance"] = "12.34"
    doc["accounts"][0]["evidence"]["as_of"] = (ANCHOR + timedelta(days=1)).isoformat()
    doc["accounts"][0]["evidence"]["valid_until"] = (
        ANCHOR + timedelta(days=1, minutes=15)
    ).isoformat()
    incoming = private_file(destination.parent / "incoming.json", doc)
    result = run_script("import-accounts", "--input", incoming, "--destination", destination)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "dry_run"
    assert destination.read_bytes() == old
    assert not list(destination.parent.glob("*.previous.*"))
    applied = run_script(
        "import-accounts", "--input", incoming, "--destination", destination, "--apply"
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(destination.read_text()) == doc
    backups = list(destination.parent.glob("accounts.json.previous.*"))
    assert len(backups) == 1 and backups[0].read_bytes() == old
    assert os.stat(destination).st_mode & 0o777 == 0o600
    assert os.stat(backups[0]).st_mode & 0o777 == 0o600
    again = run_script(
        "import-accounts", "--input", incoming, "--destination", destination, "--apply"
    )
    assert again.returncode == 0
    assert json.loads(again.stdout)["changed"] is False
    assert len(list(destination.parent.glob("accounts.json.previous.*"))) == 1


def test_account_import_cannot_extend_old_as_of_or_overwrite_newer_evidence(tmp_path):
    destination = write_accounts(tmp_path)
    old = destination.read_bytes()
    doc = account_document()
    doc["accounts"][0]["evidence"]["valid_until"] = (ANCHOR + timedelta(hours=2)).isoformat()
    incoming = private_file(destination.parent / "incoming.json", doc)
    result = run_script(
        "import-accounts", "--input", incoming, "--destination", destination, "--apply"
    )
    assert result.returncode == 2
    assert "evidence_not_newer" in result.stderr
    assert destination.read_bytes() == old


@pytest.mark.parametrize("unsafe", ["symlink", "ancestor_symlink", "hardlink", "mode", "source"])
def test_unsafe_metadata_is_rejected_without_reflecting_input(tmp_path, unsafe):
    path = write_accounts(tmp_path)
    if unsafe == "symlink":
        linked = path.parent / "linked.json"
        linked.symlink_to(path)
        path = linked
    elif unsafe == "ancestor_symlink":
        linked = tmp_path / "linked"
        linked.symlink_to(path.parent, target_is_directory=True)
        path = linked / path.name
    elif unsafe == "hardlink":
        os.link(path, path.parent / "hardlink.json")
    elif unsafe == "mode":
        path.chmod(0o640)
    else:
        doc = account_document()
        doc["accounts"][0]["evidence"]["sources"] = []
        private_file(path, doc)
    result = run_script("inspect", "--accounts-file", path)
    assert result.returncode == 2
    assert "23.45" not in result.stdout + result.stderr


def test_reviewed_import_stages_new_bundle_without_modifying_original_or_enabling(tmp_path):
    config, _, _ = write_bundle(tmp_path)
    originals = {p.name: p.read_bytes() for p in config.parent.iterdir()}
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    destination = evidence / "next-observation"
    args = ("import-reviewed", "--input", config, "--destination", destination)
    dry = run_script(*args)
    assert dry.returncode == 0, dry.stderr
    assert not destination.exists()
    applied = run_script(*args, "--apply")
    assert applied.returncode == 0, applied.stderr
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == originals
    assert {p.name: p.read_bytes() for p in config.parent.iterdir()} == originals
    assert all(
        not target["enabled"]
        for target in json.loads((destination / "gateway.json").read_text())["targets"]
    )
    assert not list(config.parent.glob("*.db*"))
    assert os.stat(destination).st_mode & 0o777 == 0o700
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in destination.iterdir())
    assert run_script(*args, "--apply").returncode == 2


def test_reviewed_summary_keeps_unverified_and_expired_estimates_unknown(tmp_path):
    config, _, _ = write_bundle(tmp_path)
    result = run_script("inspect", "--reviewed-config", config)
    assert result.returncode == 0, result.stderr
    rows = {row["provider"]: row for row in json.loads(result.stdout)["targets"]}
    assert rows["cloudflare"]["remaining_state"] == "stale_estimate"
    assert rows["ocrspace"]["qualification_state"] == "unverified"
    assert rows["sambanova"]["qualification_state"] == "unverified"


def test_collect_dry_run_reads_no_token_and_sends_no_requests(tmp_path):
    result = run_script(
        "collect",
        "--upstream",
        "http://127.0.0.1:9",
        "--token-file",
        "/nonexistent",
        "--output",
        tmp_path / "snapshot.json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry_run" and report["network_performed"] is False
    assert report["routes"] == [
        "/v1/catalog",
        "/v1/diagnostics",
        "/v1/usage",
        "/v1/tasks?limit=6",
        "/v1/model-inventory",
    ]
    assert not (tmp_path / "snapshot.json").exists()


def test_partial_provider_batch_reloads_without_affecting_admission(tmp_path):
    config, _, _ = write_bundle(tmp_path)
    target = next(
        t
        for t in json.loads(config.with_name("gateway.json").read_text())["targets"]
        if t["provider"] == "nvidia"
    )
    now = datetime(2026, 1, 12, 23, 59, tzinfo=UTC)
    snapshot = parse_models("nvidia", {"data": [{"id": target["model"]}]}, now)
    batch = private_file(
        config.parent / "batch.json",
        {"state": "stopped", "snapshots": [snapshot], "account_metadata": []},
    )
    counter = private_file(
        config.parent / "counter.json",
        {
            "schema_version": 1,
            "provider": "openrouter",
            "source": "https://openrouter.ai/api/v1/key",
            "checked_at": now.isoformat(),
            "daily_counter_semantics": "reported_tier_policy_not_admission",
            "free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0},
        },
    )
    report = config.parent / "provider-observations.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    dashboard = Dashboard(
        create_reviewed(runtime, config, clock=lambda: now),
        provider_metadata_file=report,
        clock=lambda: now,
    )
    assert dashboard.snapshot()["provider_metadata"]["state"] == "unavailable"
    result = run_script(
        "import-provider-metadata",
        "--input",
        "-",
        "--input",
        counter,
        "--reviewed-config",
        config,
        "--destination",
        report,
        "--apply",
        input_text=batch.read_text(),
    )
    assert result.returncode == 0, result.stderr
    current = dashboard.snapshot()
    observed = next(t for t in current["targets"] if t["provider"] == "nvidia")
    assert observed["catalog_observation"]["status"] == "listed"
    assert observed["enabled"] is False and current["execution_allowed"] is False
    assert current["provider_metadata"]["openrouter"]["daily"]["remaining"] == 0

    # A same-time correction cannot renew or overwrite the evidence.
    raw = json.loads(counter.read_text())
    raw["free_model_daily_requests"] = None
    private_file(counter, raw)
    result = run_script(
        "import-provider-metadata",
        "--input",
        counter,
        "--reviewed-config",
        config,
        "--destination",
        report,
        "--apply",
    )
    assert result.returncode == 2
    assert "provider_observation_not_newer" in result.stderr
    now += timedelta(minutes=1)
    expired = dashboard.snapshot()["provider_metadata"]["openrouter"]
    assert expired["state"] == "stale" and expired["daily"] is None


def test_collect_refuses_to_replace_configuration_or_other_private_file(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    output = private_file(directory / "gateway.json", {"targets": []})
    original = output.read_bytes()
    result = run_script(
        "collect",
        "--upstream",
        "http://127.0.0.1:9",
        "--token-file",
        "/nonexistent",
        "--output",
        output,
        "--apply",
    )
    assert result.returncode == 2
    assert "collection_destination_required" in result.stderr
    assert output.read_bytes() == original


def test_collect_uses_only_metadata_gets_and_discards_private_task_fields(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.command, self.path))
            responses = {
                "/v1/catalog": [],
                "/v1/diagnostics": {"targets": [], "as_of": datetime.now(UTC).isoformat()},
                "/v1/usage": [],
                "/v1/model-inventory": [
                    {
                        "provider": "google",
                        "model": "fixture-model",
                        "capabilities": ["text_generation"],
                        "features": ["text"],
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "context_tokens": 1000,
                        "max_output_tokens": 100,
                        "configured_target_ids": [],
                        "model_scope": "registered_unconfigured",
                        "capability_basis": "configured_registry",
                        "execution_allowed": False,
                        "paid_allowed": False,
                    }
                ],
                "/v1/tasks?limit=6": {
                    "tasks": [
                        {
                            "provider": "groq",
                            "model": "fixture-model",
                            "state": "completed",
                            "created_at": "2026-01-03T12:00:00+00:00",
                            "completed_at": None,
                            "latency_ms": 123,
                            "request_key": "private-task-identifier",
                            "prompt": "private-prompt",
                            "result": "private-answer",
                        }
                    ]
                },
            }
            body = json.dumps(responses[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    token = directory / "client.token"
    token.write_text("synthetic-broker-client-token-0000000000")
    token.chmod(0o600)
    output = directory / "metadata.json"
    try:
        result = run_script(
            "collect",
            "--upstream",
            f"http://127.0.0.1:{server.server_port}",
            "--token-file",
            token,
            "--output",
            output,
            "--apply",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert requests == [
        ("GET", "/v1/catalog"),
        ("GET", "/v1/diagnostics"),
        ("GET", "/v1/usage"),
        ("GET", "/v1/tasks?limit=6"),
        ("GET", "/v1/model-inventory"),
    ]
    document = json.loads(output.read_text())
    assert document["provider_balance_state"] == "manual_required"
    assert document["snapshot"]["recent"][0]["latency_ms"] == 123
    serialized = output.read_text() + result.stdout + result.stderr
    for forbidden in (
        "private-task-identifier",
        "private-prompt",
        "private-answer",
        token.read_text(),
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:80",
        "http://localhost:80",
        "http://example.com:80",
        "http://127.0.0.1:80/v1",
    ],
)
def test_collect_rejects_non_loopback_and_path_prefix_even_in_dry_run(tmp_path, url):
    result = run_script(
        "collect",
        "--upstream",
        url,
        "--token-file",
        "/nonexistent",
        "--output",
        tmp_path / "out.json",
    )
    assert result.returncode == 2
    assert not (tmp_path / "out.json").exists()


def test_older_counter_import_cannot_relabel_newer_failed_refresh(tmp_path):
    from quota_broker.dashboard_provider_metadata import REFRESH_SOURCES

    config, _, _ = write_bundle(tmp_path)
    start = datetime(2026, 1, 12, 10, tzinfo=UTC)
    statuses = {
        p: {
            "status": "permission_required"
            if row[0] == "permission_required"
            else "console_required"
        }
        for p, row in REFRESH_SOURCES.items()
    }
    statuses["openrouter"] = {"status": "query_failed"}
    report = private_file(
        config.parent / "provider-observations.json",
        {
            "schema_version": 2,
            "kind": "provider_metadata_observations",
            "models": [],
            "openrouter": {
                "source": "https://openrouter.ai/api/v1/key",
                "checked_at": start.isoformat(),
                "daily": {"used": 0, "limit": 50, "remaining": 50},
            },
            "refresh": {
                "attempted_at": (start + timedelta(minutes=10)).isoformat(),
                "providers": statuses,
            },
        },
    )
    imported = private_file(
        config.parent / "counter.json",
        {
            "schema_version": 1,
            "provider": "openrouter",
            "source": "https://openrouter.ai/api/v1/key",
            "checked_at": (start + timedelta(minutes=5)).isoformat(),
            "daily_counter_semantics": "reported_tier_policy_not_admission",
            "free_model_daily_requests": {"used": 1, "limit": 50, "remaining": 49},
        },
    )
    result = run_script(
        "import-provider-metadata",
        "--input",
        imported,
        "--reviewed-config",
        config,
        "--destination",
        report,
        "--apply",
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads(report.read_bytes())
    assert saved["refresh"]["providers"]["openrouter"]["status"] == "query_failed"
    assert saved["refresh"]["attempted_at"] == (start + timedelta(minutes=10)).isoformat()
    assert saved["openrouter"]["daily"]["remaining"] == 49
