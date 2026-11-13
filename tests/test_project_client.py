"""Project client against the actual private HTTP facade; fixture provider only."""

import http.client
import json
import threading
import time
from contextlib import contextmanager

import pytest
from test_family_adapters import GOOGLE
from test_gateway_access import TOKENS, running

from quota_broker.client import ClientError
from quota_broker.client import LegacyProjectClient as ProjectClient


def test_project_client_ocr_uses_the_same_result_text_contract(tmp_path):
    from test_family_adapters import media
    from test_gateway import target

    configured = target("ocr", "ocrspace", "ocr.space/engine2", capability="ocr")
    with running(tmp_path, private=True, configured=configured) as (
        base,
        _gateway,
        queue,
        calls,
        _,
    ):
        client = ProjectClient(base, TOKENS["a"])
        with fixture_worker(queue):
            outcome = client.run(
                {
                    "request_key": "ocr-001",
                    "capability": "ocr",
                    "input": media()["data"],
                    "max_output_tokens": 1,
                },
                wait_timeout=3,
                poll_interval=0.01,
            )
        assert outcome["state"] == "completed" and outcome["result"]["text"] == "fixture OCR"
        assert len(calls) == 1


def test_truncated_http_response_preserves_original_key_and_never_reposts():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Truncated(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"state":"queued"')
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Truncated)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = ProjectClient(f"http://127.0.0.1:{server.server_port}", TOKENS["a"])
        with pytest.raises(ClientError) as exc:
            client.submit_text("saved-key", "hello", max_output_tokens=16)
        assert exc.value.detail == {
            "error": "transport_unknown",
            "request_key": "saved-key",
            "action": "query_original_job",
        }
        assert calls == ["/quota/api/v1/jobs"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_helper_keeps_code_completion_output_limit(monkeypatch, tmp_path):
    from test_crypto_config import setup_config

    profile, _, _, _ = setup_config(tmp_path)
    from quota_broker.client import call_free_api
    from quota_broker.gateway import validate_task

    submitted = []

    def run(self, task, **_kwargs):
        submitted.append(task)
        return {"state": "completed"}

    monkeypatch.setattr(ProjectClient, "run", run)
    call_free_api(
        {"messages": [{"role": "user", "content": "def f():\n"}]},
        token=TOKENS["a"],
        request_key="code-001",
        capability="code_completion",
        max_output_tokens=16,
        trust_profile=profile,
        trust_state=tmp_path / "trust.db",
    )
    assert validate_task(submitted[0])["max_output_tokens"] == 16


@contextmanager
def fixture_worker(queue):
    stopped = threading.Event()

    def work():
        while not stopped.is_set():
            queue.tick("sdk-fixture-worker")
            stopped.wait(0.01)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_project_client_catalog_and_explain_without_execution(tmp_path):
    with running(tmp_path, private=True) as (base, _gateway, _queue, calls, _policy):
        client = ProjectClient(base, TOKENS["a"])
        assert client.catalog()[0]["provider"] == "google"
        plan = client.explain(
            {
                "request_key": "preview",
                "capability": "text_generation",
                "input": "hello",
                "max_output_tokens": 16,
            }
        )
        assert plan["selected_target_id"] == "google" and calls == []


@pytest.mark.parametrize("method", ["catalog", "explain"])
@pytest.mark.parametrize("failure", [TimeoutError, lambda: http.client.IncompleteRead(b"{", 10)])
def test_metadata_transport_errors_use_client_error(monkeypatch, method, failure):
    def fail(*args):
        raise failure()

    monkeypatch.setattr("quota_broker.client._json_http", fail)
    client = ProjectClient("https://momonong.me/quota", TOKENS["a"])
    with pytest.raises(ClientError) as exc:
        getattr(client, method)(*([{}] if method == "explain" else []))
    assert exc.value.detail["error"] == "transport_error"


@pytest.mark.parametrize("state", ["completed_usage_unknown", "failed", "unknown"])
def test_wait_preserves_outcome_semantics_without_submitting(monkeypatch, state):
    calls = []

    def reply(url, data, headers, timeout):
        calls.append((url, data))
        if url.endswith("/result"):
            return {"state": state, "request_key": "original", "result": {"text": "answer"}}
        return {
            "state": "completed" if state == "completed_usage_unknown" else state,
            "result_available": True,
        }

    monkeypatch.setattr("quota_broker.client._json_http", reply)
    client = ProjectClient("https://momonong.me/quota", TOKENS["a"])
    if state == "completed_usage_unknown":
        assert client.wait("original")["state"] == state
    else:
        with pytest.raises(ClientError) as exc:
            client.wait("original")
        assert exc.value.detail["error"] == ("job_unknown" if state == "unknown" else "job_failed")
        assert exc.value.detail["request_key"] == "original"
    assert len(calls) == 2 and all(data is None for _, data in calls)


def test_project_client_submit_status_result_and_cross_project_isolation(tmp_path):
    from quota_broker.client import LegacyProjectClient as ProjectClient

    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):

        def transport(request, *_args):
            calls.append(request)
            return 200, {}, json.dumps(GOOGLE).encode()

        gateway.family_transport = transport
        a, b = ProjectClient(base, TOKENS["a"]), ProjectClient(base, TOKENS["b"])
        submitted = a.submit_text("business-1", "hello", max_output_tokens=16)
        assert submitted["request_key"] == "business-1"
        queue.tick("fixture-worker")
        assert a.status("business-1")["state"] == "completed"
        assert a.result("business-1")["result"]["text"] == "answer"
        assert len(calls) == 1
        with pytest.raises(ClientError):
            b.result("business-1")
        a.submit_text("business-1", "hello", max_output_tokens=16)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.68.67:18588",
        "https://momonong.me/unrelated",
        "http://127.0.0.1:18588/other",
        "http://user:pass@127.0.0.1:18588",
        "http://127.0.0.1:18588?x=y",
    ],
)
def test_client_requires_local_private_tunnel_destination(url):
    from quota_broker.client import LegacyProjectClient as ProjectClient

    with pytest.raises(ClientError):
        ProjectClient(url, TOKENS["a"])


