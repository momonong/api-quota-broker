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

    class InertUpstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            payload = json.dumps({"fixture": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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

    def get(path):
        connection = HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Host": "momonong.me",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Remote-User": "admin",
                    "Authorization": "Bearer synthetic-no-authority",
                },
            )
            response = connection.getresponse()
            response.read()
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
        for path in [
            "/quota/api/v1/tasks",
            "/quota/api/v1/jobs",
            "/quota/api/admin/quota",
            "/quota/api/maintenance",
            "/quota/v1/queue/tick",
            "/quota/anything",
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
    finally:
        process.terminate()
        process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)
