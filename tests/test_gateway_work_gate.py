"""Offline HTTP checks for serialized large-payload work."""

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quota_broker import gateway_server

TOKEN = "fixture-client-token-at-least-thirty-two-characters"
HEADERS = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}


@pytest.fixture
def start_server():
    servers = []

    def start(*, run=None, tick=None, serialize_work=True):
        gateway = SimpleNamespace(
            run=Mock(side_effect=run or (lambda body: {"state": "completed"})),
            catalog=Mock(return_value={"targets": []}),
            diagnostics=Mock(return_value={}),
            clock=lambda: datetime.now(UTC),
        )
        queue = SimpleNamespace(
            tick=Mock(side_effect=tick),
            result=Mock(return_value={"answer": "fixture result"}),
            status=Mock(return_value={"state": "completed"}),
            recent=Mock(return_value=[]),
        )
        server = gateway_server.make_gateway_server(
            gateway,
            TOKEN,
            port=0,
            queue=queue,
            worker=tick is not None,
            serialize_work=serialize_work,
        )
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        servers.append((server, thread))
        return server, gateway, queue

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def request(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=HEADERS)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


def headers_only(server, *, length=8 * 1024 * 1024):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.putrequest("POST", "/v1/tasks")
    for key, value in HEADERS.items():
        connection.putheader(key, value)
    connection.putheader("Content-Length", str(length))
    connection.endheaders()
    return closing(connection)


def test_worker_rejects_post_before_body_and_keeps_metadata_available(start_server, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def tick(_worker):
        entered.set()
        assert release.wait(timeout=3)

    server, gateway, queue = start_server(tick=tick)
    try:
        assert entered.wait(timeout=2)
        read = Mock(side_effect=AssertionError("busy POST must not read body"))
        monkeypatch.setattr(server.RequestHandlerClass, "_read_body", read)
        with headers_only(server) as connection:
            response = connection.getresponse()
            assert response.status == 503
            assert json.loads(response.read()) == {"error": "work_busy"}
            assert response.getheader("Connection") == "close"
        read.assert_not_called()
        gateway.run.assert_not_called()
        for path in ("/v1/catalog", "/v1/diagnostics", "/v1/queue", "/v1/queue/job"):
            assert request(server, "GET", path)[0] == 200
        assert request(server, "GET", "/v1/queue/job/result")[:2] == (
            503,
            {"error": "work_busy"},
        )
        queue.result.assert_not_called()
    finally:
        release.set()


@pytest.mark.parametrize("blocked_phase", ["handle", "response"])
def test_post_gate_covers_handle_and_response_then_reuses(start_server, monkeypatch, blocked_phase):
    entered = threading.Event()
    release = threading.Event()

    def block():
        entered.set()
        assert release.wait(timeout=3)

    def run(body):
        if body.get("block") and blocked_phase == "handle":
            block()
        return {"block": body.get("block", False)}

    server, _, queue = start_server(run=run)
    send = server.RequestHandlerClass._send

    def blocked_send(self, status, data, **kwargs):
        if isinstance(data, dict) and data.get("block") and blocked_phase == "response":
            block()
        send(self, status, data, **kwargs)

    monkeypatch.setattr(server.RequestHandlerClass, "_send", blocked_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, server, "POST", "/v1/tasks", b'{"block":true}')
        try:
            assert entered.wait(timeout=2)
            with headers_only(server) as connection:
                response = connection.getresponse()
                assert response.status == 503
                assert json.loads(response.read()) == {"error": "work_busy"}
            assert request(server, "GET", "/v1/catalog")[0] == 200
            assert request(server, "GET", "/v1/queue/job/result")[0] == 503
            queue.result.assert_not_called()
        finally:
            release.set()
        assert pending.result(timeout=2)[0] == 200
    assert request(server, "GET", "/v1/queue/job/result")[:2] == (
        200,
        {"answer": "fixture result"},
    )
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


def test_result_decryption_holds_gate(start_server):
    entered = threading.Event()
    release = threading.Event()
    server, _, queue = start_server()

    def result(_key):
        entered.set()
        assert release.wait(timeout=3)
        return {"answer": "fixture result"}

    queue.result.side_effect = result
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, server, "GET", "/v1/queue/job/result")
        try:
            assert entered.wait(timeout=2)
            assert request(server, "POST", "/v1/tasks", b"{}")[0] == 503
            assert request(server, "GET", "/v1/catalog")[0] == 200
        finally:
            release.set()
        assert pending.result(timeout=2)[0] == 200
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


