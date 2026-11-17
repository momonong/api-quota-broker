"""Run the actual Caddy matcher against an inert loopback upstream, never a Broker."""

import importlib.util
import json
import shutil
import socket
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_public_routes_never_proxy_execution_admin_or_maintenance(tmp_path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy binary required for public ingress integration")
    spec = importlib.util.spec_from_file_location(
        "quota_public_gate", Path(__file__).parents[1] / "deploy/quota-web/gate.py"
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    requests = []
    forwarded_headers = []
    response_headers = []

    class InertUpstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            forwarded_headers.append({name.lower(): value for name, value in self.headers.items()})
            payload = json.dumps({"fixture": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), InertUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Replace only fixture port. All production matchers/handler ordering are unchanged.
    routes = gate.CADDY.decode().replace("127.0.0.1:18095", f"127.0.0.1:{upstream.server_port}")
    config = tmp_path / "Caddyfile"
    config.write_text(f"{{\n admin off\n}}\nhttp://:{port} {{\n bind 127.0.0.1\n{routes}\n}}\n")
    process = subprocess.Popen(
        [caddy, "run", "--config", str(config), "--adapter", "caddyfile"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def get(path, *, scheme="https", host="momonong.me", method="GET"):
        connection = HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request(
                method,
                path,
                headers={
                    "Host": host,
                    "X-Forwarded-Proto": scheme,
                    "X-Forwarded-For": "203.0.113.9",
                    "X-Remote-User": "admin",
                    "Authorization": "Bearer synthetic-no-authority",
                    "Proxy-Authorization": "synthetic-no-authority",
                    "Forwarded": "for=192.0.2.25",
                    "X-Auth-User": "synthetic-no-authority",
                    "X-Original-URL": "/quota/api/maintenance",
                    "Cf-Access-Jwt-Assertion": "synthetic-no-authority",
                },
            )
            response = connection.getresponse()
            response.read()
            response_headers.append(dict(response.getheaders()))
            return response.status
        finally:
            connection.close()

    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                assert get("/quota/api/snapshot") == 200
                break
            except (OSError, AssertionError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("inert Caddy fixture did not become ready")
                time.sleep(0.02)
        assert requests == ["/quota/api/snapshot"]
        requests.clear()
        assert get("/quota") == 308
        assert response_headers[-1]["Location"] == "/quota/"
        assert get("/quota/api/snapshot", scheme="http") == 308
        assert response_headers[-1]["Location"] == "https://momonong.me/quota/api/snapshot"
        get("/quota/api/snapshot", host="other.invalid")
        assert requests == []
        for path in [
            "/quota/api/v1/tasks",
            "/quota/api/v1/jobs",
            "/quota/api/admin/quota",
            "/quota/api/maintenance",
            "/quota/v1/queue/tick",
            "/quota/anything",
            "/quota/api/advice/execute",
        ]:
            assert get(path) == 404
        assert requests == []
        for path in [
            "/quota/",
            "/quota/dashboard.js",
            "/quota/dashboard.css",
            "/quota/api/usage?provider=google",
            "/quota/api/advice",
        ]:
            assert get(path) == 200
        assert len(requests) == 5
        for path in ["/quota/api/login", "/quota/api/logout", "/quota/api/advice"]:
            assert get(path, method="POST") == 200
        assert len(requests) == 8
        for headers in forwarded_headers:
            assert headers["host"] == "momonong.me"
            assert headers["x-forwarded-proto"] == "https"
            assert headers["x-forwarded-for"] == "127.0.0.1"
            assert not {"authorization", "proxy-authorization", "forwarded"} & headers.keys()
            assert not any(
                name.startswith(("x-remote-", "x-auth-", "x-original-", "cf-access-"))
                for name in headers
            )
        assert response_headers[-1]["Cache-Control"] == "no-store"
    finally:
        process.terminate()
        process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)
