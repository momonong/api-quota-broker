"""Loopback-only dashboard facade with an independent browser session."""

import hmac
import json
import os
import secrets
import stat
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from .dashboard import Dashboard, DashboardError
from .gateway_server import _bounded_json, _json_depth

BODY_LIMIT = 65536
SESSION_SECONDS = 8 * 3600
ASSETS = Path(__file__).with_name("dashboard_assets")


def read_private_token(path: str | Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("private token file required (owner only)")
        token = stream.read(1025).strip()
    if not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("token must contain 32..1024 non-whitespace characters")
    return token


def make_dashboard_server(
    board: Dashboard, token: str, *, port: int = 18086, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("dashboard binds literal IPv4 loopback only")
    if not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("separate dashboard token required")
    upstream_token = getattr(board.source, "_token", None)
    if upstream_token is not None and hmac.compare_digest(token, upstream_token):
        raise ValueError("dashboard token must differ from upstream token")
    sessions: dict[str, float] = {}
    failures: list[float] = []
    lock = threading.Lock()
    work = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args: object) -> None:
            pass

        def _send(
            self,
            status: int,
            data: Any,
            *,
            cookie: str | None = None,
            content_type: str = "application/json; charset=utf-8",
        ) -> None:
            try:
                body = data if isinstance(data, bytes) else _bounded_json(data, 2 * 1024 * 1024)
            except Exception:  # noqa: BLE001 - sanitized public boundary
                status, body = 503, b'{"error":"source_unavailable"}'
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.send_header("Connection", "close")
            self.close_connection = True
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _origin(self, required: bool = False) -> bool:
            expected = f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            origins = self.headers.get_all("Origin") or []
            return (
                self.headers.get_all("Host") == [expected]
                and (
                    origins == ["http://" + expected]
                    if required
                    else origins in ([], ["http://" + expected])
                )
                and self.headers.get("Sec-Fetch-Site") in {None, "none", "same-origin"}
            )

        def _session(self) -> str | None:
            try:
                parsed = SimpleCookie()
                parsed.load(self.headers.get("Cookie", ""))
                item = parsed.get("aqb_dashboard")
                value = item.value if item else None
                with lock:
                    return value if value and sessions.get(value, 0) > time.monotonic() else None
            except Exception:  # noqa: BLE001 - malformed cookie fails closed
                return None

        def do_GET(self) -> None:
            if not self._origin():
                self._send(403, {"error": "origin_rejected"})
                return
            if self.path in {"/", "/dashboard.css", "/dashboard.js"}:
                name, kind = {
                    "/": ("index.html", "text/html"),
                    "/dashboard.css": ("dashboard.css", "text/css"),
                    "/dashboard.js": ("dashboard.js", "text/javascript"),
                }[self.path]
                self._send(200, (ASSETS / name).read_bytes(), content_type=kind + "; charset=utf-8")
                return
            if self.path != "/api/snapshot":
                self._send(404, {"error": "not_found"})
                return
            if not self._session():
                self._send(401, {"error": "unauthorized"})
                return
            self._run(board.snapshot)

        def _run(self, action: Any) -> None:
            if not work.acquire(blocking=False):
                self._send(503, {"error": "source_busy"})
                return
            try:
                self._send(200, action())
            except DashboardError as error:
                code = "invalid_task" if str(error) == "invalid_task" else "source_unavailable"
                self._send(400 if code == "invalid_task" else 503, {"error": code})
            except Exception:  # noqa: BLE001 - sanitized public boundary
                self._send(503, {"error": "source_unavailable"})
            finally:
                work.release()

        def do_POST(self) -> None:
            if self.path not in {"/api/login", "/api/logout", "/api/advice"}:
                self._send(404, {"error": "not_found"})
                return
            if not self._origin(True) or self.headers.get("X-Dashboard-Request") != "1":
                self._send(403, {"error": "origin_rejected"})
                return
            session = self._session()
            if self.path != "/api/login" and not session:
                self._send(401, {"error": "unauthorized"})
                return
            lengths = self.headers.get_all("Content-Length") or []
            if (
                len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdigit()
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
            ):
                self._send(400, {"error": "invalid_request"})
                return
            length = int(lengths[0])
            if length > BODY_LIMIT:
                self._send(413, {"error": "body_too_large"})
                return
            try:
                deadline = time.monotonic() + 5
                raw = bytearray()
                while len(raw) < length:
                    self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                    if time.monotonic() >= deadline:
                        raise ValueError
                    part = self.rfile.read1(length - len(raw))
                    if not part:
                        raise ValueError
                    raw.extend(part)
                _json_depth(bytes(raw), maximum=16)
                body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(body, dict):
                    raise TypeError
            except Exception:  # noqa: BLE001 - malformed/timeout body fails closed
                self._send(400, {"error": "invalid_request"})
                return
            if self.path == "/api/login":
                now = time.monotonic()
                with lock:
                    failures[:] = [t for t in failures if t > now - 60]
                    if len(failures) >= 5:
                        self._send(429, {"error": "login_throttled"})
                        return
                    supplied = body.get("token")
                    if (
                        set(body) != {"token"}
                        or not isinstance(supplied, str)
                        or not supplied.isascii()
                        or not hmac.compare_digest(supplied, token)
                    ):
                        failures.append(now)
                        self._send(401, {"error": "unauthorized"})
                        return
                    for key in list(sessions):
                        if sessions[key] <= now:
                            del sessions[key]
                    if len(sessions) >= 128:
                        self._send(503, {"error": "session_limit"})
                        return
                    value = secrets.token_urlsafe(32)
                    sessions[value] = now + SESSION_SECONDS
                self._send(
                    200,
                    {"state": "authenticated"},
                    cookie=f"aqb_dashboard={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}",
                )
            elif self.path == "/api/logout":
                with lock:
                    if session:
                        sessions.pop(session, None)
                self._send(
                    200,
                    {"state": "signed_out"},
                    cookie="aqb_dashboard=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                )
            else:
                self._run(lambda: board.advice(body))

    return ThreadingHTTPServer((host, port), Handler)