def test_worker_skips_tick_while_post_holds_gate(start_server):
    first_tick = threading.Event()
    post_entered = threading.Event()
    release = threading.Event()
    overlap = threading.Event()

    def tick(_worker):
        if post_entered.is_set() and not release.is_set():
            overlap.set()
        first_tick.set()

    def run(_body):
        post_entered.set()
        assert release.wait(timeout=3)
        return {"state": "completed"}

    server, _, queue = start_server(run=run, tick=tick)
    assert first_tick.wait(timeout=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(request, server, "POST", "/v1/tasks", b"{}")
        try:
            assert post_entered.wait(timeout=2)
            # Wait across the worker's next scheduled 0.5-second cycle.
            time.sleep(0.6)
            assert not overlap.is_set()
            assert queue.tick.call_count == 1
        finally:
            release.set()
        assert pending.result(timeout=2)[0] == 200


def test_invalid_json_and_handler_exception_release_gate(start_server):
    server, gateway, _ = start_server()
    assert request(server, "POST", "/v1/tasks", b"invalid")[0] == 400
    gateway.run.side_effect = RuntimeError("fixture internal error")
    assert request(server, "POST", "/v1/tasks", b"{}")[:2] == (
        503,
        {"error": "internal_error"},
    )
    gateway.run.side_effect = lambda body: {"state": "completed"}
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


def test_response_disconnect_releases_gate(start_server, monkeypatch):
    server, _, _ = start_server()
    send = server.RequestHandlerClass._send

    def broken_send(self, status, data, **kwargs):
        raise BrokenPipeError("fixture disconnected client")

    monkeypatch.setattr(server.RequestHandlerClass, "_send", broken_send)
    monkeypatch.setattr(server, "handle_error", Mock())
    with pytest.raises(http.client.RemoteDisconnected):
        request(server, "POST", "/v1/tasks", b"{}")
    monkeypatch.setattr(server.RequestHandlerClass, "_send", send)
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


@pytest.mark.parametrize("drip", [False, True])
def test_body_deadline_releases_gate_for_idle_and_dripping_client(start_server, monkeypatch, drip):
    monkeypatch.setattr(gateway_server, "BODY_READ_TIMEOUT", 0.3)
    server, gateway, _ = start_server()
    with headers_only(server, length=100) as connection:
        stop = threading.Event()

        def send_drips():
            while not stop.wait(0.04):
                try:
                    connection.sock.sendall(b" ")
                except OSError:
                    return

        sender = threading.Thread(target=send_drips, daemon=True)
        if drip:
            sender.start()
        started = time.monotonic()
        try:
            response = connection.getresponse()
            assert response.status == 408
            assert json.loads(response.read()) == {"error": "request_timeout"}
            assert response.getheader("Connection") == "close"
            assert time.monotonic() - started < 1.5
        finally:
            stop.set()
            if drip:
                sender.join(timeout=2)
        gateway.run.assert_not_called()
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


def test_incomplete_body_releases_gate(start_server):
    server, _, _ = start_server()
    with headers_only(server, length=10) as connection:
        connection.sock.sendall(b"{}")
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "invalid_request"}
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200


def test_worker_exception_releases_gate_and_stops_worker(start_server):
    def broken_tick(_worker):
        raise RuntimeError("fixture worker error")

    server, _, _ = start_server(tick=broken_tick)
    assert server.stop_worker.wait(timeout=2)
    assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200
    assert request(server, "GET", "/v1/diagnostics")[1]["queue_worker"]["error_code"] == (
        "worker_stopped"
    )


def test_default_allows_post_and_result_during_worker(start_server):
    entered = threading.Event()
    release = threading.Event()

    def tick(_worker):
        entered.set()
        assert release.wait(timeout=3)

    server, _, _ = start_server(tick=tick, serialize_work=False)
    try:
        assert entered.wait(timeout=2)
        assert request(server, "POST", "/v1/tasks", b"{}")[0] == 200
        assert request(server, "GET", "/v1/queue/job/result")[0] == 200
    finally:
        release.set()
