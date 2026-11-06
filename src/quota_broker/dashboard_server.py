"""Loopback-only dashboard facade with an independent browser session."""

import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from .dashboard import Dashboard, DashboardError
from .dashboard_auth import BrowserAuthError, BrowserPasswordFile
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
    board: Dashboard,
    token: str | None = None,
    *,
    password_file: str | Path | BrowserPasswordFile | None = None,
    port: int = 18086,
    host: str = "127.0.0.1",
    public_base_url: str | None = None,
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("dashboard binds literal IPv4 loopback only")
    if (token is None) == (password_file is None):
        raise ValueError("exactly one browser authentication mode required")
    password_reader = (
        password_file
        if isinstance(password_file, BrowserPasswordFile)
        else BrowserPasswordFile(password_file)
        if password_file is not None
        else None
    )
    if password_reader is not None:
        password_reader.read()  # Startup fails closed; never falls back to a token.
    if token is not None and (
        not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token)
    ):
        raise ValueError("separate dashboard token required")
    for source in (board.source, board.activity):
        upstream_token = getattr(source, "_token", None)
        if (
            token is not None
            and upstream_token is not None
            and hmac.compare_digest(token, upstream_token)
        ):
            raise ValueError("dashboard token must differ from upstream token")
    public_origin = None
    if public_base_url is not None:
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme != "https"
            or parsed.path != "/quota/"
            or not parsed.hostname
            or parsed.netloc != parsed.hostname
            or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}", parsed.hostname)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HTTPS public base URL with /quota/ required")
        public_origin = "https://" + parsed.netloc
        if public_base_url != public_origin + "/quota/":
            raise ValueError("canonical HTTPS public base URL required")
    prefix = "/quota/" if public_origin else "/"
    cookie_name = "__Secure-aqb_quota" if public_origin else "aqb_dashboard"
    cookie_flags = (
        f"Path={prefix}; " + ("Secure; " if public_origin else "") + "HttpOnly; SameSite=Strict"
    )
    routes = {
        prefix + suffix: "/" + suffix
        for suffix in (
            "",
            "dashboard.css",
            "dashboard.js",
            "api/snapshot",
            "api/usage",
            "api/login",
            "api/logout",
            "api/advice",
        )
    }

    sessions: dict[str, tuple[float, str | None]] = {}
    failures: list[float] = []
    lock = threading.Lock()
    work = threading.Lock()
    login_work = threading.Lock()

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
            location: str | None = None,
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
            if location:
                self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _origin(self, required: bool = False) -> bool:
            expected = (
                public_origin.removeprefix("https://")
                if public_origin
                else f"127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}"
            )
            origin = public_origin or "http://" + expected
            origins = self.headers.get_all("Origin") or []
            return (
                self.headers.get_all("Host") == [expected]
                and (origins == [origin] if required else origins in ([], [origin]))
                and self.headers.get("Sec-Fetch-Site") in {None, "none", "same-origin"}
            )

        def _session(self) -> str | None:
            try:
                revision = password_reader.read().revision if password_reader else None
                parsed = SimpleCookie()
                parsed.load(self.headers.get("Cookie", ""))
                item = parsed.get(cookie_name)
                value = item.value if item else None
                with lock:
                    item_session = sessions.get(value) if value else None
                    if (
                        item_session
                        and item_session[0] > time.monotonic()
                        and item_session[1] == revision
                    ):
                        return value
                    if value:
                        sessions.pop(value, None)
                    return None
            except BrowserAuthError:
                with lock:
                    sessions.clear()
                return None
            except Exception:  # noqa: BLE001 - malformed cookie fails closed
                return None

        def do_GET(self) -> None:
            if not self._origin():
                self._send(403, {"error": "origin_rejected"})
                return
            if public_origin and self.path == "/quota":
                self._send(308, b"", location=prefix)
                return
            parsed = urlsplit(self.path)
            route = routes.get(parsed.path) if not parsed.scheme and not parsed.netloc else None
            if parsed.fragment or ("?" in self.path and route != "/api/usage"):
                self._send(404, {"error": "not_found"})
                return
            if route in {"/", "/dashboard.css", "/dashboard.js"}:
                name, kind = {
                    "/": ("index.html", "text/html"),
                    "/dashboard.css": ("dashboard.css", "text/css"),
                    "/dashboard.js": ("dashboard.js", "text/javascript"),
                }[route]
                self._send(200, (ASSETS / name).read_bytes(), content_type=kind + "; charset=utf-8")
                return
            if route not in {"/api/snapshot", "/api/usage"} or parsed.fragment:
                self._send(404, {"error": "not_found"})
                return
            if not self._session():
                self._send(401, {"error": "unauthorized"})
                return
            if route == "/api/usage":
                try:
                    if len(parsed.query) > 512:
                        raise ValueError
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                    if any(len(values) != 1 for values in query.values()):
                        raise ValueError
                    filters = {key: values[0] for key, values in query.items()}
                except ValueError:
                    self._send(400, {"error": "invalid_filters"})
                    return
                self._run(lambda: board.usage_report(filters))
            else:
                self._run(board.snapshot)

        def _run(self, action: Any) -> None:
            if not self._session():
                self._send(401, {"error": "unauthorized"})
                return
            if not work.acquire(blocking=False):
                self._send(503, {"error": "source_busy"})
                return
            try:
                result = action()
                if not self._session():
                    self._send(401, {"error": "unauthorized"})
                    return
                self._send(200, result)
            except DashboardError as error:
                code = (
                    str(error)
                    if str(error) in {"invalid_task", "invalid_filters"}
                    else "source_unavailable"
                )
                self._send(
                    400 if code in {"invalid_task", "invalid_filters"} else 503, {"error": code}
                )
            except Exception:  # noqa: BLE001 - sanitized public boundary
                self._send(503, {"error": "source_unavailable"})
            finally:
                work.release()

        def do_POST(self) -> None:
            route = routes.get(self.path)
            if route not in {"/api/login", "/api/logout", "/api/advice"}:
                self._send(404, {"error": "not_found"})
                return
            if not self._origin(True) or self.headers.get("X-Dashboard-Request") != "1":
                self._send(403, {"error": "origin_rejected"})
                return
            session = self._session()
            if route != "/api/login" and not session:
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
            if route == "/api/login":
                self._login(body)
            elif route == "/api/logout":
                with lock:
                    if session:
                        sessions.pop(session, None)
                self._send(
                    200,
                    {"state": "signed_out"},
                    cookie=f"{cookie_name}=; {cookie_flags}; Max-Age=0",
                )
            else:
                self._run(lambda: board.advice(body))

        def _login(self, body: dict[str, Any]) -> None:
            if not login_work.acquire(blocking=False):
                self._send(429, {"error": "login_throttled"})
                return
            try:
                now = time.monotonic()
                with lock:
                    failures[:] = [t for t in failures if t > now - 60]
                    throttled = len(failures) >= 5
                if throttled:
                    self._send(429, {"error": "login_throttled"})
                    return
                supplied = body.get("token")
                valid = False
                revision = None
                if set(body) == {"token"}:
                    if password_reader:
                        try:
                            before = password_reader.read()
                            valid = before.verify(supplied)
                            after = password_reader.read()
                            valid = valid and before == after
                            revision = after.revision
                        except BrowserAuthError:
                            pass
                    else:
                        valid = (
                            isinstance(supplied, str)
                            and supplied.isascii()
                            and hmac.compare_digest(supplied, cast(str, token))
                        )
                if not valid:
                    with lock:
                        failures.append(time.monotonic())
                    self._send(401, {"error": "unauthorized"})
                    return
                with lock:
                    now = time.monotonic()
                    for key in list(sessions):
                        if sessions[key][0] <= now or sessions[key][1] != revision:
                            del sessions[key]
                    if len(sessions) >= 128:
                        self._send(503, {"error": "session_limit"})
                        return
                    value = secrets.token_urlsafe(32)
                    sessions[value] = (now + SESSION_SECONDS, revision)
                self._send(
                    200,
                    {"state": "authenticated"},
                    cookie=f"{cookie_name}={value}; {cookie_flags}; Max-Age={SESSION_SECONDS}",
                )
            finally:
                login_work.release()

    return ThreadingHTTPServer((host, port), Handler)
