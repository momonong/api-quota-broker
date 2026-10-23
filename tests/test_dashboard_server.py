"""Local HTTP boundaries: sessions, origins, redaction, route and input limits."""

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quota_broker.dashboard import Dashboard, DashboardError, UpstreamSource
from quota_broker.dashboard_demo import create_demo

TOKEN = "synthetic-dashboard-login-only-00000000"


@contextmanager
def running(tmp_path):
    from quota_broker.dashboard_server import make_dashboard_server

    board = Dashboard(create_demo(tmp_path))
    server = make_dashboard_server(board, TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, board
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(server, path, *, method="GET", payload=None, cookie=None, origin=True, headers=None):
    con = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    h = {"Content-Type": "application/json", "X-Dashboard-Request": "1"}
    if origin:
        h["Origin"] = f"http://127.0.0.1:{server.server_port}"
    if cookie:
        h["Cookie"] = cookie
    h.update(headers or {})
    con.request(method, path, json.dumps(payload) if payload is not None else None, h)
    response = con.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    con.close()
    return result


def login(server):
    status, headers, _ = request(server, "/api/login", method="POST", payload={"token": TOKEN})
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    return headers["Set-Cookie"].split(";")[0]


def test_session_required_upstream_bearer_cannot_authenticate(tmp_path):
    with running(tmp_path) as (server, _):
        assert request(server, "/api/snapshot")[0] == 401
        assert (
            request(server, "/api/snapshot", headers={"Authorization": "Bearer " + TOKEN})[0] == 401
        )
        cookie = login(server)
        status, _, raw = request(server, "/api/snapshot", cookie=cookie)
        assert status == 200 and json.loads(raw)["source"]["kind"] == "demo"
        assert TOKEN.encode() not in raw


def test_origin_route_input_and_logout_boundaries(tmp_path):
    with running(tmp_path) as (server, _):
        cookie = login(server)
        body = {
            "request_key": "advice",
            "capability": "text_generation",
            "input": "合成測試",
            "max_output_tokens": 16,
        }
        assert request(server, "/api/advice", method="POST", payload=body, cookie=cookie)[0] == 200
        assert (
            request(
                server, "/api/advice", method="POST", payload=body, cookie=cookie, origin=False
            )[0]
            == 403
        )
        assert (
            request(
                server,
                "/api/advice",
                method="POST",
                payload=body,
                cookie=cookie,
                headers={"Origin": "https://evil.invalid"},
            )[0]
            == 403
        )
        for path in (
            "/v1/tasks",
            "/v1/queue",
            "/v1/admin/discovery/refresh",
            "/api/advice?path=/v1/tasks",
            "/%61pi/advice",
        ):
            assert request(server, path, method="POST", payload=body, cookie=cookie)[0] == 404
        assert (
            request(
                server, "/api/advice", method="POST", payload={"input": "x" * 70000}, cookie=cookie
            )[0]
            == 413
        )
        assert request(server, "/api/advice", method="POST", payload={}, cookie=cookie)[0] == 400
        assert request(server, "/api/logout", method="POST", payload={}, cookie=cookie)[0] == 200
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401


def test_errors_are_fixed_and_never_reflect_exception_or_old_data(tmp_path):
    with running(tmp_path) as (server, board):
        cookie = login(server)
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 200

        def broken():
            raise RuntimeError("sensitive-upstream-token <script>execute()</script>")

        board.source.catalog = broken
        status, _, raw = request(server, "/api/snapshot", cookie=cookie)
        assert status == 503 and json.loads(raw) == {"error": "source_unavailable"}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:80",
        "http://localhost:80",
        "https://127.0.0.1:80",
        "http://127.0.0.1:80/path",
        "http://user@127.0.0.1:80",
        "http://127.0.0.1:80?x=y",
    ],
)
def test_upstream_loopback_url_validation(url):
    with pytest.raises(ValueError):
        UpstreamSource(url, TOKEN)


