"""Historical filters use the complete ledger; model metadata grants no execution."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_dashboard import dashboard
from test_gateway import NOW

from quota_broker.dashboard import ActivitySource, Dashboard, DashboardError, UpstreamSource
from quota_broker.gateway_server import make_gateway_server


def recorded_attempts(source):
    """Content-free old rows and six newer rows, without provider dispatch."""
    with sqlite3.connect(source.gateway.db) as con:
        for number, age in enumerate([48, 47, 46, 5, 4, 3, 2, 1, 0]):
            when = (NOW - timedelta(hours=age)).isoformat()
            con.execute(
                "INSERT INTO gateway_tasks(request_key,payload_hmac,state,provider,model,"
                "created_at,dispatched_at,completed_at,reported_input_tokens,reported_output_tokens) "
                "VALUES(?,?,'completed','google','gemini-3.5-flash-lite',?,?,?,10,2)",
                (f"private-synthetic-key-{number}", "fixture-digest", when, when, when),
            )


def test_usage_range_queries_old_rows_beyond_recent_six_and_never_infers_task(tmp_path):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    report = board.usage_report(
        {
            "from": (NOW - timedelta(hours=48)).isoformat(),
            "to": (NOW - timedelta(hours=46)).isoformat(),
            "provider": "google",
            "task": "unknown",
        }
    )
    assert report["source"]["state"] == "available"
    assert report["rows"][0]["requests"] == 2
    assert report["rows"][0]["reported_input_tokens"] == 20
    assert report["rows"][0]["task_type"] == "unknown"
    assert report["task_metadata"]["state"] == "available_for_new_records"
    assert report["coverage"]["time_basis"] == "dispatched_at"
    assert report["execution_allowed"] is report["paid_allowed"] is False
    assert "private-synthetic-key" not in json.dumps(report)
    # Legacy rows never acquire a task label from their configured model.
    other_task = board.usage_report({"task": "translation"})
    assert other_task["rows"] == []
    assert other_task["source"]["state"] == "available"


@pytest.mark.parametrize(
    "filters",
    [
        {"from": "2026-09-30"},
        {"from": NOW.isoformat(), "to": NOW.isoformat()},
        {"provider": "https://elsewhere.invalid"},
        {"model": "https://not-a-model.invalid?q=1"},
        {"task": "Bearer synthetic-private"},
        {"from": [NOW.isoformat()]},
    ],
)
def test_usage_filters_are_bounded_and_validated_before_reading_source(tmp_path, filters):
    board, source = dashboard(tmp_path)
    source.usage_history = lambda _: (_ for _ in ()).throw(AssertionError("source read"))
    with pytest.raises(DashboardError, match="invalid_filters"):
        board.usage_report(filters)


def test_inventory_lists_registered_models_without_borrowing_target_balance(tmp_path):
    board, source = dashboard(tmp_path)
    snapshot = board.snapshot()
    assert snapshot["model_inventory"]["state"] == "available"
    models = snapshot["models"]
    nvidia = [row for row in models if row["provider"] == "nvidia"]
    assert len(nvidia) >= 2
    assert all(row["model_scope"] == "registered_unconfigured" for row in nvidia)
    assert all(row["configured_target_ids"] == [] for row in nvidia)
    assert all("observations" not in row and "balance" not in row for row in models)
    ready = next(row for row in models if row["model"] == "gemini-3.5-flash-lite")
    assert ready["configured_target_ids"] == ["demo-ready"]
    assert ready["capabilities"] == ["text_generation"]
    assert ready["input_modalities"] == ["document", "text"]
    assert ready["output_modalities"] == ["text"]
    assert ready["execution_allowed"] is False
    ocr = next(row for row in models if row["provider"] == "ocrspace")
    assert ocr["input_modalities"] == ["image"] and ocr["output_modalities"] == ["text"]
    target = snapshot["targets"][0]
    assert target["capabilities"] == ["text_generation"]
    assert "document" in target["input_modalities"]
    assert len(source.gateway.registry.models) == len(models)


def test_reviewed_mode_has_no_fake_empty_history(tmp_path):
    board, source = dashboard(tmp_path)
    source.kind = "reviewed"
    source.source_metadata = dict
    report = board.usage_report({})
    assert report["rows"] is None
    assert report["source"]["state"] == "unavailable"


def test_model_filter_queries_full_ledger_and_rejects_contradictory_source(tmp_path):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    model = "gemini-3.5-flash-lite"
    assert board.usage_report({"model": model})["rows"][0]["requests"] == 9
    other_model = next(
        model for _, model in source.gateway.registry.models if model != "gemini-3.5-flash-lite"
    )
    assert board.usage_report({"model": other_model})["rows"] == []
    original = source.usage_history
    source.usage_history = lambda _: original(
        {"provider": None, "model": None, "from": None, "to": None, "task": None}
    )
    report = board.usage_report({"model": other_model})
    assert report["rows"] is None
    assert report["source"]["state"] == "unavailable"


@pytest.mark.parametrize("failure", ["old_upstream", "contradictory_target"])
def test_unavailable_inventory_preserves_configured_target_snapshot(tmp_path, failure):
    board, source = dashboard(tmp_path)
    if failure == "old_upstream":

        def missing_models():
            raise DashboardError("source_unavailable")

        source.models = missing_models
        original_catalog = source.catalog

        def old_catalog():
            rows = original_catalog()
            for row in rows:
                row.pop("input_modalities")
                row.pop("output_modalities")
            return rows

        source.catalog = old_catalog
    else:
        rows = source.models()
        rows[0]["configured_target_ids"] = ["demo-ready"]
        rows[0]["model_scope"] = "configured_gateway_target"
        source.models = lambda: rows
    snapshot = board.snapshot()
    assert snapshot["model_inventory"]["state"] == "unavailable"
    assert snapshot["models"] == []
    assert len(snapshot["targets"]) == 3
    assert snapshot["targets"][0]["capabilities"] == ["text_generation"]
    if failure == "old_upstream":
        assert snapshot["targets"][0]["input_modalities"] == []
        assert snapshot["targets"][0]["output_modalities"] == []
    assert board.usage_report({})["source"]["state"] == "available"


@contextmanager
def gateway_http(source):
    token = "synthetic-history-upstream-token-0000000000"
    server = make_gateway_server(source.gateway, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_upstream_and_activity_query_full_history_over_real_http(tmp_path):
    board, source = dashboard(tmp_path)
    recorded_attempts(source)
    with sqlite3.connect(source.gateway.db) as con:
        con.execute(
            "UPDATE gateway_tasks SET task_type='translation' WHERE request_key IN (?,?)",
            ("private-synthetic-key-0", "private-synthetic-key-1"),
        )
    filters = {
        "from": (NOW - timedelta(hours=48)).isoformat(),
        "to": (NOW - timedelta(hours=46)).isoformat(),
        "provider": "google",
    }
    with gateway_http(source) as (url, token):
        upstream = UpstreamSource(url, token)
        remote = Dashboard(upstream, clock=lambda: NOW)
        assert remote.usage_report(filters)["rows"][0]["requests"] == 2
        other_model = next(
            model for _, model in source.gateway.registry.models if model != "gemini-3.5-flash-lite"
        )
        assert remote.usage_report({**filters, "model": other_model})["rows"] == []
        assert (
            remote.usage_report({**filters, "model": "gemini-3.5-flash-lite"})["rows"][0][
                "requests"
            ]
            == 2
        )
        typed = remote.usage_report({**filters, "task": "translation"})
        assert typed["rows"][0]["task_type"] == "translation"
        assert typed["rows"][0]["requests"] == 2
        assert remote.usage_report({**filters, "task": "text_generation"})["rows"] == []
        assert remote.snapshot()["model_inventory"]["state"] == "available"
        source.kind = "reviewed"
        source.source_metadata = dict
        board.activity = ActivitySource(url, token)
        assert board.usage_report(filters)["rows"][0]["reported_output_tokens"] == 4
        configured = board.snapshot()["targets"][0]["runtime_configuration"]
        assert configured["state"] == "available"
        assert configured["enabled"] is True
        assert configured["basis"] == "live_catalog_model_match_not_account_qualification"
        for path in (
            "/v1/usage?from=2026-09-30",
            "/v1/usage?provider=google&provider=groq",
            "/v1/usage?task=private_custom_label",
            "/v1/usage?from=2026-09-30T01:00:00Z#fragment",
            "http://elsewhere.invalid/v1/usage?provider=google",
            "/v1/tasks?limit=100",
        ):
            with pytest.raises(DashboardError, match="route_not_allowed"):
                upstream._get("GET", path)


def test_reviewed_protection_switch_does_not_claim_live_broker_is_disabled(tmp_path):
    board, source = dashboard(tmp_path)
    source.kind = "reviewed"
    source.source_metadata = dict
    snapshot = board.snapshot()
    for row in snapshot["targets"]:
        assert row["runtime_configuration"]["state"] == "unknown"
        assert row["runtime_configuration"]["enabled"] is None


def test_report_http_auth_filters_and_prefix_are_preserved(tmp_path):
    from test_dashboard_server import login, request, running

    with running(tmp_path) as (server, board):
        recorded_attempts(board.source)
        assert request(server, "/api/usage")[0] == 401
        cookie = login(server)
        query = urlencode(
            {
                "from": (NOW - timedelta(hours=48)).isoformat(),
                "to": (NOW - timedelta(hours=46)).isoformat(),
                "provider": "google",
                "task": "unknown",
            }
        )
        status, _, raw = request(server, "/api/usage?" + query, cookie=cookie)
        assert status == 200 and json.loads(raw)["rows"][0]["requests"] == 2
        for query in ("from=2026-09-30", "provider=google&provider=groq", "extra=1", "task="):
            assert request(server, "/api/usage?" + query, cookie=cookie)[0] == 400
        assert request(server, "/api/snapshot?from=2026-09-30", cookie=cookie)[0] == 404
        assert request(server, "/quota/api/usage", cookie=cookie)[0] == 404


@pytest.mark.parametrize(
    "rows", [[], [{"provider": "google", "model": "gemini-3.5-flash-lite", "requests": 4}]]
)
def test_old_upstream_task_filter_is_unavailable_even_when_unfiltered_usage_is_empty(rows):
    class OldHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            data = json.dumps({"error": "invalid_request"} if "task" in query else rows).encode()
            self.send_response(400 if "task" in query else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), OldHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = UpstreamSource(
            f"http://127.0.0.1:{server.server_port}", "synthetic-old-upstream-token-00000000"
        )
        board = Dashboard(source, clock=lambda: NOW)
        all_tasks = board.usage_report({})
        assert all_tasks["source"]["state"] == "available"
        assert all_tasks["task_metadata"]["state"] == "unavailable"
        if rows:
            assert all_tasks["rows"][0]["task_type"] == "unknown"
        else:
            assert all_tasks["rows"] == []
        for task_type in ("translation", "unknown"):
            filtered = board.usage_report({"task": task_type})
            assert filtered["source"]["state"] == "unavailable"
            assert filtered["rows"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