def test_client_transport_failure_never_retries_or_relabels_request(monkeypatch):
    from quota_broker.client import LegacyProjectClient as ProjectClient

    calls = []

    def fail(url, data, headers, timeout):
        calls.append((url, data))
        raise TimeoutError()

    monkeypatch.setattr("quota_broker.client._json_http", fail)
    client = ProjectClient("http://127.0.0.1:18588", TOKENS["a"])
    with pytest.raises(ClientError) as exc:
        client.submit_text("durable-business-key", "hello", max_output_tokens=16)
    assert exc.value.detail == {
        "error": "transport_unknown",
        "request_key": "durable-business-key",
        "action": "query_original_job",
    }
    assert len(calls) == 1 and calls[0][1]["request_key"] == "durable-business-key"


def test_project_request_keys_match_gateway_contract():
    from quota_broker.client import LegacyProjectClient as ProjectClient

    for value in ("~existing", ".existing", "_existing", "-existing"):
        assert ProjectClient._key(value) == value
    with pytest.raises(ClientError):
        ProjectClient._key("invalid:key")


def test_call_free_api_one_call_helper(tmp_path):
    from test_encrypted_api import encrypted_fixture

    from quota_broker.client import call_free_api

    with encrypted_fixture(tmp_path) as (
        base,
        profile,
        tls,
        gateway,
        queue,
        calls,
        _policy,
        _endpoint,
    ):

        def transport(request, *_args):
            calls.append(request)
            return 200, {}, json.dumps(GOOGLE).encode()

        gateway.family_transport = transport
        with fixture_worker(queue):
            result = call_free_api(
                "hello",
                token=TOKENS["a"],
                base_url=base,
                request_key="helper-call-1",
                trust_profile=profile,
                trust_state=tmp_path / "trust.db",
                ssl_context=tls,
                max_output_tokens=16,
                options={"temperature": 0.2},
                wait_timeout=2,
                poll_interval=0.01,
            )
        assert result["request_key"] == "helper-call-1"
        assert result["state"] == "completed"
        assert result["result"]["text"] == "answer"
        assert len(calls) == 1
        assert calls[0].payload["generationConfig"]["temperature"] == 0.2