def test_upstream_allowlist_rejects_execution_before_network():
    source = UpstreamSource("http://127.0.0.1:9", TOKEN)
    with pytest.raises(DashboardError, match="route_not_allowed"):
        source._get("POST", "/v1/tasks", {})


def test_cli_offline_mode_is_exclusive_and_private_token_files(tmp_path, monkeypatch):
    from quota_broker.cli import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "dashboard-serve",
            "--demo",
            "--upstream",
            "http://127.0.0.1:9",
            "--dashboard-token-file",
            str(tmp_path / "unused"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "response_kind", ["redirect", "token", "escaped_token", "oversize", "bad_json"]
)
def test_upstream_rejects_redirects_reflection_and_unbounded_shapes(response_kind):
    routes = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            routes.append(self.path)
            self.send_response(302 if response_kind == "redirect" else 200)
            if response_kind == "redirect":
                self.send_header("Location", "/v1/tasks")
            self.end_headers()
            if response_kind == "token":
                self.wfile.write(json.dumps({"model": TOKEN}).encode())
            elif response_kind == "escaped_token":
                self.wfile.write(('"' + "".join(f"\\u{ord(c):04x}" for c in TOKEN) + '"').encode())
            elif response_kind == "oversize":
                try:
                    self.wfile.write(b'"' + b"x" * (2 * 1024 * 1024 + 1))
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.wfile.write(b"not JSON")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = UpstreamSource(f"http://127.0.0.1:{server.server_port}", TOKEN)
        with pytest.raises(DashboardError, match="source_unavailable"):
            source.catalog()
        assert routes == ["/v1/catalog"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_private_token_file_permissions_symlinks_and_separate_identity(tmp_path):
    from quota_broker.dashboard_server import make_dashboard_server, read_private_token

    path = tmp_path / "synthetic.token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_private_token(path)
    path.chmod(0o600)
    assert read_private_token(path) == TOKEN
    link = tmp_path / "linked.token"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_private_token(link)
    with pytest.raises(ValueError, match="differ"):
        make_dashboard_server(Dashboard(UpstreamSource("http://127.0.0.1:9", TOKEN)), TOKEN, port=0)


def test_real_gateway_http_contract_without_any_provider_or_secret_access(tmp_path):
    from quota_broker.gateway_server import make_gateway_server

    demo = create_demo(tmp_path)
    gateway_token = "synthetic-gateway-upstream-only-00000000"
    server = make_gateway_server(demo.gateway, gateway_token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        board = Dashboard(UpstreamSource(f"http://127.0.0.1:{server.server_port}", gateway_token))
        snapshot = board.snapshot()
        assert snapshot["source"]["kind"] == "gateway"
        assert len(snapshot["targets"]) == 3
        assert snapshot["recent"] == [] and snapshot["usage"] == []
        result = board.advice(
            {
                "request_key": "synthetic-upstream-advice",
                "capability": "text_generation",
                "input": "合成測試",
                "max_output_tokens": 16,
            }
        )
        assert result["decision"] == "recommend"
        assert result["execution_allowed"] is False
        assert gateway_token not in json.dumps(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_duplicate_origin_and_body_framing_are_rejected(tmp_path):
    with running(tmp_path) as (server, _):
        cookie = login(server)
        for extra in ({"Transfer-Encoding": "chunked"}, {"Content-Length": "not-an-integer"}):
            status, _, raw = request(
                server, "/api/advice", method="POST", payload={}, cookie=cookie, headers=extra
            )
            assert status == 400 and json.loads(raw) == {"error": "invalid_request"}
        con = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        con.putrequest("POST", "/api/logout")
        con.putheader("Origin", f"http://127.0.0.1:{server.server_port}")
        con.putheader("Origin", "https://untrusted.invalid")
        con.putheader("X-Dashboard-Request", "1")
        con.putheader("Content-Type", "application/json")
        con.putheader("Content-Length", "2")
        con.putheader("Cookie", cookie)
        con.endheaders(b"{}")
        response = con.getresponse()
        assert response.status == 403
        response.read()
        con.close()
