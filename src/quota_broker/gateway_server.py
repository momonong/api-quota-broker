"""Required-auth loopback HTTP facade for the unified gateway."""

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .gateway import Gateway, GatewayError


def make_gateway_server(
    gateway: Gateway, token: str, host: str = "127.0.0.1", port: int = 18084
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("gateway binds loopback only")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("gateway client token must be at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def _send(self, status: int, data: object) -> None:
            body = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _handle(self, body: dict[str, Any] | None = None) -> None:
            try:
                parsed = urlsplit(self.path)
                if parsed.fragment:
                    raise GatewayError("invalid_request", "invalid path")
                result: object
                if self.command == "GET" and parsed.path == "/v1/catalog" and not parsed.query:
                    result = gateway.catalog()
                elif (
                    self.command == "GET" and parsed.path == "/v1/diagnostics" and not parsed.query
                ):
                    result = gateway.diagnostics()
                elif self.command == "GET" and parsed.path == "/v1/tasks":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"limit", "before", "provider", "model", "state"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise GatewayError("invalid_request", "invalid task filters")
                    raw_limit = query.get("limit", ["20"])[0]
                    if not raw_limit.isascii() or not raw_limit.isdigit() or len(raw_limit) > 3:
                        raise GatewayError("invalid_request", "invalid task limit")
                    result = gateway.recent(
                        limit=int(raw_limit),
                        before=query.get("before", [None])[0],
                        provider=query.get("provider", [None])[0],
                        model=query.get("model", [None])[0],
                        state=query.get("state", [None])[0],
                    )
                elif (
                    self.command == "GET"
                    and parsed.path.startswith("/v1/tasks/")
                    and not parsed.query
                ):
                    result = gateway.status(parsed.path.removeprefix("/v1/tasks/"))
                elif self.command == "GET" and parsed.path == "/v1/usage":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"provider", "model", "from", "to"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise GatewayError("invalid_request", "invalid usage filters")
                    result = gateway.usage(
                        provider=query.get("provider", [None])[0],
                        model=query.get("model", [None])[0],
                        from_at=query.get("from", [None])[0],
                        to_at=query.get("to", [None])[0],
                    )
                elif self.command == "POST" and parsed.path == "/v1/tasks" and not parsed.query:
                    if body is None:
                        raise GatewayError("invalid_request", "missing body")
                    result = gateway.run(body)
                elif (
                    self.command == "POST"
                    and parsed.path == "/v1/routes/explain"
                    and not parsed.query
                ):
                    if body is None:
                        raise GatewayError("invalid_request", "missing body")
                    result = gateway.explain(body)
                else:
                    self._send(404, {"error": "not_found"})
                    return
                self._send(200, result)
            except Exception as exc:  # noqa: BLE001 - never log provider or secret errors
                if not isinstance(exc, GatewayError):
                    self._send(503, {"error": "internal_error"})
                    return
                status = {
                    "invalid_request": 400,
                    "not_found": 404,
                    "conflict": 409,
                    "unavailable": 503,
                }.get(exc.code, 503)
                self._send(
                    status, {"error": exc.code, "message": str(exc), "wait_until": exc.wait_until}
                )

        def do_GET(self) -> None:
            if self._authorized():
                self._handle()

        def do_POST(self) -> None:
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65_536:
                    raise ValueError
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send(400, {"error": "invalid_request"})
                return
            self._handle(body)

    return ThreadingHTTPServer((host, port), Handler)