@pytest.mark.parametrize(
    "base_url",
    ["https://momonong.me", "https://momonong.me/quota", "https://momonong.me/quota/api/v1/"],
)
def test_https_api_root_keeps_token_on_configured_origin(monkeypatch, base_url):
    sent = []

    def http(url, data, headers, timeout):
        sent.append((url, data, headers))
        return {"request_key": "https-key", "state": "queued"}

    monkeypatch.setattr("quota_broker.client._json_http", http)
    client = ProjectClient(base_url, TOKENS["a"])
    assert client.submit_text("https-key", "hello")["state"] == "queued"
    assert sent[0][0] == "https://momonong.me/quota/api/v1/jobs"
    assert sent[0][2] == {"Authorization": "Bearer " + TOKENS["a"]}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/quota/api/v1",
        "https://user:password@example.com/quota/api/v1",
        "https://example.com/quota/api/v1?token=secret",
        "https://example.com/quota/api/v1#fragment",
        "https://example.com/quota/api/v1/../admin",
        "https://example.com\n/quota/api/v1",
    ],
)
def test_sdk_rejects_unsafe_api_roots(url):
    with pytest.raises(ClientError):
        ProjectClient(url, TOKENS["a"])


def test_wait_timeout_preserves_job_for_later_result_without_replay(tmp_path):
    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):
        gateway.family_transport = lambda request, *_: (
            calls.append(request) or (200, {}, json.dumps(GOOGLE).encode())
        )
        client = ProjectClient(base + "/quota/api/v1", TOKENS["a"])
        submitted = client.submit_text("wait-key", "hello", max_output_tokens=16)
        assert submitted["state"] == "queued"
        start = time.monotonic()
        with pytest.raises(ClientError) as error:
            client.wait("wait-key", wait_timeout=0.05, poll_interval=0.01)
        assert time.monotonic() - start < 1
        assert error.value.detail["error"] == "wait_timeout"
        assert error.value.detail["request_key"] == "wait-key"
        assert calls == []
        queue.tick("fixture-worker")
        assert client.wait("wait-key", wait_timeout=1)["result"]["text"] == "answer"
        assert client.wait("wait-key", wait_timeout=1)["result"]["text"] == "answer"
        assert len(calls) == 1


def test_run_unknown_keeps_original_key_and_never_resubmits(tmp_path):
    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):

        def uncertain(request, *_):
            calls.append(request)
            raise TimeoutError("fixture uncertainty")

        gateway.family_transport = uncertain
        client = ProjectClient(base, TOKENS["a"])
        with fixture_worker(queue), pytest.raises(ClientError) as error:
            client.run(
                {
                    "request_key": "unknown-key",
                    "capability": "text_generation",
                    "input": {"messages": [{"role": "user", "content": "hello"}]},
                    "max_output_tokens": 16,
                },
                wait_timeout=2,
                poll_interval=0.01,
            )
        assert error.value.detail["error"] == "job_unknown"
        assert error.value.detail["request_key"] == "unknown-key"
        assert client.status("unknown-key")["state"] == "unknown"
        assert len(calls) == 1


def test_convenience_call_requires_a_stable_key_before_network(monkeypatch):
    from quota_broker.client import call_free_api

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid request must not reach HTTP")

    monkeypatch.setattr("quota_broker.client._json_http", forbidden)
    with pytest.raises(ClientError):
        call_free_api("hello", token=TOKENS["a"])


def test_invalid_wait_options_do_not_submit(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid wait policy must not submit")

    monkeypatch.setattr("quota_broker.client._json_http", forbidden)
    client = ProjectClient("http://127.0.0.1:18588", TOKENS["a"])
    for wait_timeout, poll_interval in [(float("nan"), 1), (1, 0), (-1, 1), (True, 1)]:
        with pytest.raises(ClientError):
            client.run(
                {"request_key": "invalid"}, wait_timeout=wait_timeout, poll_interval=poll_interval
            )
