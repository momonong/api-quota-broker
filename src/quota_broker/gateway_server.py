"""Required-auth loopback HTTP facade for the unified gateway."""

import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .access import AccessPolicyFile, Principal, ProjectAccess, metadata, task_metadata
from .core import stamp
from .discovery import DiscoveryError
from .families import MediaLimits
from .gateway import Gateway, GatewayError
from .queue import DurableQueue

BODY_READ_TIMEOUT = 30.0
_SERVER_SETUP_LOCK = threading.Lock()


def _json_depth(raw: bytes, maximum: int = 32) -> None:
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > maximum:
                raise ValueError("invalid JSON depth")
        elif char in (93, 125):
            depth -= 1


def _bounded_json(data: object, maximum: int) -> bytes:
    pieces = []
    size = 0
    encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    for piece in encoder.iterencode(data):
        encoded = piece.encode()
        size += len(encoded)
        if size > maximum:
            raise ValueError("response exceeds limit")
        pieces.append(encoded)
    return b"".join(pieces)


def make_gateway_server(
    gateway: Gateway,
    token: str,
    host: str = "127.0.0.1",
    port: int = 18084,
    *,
    queue: DurableQueue | None = None,
    worker: bool = False,
    admin_token: str | None = None,
    serialize_work: bool = False,
    access_policy: AccessPolicyFile | None = None,
    private_api: bool = False,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("gateway binds loopback only")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("gateway client token must be at least 32 characters")
    if admin_token is not None and (
        len(admin_token) < 32 or hmac.compare_digest(admin_token, token)
    ):
        raise ValueError("separate administrator token required")
    if private_api and access_policy is None:
        raise ValueError("private API requires scoped access policy")
    if access_policy is not None:
        reserved_digests = {
            hashlib.sha256(value.encode()).hexdigest()
            for value in (token, admin_token)
            if value is not None
        }
        if any(principal.token_sha256 in reserved_digests for principal in access_policy.read()):
            raise ValueError("scoped credentials must differ from legacy client/admin credentials")
    media_limits = getattr(gateway, "media_limits", MediaLimits())
    with _SERVER_SETUP_LOCK:
        project_access = (
            ProjectAccess(gateway, access_policy) if access_policy is not None else None
        )
        work_gate = None
        if serialize_work:
            work_gate = getattr(gateway, "_http_work_gate", None)
            if work_gate is None:
                work_gate = threading.Lock()
                gateway._http_work_gate = work_gate
    worker_state: dict[str, Any] = {
        "enabled": queue is not None and worker,
        "running": False,
        "stopped": False,
        "error_code": None,
        "last_tick_at": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def _send(self, status: int, data: object, *, close: bool = False) -> None:
            try:
                body = _bounded_json(data, media_limits.result_bytes)
            except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
                status, body = 503, b'{"error":"internal_error"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _work(self, action: Callable[[], None]) -> None:
            if work_gate is None:
                action()
                return
            if not work_gate.acquire(blocking=False):
                self._send(503, {"error": "work_busy"}, close=True)
                return
            try:
                # Also bound response writes while the gate is held.
                self.connection.settimeout(BODY_READ_TIMEOUT)
                action()
            finally:
                work_gate.release()

        def _read_body(self, length: int) -> bytes:
            if work_gate is None:
                return self.rfile.read(length)
            # An idle socket timeout alone can be kept alive by a slow drip.
            deadline = time.monotonic() + BODY_READ_TIMEOUT
            raw = bytearray()
            while len(raw) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(length - len(raw))
                if not chunk:
                    raise ValueError("incomplete body")
                raw.extend(chunk)
            self.connection.settimeout(BODY_READ_TIMEOUT)
            return bytes(raw)

        def _authorized(self) -> bool:
            self.principal: Principal | None = None
            parsed = urlsplit(self.path)
            path = parsed.path
            if private_api:
                parts = path.split("/")
                allowed = (
                    len(parts) >= 5
                    and parts[1:4] == ["quota", "api", "v1"]
                    and parts[4] in {"tasks", "jobs"}
                )
                tail = parts[5:]
                allowed = allowed and (
                    (not tail and self.command in {"GET", "POST"})
                    or (len(tail) == 1 and tail[0] != "tick" and self.command == "GET")
                    or (
                        parts[4] == "jobs"
                        and len(tail) == 2
                        and (
                            (tail[1] == "result" and self.command == "GET")
                            or (tail[1] == "cancel" and self.command == "POST")
                        )
                    )
                )
                if not allowed or parsed.fragment:
                    self._send(404, {"error": "not_found"})
                    return False
                path = "/v1/" + ("queue" if parts[4] == "jobs" else "tasks")
                if tail:
                    path += "/" + "/".join(tail)
                self.path = path + ("?" + parsed.query if parsed.query else "")
            supplied = self.headers.get("Authorization", "")
            if (
                len(self.headers.get_all("Authorization", [])) != 1
                or not supplied.isascii()
                or len(supplied) > 4103
            ):
                self._send(401, {"error": "unauthorized"})
                return False
            if not private_api:
                expected = admin_token if path.startswith("/v1/admin/") else token
                if expected is not None and hmac.compare_digest(supplied, "Bearer " + expected):
                    return True
            if access_policy is not None and supplied.startswith("Bearer "):
                try:
                    self.principal = access_policy.authenticate(supplied[7:])
                except ValueError:
                    self.principal = None
            principal = self.principal
            if principal is None:
                self._send(401, {"error": "unauthorized"})
                return False
            if "admin" in principal.scopes and not private_api:
                return True
            scope = None
            if self.command == "GET" and path in {"/v1/catalog", "/v1/model-inventory"}:
                scope = "read"
            elif self.command == "GET" and path in {"/v1/usage", "/v1/diagnostics"}:
                scope = "read" if principal.project_id is None else None
            elif self.command == "POST" and path == "/v1/routes/explain":
                scope = "plan"
            elif path == "/v1/tasks" or path.startswith("/v1/tasks/"):
                if self.command == "GET":
                    scope = (
                        "read" if principal.project_id is not None or path == "/v1/tasks" else None
                    )
                elif (
                    self.command == "POST"
                    and path == "/v1/tasks"
                    and principal.project_id is not None
                ):
                    scope = "execute"
            elif (
                (path == "/v1/queue" or path.startswith("/v1/queue/"))
                and principal.project_id is not None
                and path != "/v1/queue/tick"
            ):
                scope = "read" if self.command == "GET" else "execute"
            if scope is None or scope not in principal.scopes:
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def _project(self, parsed: Any, body: dict[str, Any] | None) -> object:
            assert project_access is not None and self.principal is not None
            principal = self.principal
            kind = "jobs" if parsed.path.startswith("/v1/queue") else "tasks"
            prefix = "/v1/queue" if kind == "jobs" else "/v1/tasks"
            tail = (
                parsed.path.removeprefix(prefix).removeprefix("/").split("/")
                if parsed.path != prefix
                else []
            )
            engine = queue if kind == "jobs" else gateway
            if engine is None:
                raise GatewayError("queue_disabled", "queue is not enabled")
            if not tail and self.command == "GET":
                return project_access.recent(
                    principal, kind, engine.status, parse_qs(parsed.query, keep_blank_values=True)
                )
            if parsed.query:
                raise GatewayError("invalid_request", "query is not supported")
            if not tail and self.command == "POST" and body is not None:
                internal, data = project_access.claim(principal, kind, body)
                result = (
                    queue.submit(data)
                    if kind == "jobs" and queue is not None
                    else gateway.run(data)
                )
                return project_access.external(result, internal, body["request_key"])
            if not tail or len(tail) > 2:
                raise GatewayError("not_found", "task endpoint not found")
            internal = project_access.lookup(principal, kind, tail[0])
            if len(tail) == 1 and self.command == "GET":
                result = engine.status(internal)
            elif kind == "jobs" and queue is not None and len(tail) == 2:
                if tail[1] == "result" and self.command == "GET":
                    result = queue.result(internal)
                elif tail[1] == "cancel" and self.command == "POST" and body == {}:
                    result = queue.cancel(internal)
                else:
                    raise GatewayError("not_found", "task endpoint not found")
            else:
                raise GatewayError("not_found", "task endpoint not found")
            return project_access.external(result, internal, tail[0])

        def _handle(self, body: dict[str, Any] | None = None) -> None:
            try:
                parsed = urlsplit(self.path)
                if parsed.fragment:
                    raise GatewayError("invalid_request", "invalid path")
                result: object
                principal = self.principal
                if (
                    principal is not None
                    and principal.project_id is not None
                    and "admin" not in principal.scopes
                    and (
                        parsed.path == "/v1/tasks"
                        or parsed.path.startswith("/v1/tasks/")
                        or parsed.path == "/v1/queue"
                        or parsed.path.startswith("/v1/queue/")
                    )
                ):
                    self._send(200, self._project(parsed, body))
                    return
                if (
                    parsed.path.startswith("/v1/admin/targets/")
                    and self.command == "POST"
                    and not parsed.query
                ):
                    parts = parsed.path.split("/")[4:]
                    if len(parts) == 2 and parts[1] == "quota" and body is not None:
                        gateway.observe_quota(parts[0], body)
                    elif len(parts) == 3 and parts[1:] == ["health", "reset"] and body == {}:
                        gateway.reset_health(parts[0])
                    else:
                        raise GatewayError("invalid_request", "invalid administrator operation")
                    result = {"state": "updated", "target_id": parts[0]}
                elif (
                    self.command == "POST"
                    and not parsed.query
                    and parsed.path in {"/v1/admin/discovery/refresh", "/v1/admin/discovery/attest"}
                ):
                    if body is None:
                        raise DiscoveryError("invalid_request", "missing discovery metadata")
                    result = (
                        gateway.discovery.refresh(body)
                        if parsed.path.endswith("/refresh")
                        else gateway.discovery.attest(body)
                    )
                elif self.command == "GET" and parsed.path == "/v1/coverage":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"provider", "model", "capability", "limit", "before"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise DiscoveryError("invalid_request", "invalid coverage filters")
                    raw_limit = query.get("limit", ["100"])[0]
                    if not raw_limit.isascii() or not raw_limit.isdigit() or len(raw_limit) > 4:
                        raise DiscoveryError("invalid_request", "invalid coverage limit")
                    result = gateway.discovery.coverage(
                        provider=query.get("provider", [None])[0],
                        model=query.get("model", [None])[0],
                        capability=query.get("capability", [None])[0],
                        limit=int(raw_limit),
                        before=query.get("before", [None])[0],
                    )
                elif self.command == "GET" and parsed.path == "/v1/coverage/candidates":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"provider"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise DiscoveryError("invalid_request", "invalid candidate filters")
                    result = gateway.discovery_candidates(provider=query.get("provider", [None])[0])
                elif parsed.path == "/v1/queue" or parsed.path.startswith("/v1/queue/"):
                    if queue is None:
                        raise GatewayError("queue_disabled", "queue is not enabled")
                    if parsed.query:
                        raise GatewayError("invalid_request", "queue query is not supported")
                    parts = parsed.path.split("/")[3:]
                    if not parts and self.command == "POST" and body is not None:
                        result = queue.submit(body)
                    elif not parts and self.command == "GET":
                        result = queue.recent()
                    elif parts == ["tick"] and self.command == "POST" and body == {}:
                        result = queue.tick("http-worker")
                    elif len(parts) == 1 and self.command == "GET":
                        result = queue.status(parts[0])
                    elif len(parts) == 2 and parts[1] == "result" and self.command == "GET":
                        result = queue.result(parts[0])
                    elif (
                        len(parts) == 2
                        and parts[1] == "cancel"
                        and self.command == "POST"
                        and body == {}
                    ):
                        result = queue.cancel(parts[0])
                    else:
                        raise GatewayError("not_found", "queue endpoint not found")
                elif self.command == "GET" and parsed.path == "/v1/catalog" and not parsed.query:
                    result = gateway.catalog()
                elif (
                    self.command == "GET"
                    and parsed.path == "/v1/model-inventory"
                    and not parsed.query
                ):
                    result = gateway.model_inventory()
                elif (
                    self.command == "GET" and parsed.path == "/v1/diagnostics" and not parsed.query
                ):
                    result = gateway.diagnostics()
                    result["queue_worker"] = dict(worker_state)
                    result["execution_policy"] = {
                        "serialize_work": serialize_work,
                        "media_input_bytes": media_limits.decoded_input_bytes,
                        "request_bytes": media_limits.request_bytes,
                        "result_bytes": media_limits.result_bytes,
                        "google_pdf_count_tokens": getattr(
                            gateway, "google_pdf_count_tokens", False
                        ),
                        "count_estimate_ttl_seconds": 120,
                        "count_margin_percent": 20,
                        "count_margin_tokens": 256,
                    }
                elif self.command == "GET" and parsed.path == "/v1/tasks":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"limit", "before", "provider", "model", "state"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise GatewayError("invalid_request", "invalid task filters")
                    if (
                        principal is not None
                        and "admin" not in principal.scopes
                        and "before" in query
                    ):
                        raise GatewayError(
                            "invalid_request", "global metadata cursors are disabled"
                        )
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
                    if set(query) - {"provider", "model", "from", "to", "task"} or any(
                        len(values) != 1 for values in query.values()
                    ):
                        raise GatewayError("invalid_request", "invalid usage filters")
                    result = gateway.usage(
                        provider=query.get("provider", [None])[0],
                        model=query.get("model", [None])[0],
                        from_at=query.get("from", [None])[0],
                        to_at=query.get("to", [None])[0],
                        task_type=query.get("task", [None])[0],
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
                    if principal is not None and project_access is not None:
                        project_access.bounds(principal, body)
                    result = gateway.explain(body)
                else:
                    self._send(404, {"error": "not_found"})
                    return
                if principal is not None and "admin" not in principal.scopes:
                    if parsed.path == "/v1/tasks":
                        result = task_metadata(result)
                    else:
                        result = metadata(result)
                self._send(200, result)
            except Exception as exc:  # noqa: BLE001 - never log provider or secret errors
                if not isinstance(exc, (GatewayError, DiscoveryError)):
                    self._send(503, {"error": "internal_error"})
                    return
                status = {
                    "invalid_request": 400,
                    "forbidden": 403,
                    "project_limit": 429,
                    "not_found": 404,
                    "conflict": 409,
                    "unavailable": 503,
                }.get(exc.code, 503)
                self._send(
                    status,
                    {
                        "error": exc.code,
                        "message": str(exc),
                        **(
                            {"details": exc.details}
                            if isinstance(exc, GatewayError) and exc.details is not None
                            else {}
                        ),
                        "wait_until": getattr(exc, "wait_until", None),
                    },
                )

        def do_GET(self) -> None:
            if self._authorized():
                parts = urlsplit(self.path).path.split("/")
                if len(parts) == 5 and parts[1:3] == ["v1", "queue"] and parts[4] == "result":
                    self._work(self._handle)
                else:
                    self._handle()

        def do_POST(self) -> None:
            if self._authorized():
                self._work(self._post)

        def _post(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                max_bytes = (
                    min(5 * 1024 * 1024, media_limits.request_bytes)
                    if urlsplit(self.path).path == "/v1/admin/discovery/refresh"
                    else media_limits.request_bytes
                )
                if not 0 < length <= max_bytes:
                    raise ValueError
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError
                raw = self._read_body(length)
                _json_depth(raw)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise TypeError
            except TimeoutError:
                self._send(408, {"error": "request_timeout"}, close=True)
                return
            except (ValueError, TypeError, RecursionError, UnicodeError):
                self._send(400, {"error": "invalid_request"}, close=work_gate is not None)
                return
            self._handle(body)

    class GatewayServer(ThreadingHTTPServer):
        def __init__(self) -> None:
            super().__init__((host, port), Handler)
            self.stop_worker = threading.Event()
            self.worker_thread: threading.Thread | None = None

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            if queue is not None and worker:
                worker_id = "server-" + uuid.uuid4().hex

                def process() -> None:
                    worker_state["running"] = True
                    while not self.stop_worker.is_set():
                        try:
                            if work_gate is None:
                                queue.tick(worker_id)
                                worker_state["last_tick_at"] = stamp(gateway.clock())
                            elif work_gate.acquire(blocking=False):
                                try:
                                    queue.tick(worker_id)
                                    worker_state["last_tick_at"] = stamp(gateway.clock())
                                finally:
                                    work_gate.release()
                        except Exception:  # noqa: BLE001 - no content or credential logs
                            # A broken configuration stops processing; leases preserve recovery.
                            self.stop_worker.set()
                            worker_state["error_code"] = "worker_stopped"
                        self.stop_worker.wait(0.5)
                    worker_state["running"] = False
                    worker_state["stopped"] = True

                self.worker_thread = threading.Thread(target=process, daemon=True)
                self.worker_thread.start()
            try:
                super().serve_forever(poll_interval)
            finally:
                self.stop_worker.set()

        def server_close(self) -> None:
            self.stop_worker.set()
            super().server_close()
            if self.worker_thread is not None:
                self.worker_thread.join(timeout=1)

    return GatewayServer()
