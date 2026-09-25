"""Loopback JSON control plane. Provider payloads and credentials never enter it."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .core import Broker, BrokerError


def make_server(
    broker: Broker, host: str = "127.0.0.1", port: int = 18081, token: str | None = None
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("v0.1 binds loopback only")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status: int, value: dict | list):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _route(self, body: dict | None = None):
            if not self._authorized():
                return
            try:
                path = urlsplit(self.path)
                if path.query or path.fragment:
                    raise BrokerError("invalid_request", "query parameters are not accepted")
                if self.command == "GET" and path.path == "/v1/catalog":
                    result = broker.catalog()
                elif self.command == "GET" and path.path.startswith("/v1/reservations/"):
                    result = broker.status(path.path.removeprefix("/v1/reservations/"))
                elif self.command == "POST" and path.path == "/v1/reservations":
                    result = broker.reserve(body)
                elif (
                    self.command == "POST"
                    and path.path.startswith("/v1/reservations/")
                    and path.path.endswith("/dispatch")
                ):
                    rid = path.path.removeprefix("/v1/reservations/").removesuffix("/dispatch")
                    result = broker.dispatch(rid)
                elif self.command == "POST" and path.path == "/v1/reports":
                    result = broker.report(body)
                else:
                    self._send(404, {"error": "not_found"})
                    return
                self._send(200, result)
            except BrokerError as exc:
                code = 409 if exc.code in {"conflict", "invalid_transition"} else 400
                if exc.code == "unavailable":
                    code = 503
                elif exc.code == "not_found":
                    code = 404
                self._send(
                    code, {"error": exc.code, "message": str(exc), "wait_until": exc.wait_until}
                )

        def do_GET(self):
            self._route()

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384:
                    raise ValueError("invalid body length")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError("expected object")
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send(400, {"error": "invalid_request"})
                return
            self._route(body)

    return ThreadingHTTPServer((host, port), Handler)
