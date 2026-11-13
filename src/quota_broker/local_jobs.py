"""Pinned local durable jobs. A committed dispatch intent is never sent twice."""

import base64
import hmac
import io
import json
import re
import sqlite3
import ssl
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .access import ProjectAccess
from .core import canonical
from .crypto_config import private_file, unique_json
from .gateway import GatewayError
from .queue import DurableQueue

TERMINAL = {"completed", "failed", "expired", "cancelled"}


@dataclass(frozen=True)
class LocalTarget:
    target_id: str
    base_url: str
    deployment_id: str
    model: str

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.target_id)
            or url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or not re.fullmatch(r"dep_[0-9a-f]{64}", self.deployment_id)
            or self.model != "Qwen/Qwen3.5-4B"
        ):
            raise ValueError("invalid approved local target")

    @property
    def identity(self) -> str:
        return canonical(asdict(self))


class Backend(Protocol):
    target: LocalTarget

    def submit(self, key: str, data: dict[str, Any]) -> dict[str, Any]: ...
    def lookup(self, key: str) -> dict[str, Any] | None: ...
    def status(self, jid: str) -> dict[str, Any] | None: ...
    def result(self, jid: str) -> dict[str, Any]: ...
    def cancel(self, jid: str) -> dict[str, Any]: ...


class LocalHTTPBackend:
    def __init__(
        self,
        target: LocalTarget,
        token_file: str | Path,
        *,
        ca_file: str | None = None,
        exact_lookup: bool = False,
    ):
        self.target, self.token_file, self.exact_lookup = target, Path(token_file), exact_lookup
        self.context = ssl.create_default_context(cafile=ca_file)

    @classmethod
    def load(cls, path: str | Path) -> "LocalHTTPBackend":
        with Path(path).open("rb") as stream:
            data = unique_json(stream.read(65537))
        if (
            set(data) != {"version", "target", "token_file", "ca_file", "exact_lookup"}
            or data["version"] != 1
            or type(data["exact_lookup"]) is not bool
        ):
            raise ValueError("invalid local configuration")
        return cls(
            LocalTarget(**data["target"]),
            data["token_file"],
            ca_file=data["ca_file"],
            exact_lookup=data["exact_lookup"],
        )

    def _http(
        self, path: str, body: dict[str, Any] | None = None, key: str | None = None
    ) -> dict[str, Any] | None:
        from .secure_client import exchange

        private_file(self.token_file)
        with self.token_file.open() as stream:
            token = stream.read(4097).strip()
        if not 32 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid worker credential")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        status, response_headers, raw = exchange(
            self.target.base_url + path,
            None if body is None else canonical(body).encode(),
            headers,
            30,
            self.context,
        )
        if (
            len(raw) > 2 * 1024 * 1024
            or response_headers.get("content-type", "").split(";", 1)[0] != "application/json"
        ):
            raise ValueError("invalid worker response")
        if status == 404 and body is None:
            return None
        if status not in {200, 201}:
            raise ValueError("worker unavailable")
        from .gateway_server import _json_depth

        _json_depth(raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("invalid worker response")
        return data

    def submit(self, key: str, data: dict[str, Any]) -> dict[str, Any]:
        result = self._http("/v1/jobs", data, key)
        assert result is not None
        return result

    def lookup(self, key: str) -> dict[str, Any] | None:
        if not self.exact_lookup:
            return None  # Existing backend has no exact recovery route. Never POST to find it.
        if not re.fullmatch(r"quota-[0-9a-f]{32}", key):
            raise ValueError("invalid downstream identity")
        return self._http("/v1/jobs/by-idempotency/" + key)

    @staticmethod
    def _path(jid: str) -> str:
        if not re.fullmatch(r"job_[0-9a-f]{32}", jid):
            raise ValueError("invalid downstream identity")
        return "/v1/jobs/" + jid

    def status(self, jid: str) -> dict[str, Any] | None:
        return self._http(self._path(jid))

    def result(self, jid: str) -> dict[str, Any]:
        result = self._http(self._path(jid) + "/result")
        if result is None:
            raise ValueError("worker result unavailable")
        return result

    def cancel(self, jid: str) -> dict[str, Any]:
        result = self._http(self._path(jid) + "/cancel", {})
        assert result is not None
        return result


class LocalJobs:
    def __init__(self, storage: DurableQueue, access: ProjectAccess, backend: Backend):
        self.storage, self.access, self.backend = storage, access, backend
        self.gateway, self.db = access.gateway, access.gateway.db
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS local_jobs (
                request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                target TEXT NOT NULL, downstream_key TEXT NOT NULL UNIQUE,
                downstream_id TEXT, intent INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL, result_state TEXT NOT NULL DEFAULT 'none',
                created REAL NOT NULL, deadline REAL NOT NULL, ended REAL, expires REAL,
                payload BLOB, result BLOB, error_code TEXT, cancel_intent INTEGER NOT NULL DEFAULT 0)""")

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        target = self.backend.target
        if (
            data.get("execution_policy") != "local_only"
            or data.get("provider") not in (None, "selfhost")
            or data.get("model") not in (None, target.model)
            or data["capability"] not in {"text_generation", "vision"}
            or not 1 <= data["max_output_tokens"] <= 1024
            or set(data.get("requirements", {}).get("features", [])) - {"text", "vision"}
            or set(data.get("options", {})) - {"temperature", "top_p", "stop"}
        ):
            raise GatewayError("local_unsupported", "local capability or model is unsupported")
        options = data.get("options", {})
        stops = options.get("stop", [])
        if options.get("top_p", 1) <= 0 or len(stops) > 4 or any(len(stop) > 200 for stop in stops):
            raise GatewayError("local_unsupported", "local generation option bound exceeded")
        messages = data["input"]
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        else:
            messages = messages["messages"]
        if not 1 <= len(messages) <= 64:
            raise GatewayError("local_unsupported", "local message count exceeded")
        converted, images, text_bytes = [], 0, 0
        for message in messages:
            if set(message) != {"role", "content"} or message["role"] not in {
                "system",
                "user",
                "assistant",
            }:
                raise GatewayError("local_unsupported", "unsupported local message")
            content = message["content"]
            if isinstance(content, str):
                text_bytes += len(content.encode())
            else:
                if message["role"] != "user" or not 1 <= len(content) <= 8:
                    raise GatewayError("local_unsupported", "unsupported local content parts")
                parts = []
                for part in content:
                    if part.get("type") == "text" and set(part) == {"type", "text"}:
                        parts.append(part)
                        text_bytes += len(part["text"].encode())
                    elif (
                        part.get("type") == "image"
                        and set(part) == {"type", "mime_type", "data"}
                        and part["mime_type"] == "image/png"
                        and len(part["data"]) <= 4 * ((1024 * 1024 + 2) // 3)
                        and message["role"] == "user"
                    ):
                        self._png(part["data"])
                        images += 1
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:" + part["mime_type"] + ";base64," + part["data"]
                                },
                            }
                        )
                    else:
                        raise GatewayError("local_unsupported", "unsupported local media")
                content = parts
            converted.append({"role": message["role"], "content": content})
        if (
            images > 1
            or text_bytes + len(messages) * 64 + images * 4096 + data["max_output_tokens"] > 8192
        ):
            raise GatewayError("local_unsupported", "local context bound exceeded")
        return {
            "deployment_id": target.deployment_id,
            "operation": "chat",
            "input": {
                "model": target.model,
                "messages": converted,
                "max_tokens": data["max_output_tokens"],
                **data.get("options", {}),
            },
            "urgent": False,
            "depends_on": [],
            "queue_timeout_seconds": 900,
            "execution_timeout_seconds": 60,
        }

    @staticmethod
    def _png(encoded: str) -> None:
        try:
            from PIL import Image
        except ImportError:
            raise GatewayError(
                "local_unavailable", "local image validation dependency missing"
            ) from None
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 1048576:
                raise ValueError
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format != "PNG" or picture.width > 1024 or picture.height > 1024:
                    raise ValueError
                picture.verify()
        except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
            raise GatewayError(
                "local_unsupported", "local image requires valid PNG up to 1024x1024"
            ) from None

    def catalog(self, project_id: str | None = None) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db)) as con:
            owners = [
                r[0]
                for r in con.execute(
                    "SELECT o.project_id FROM gateway_project_objects o LEFT JOIN local_jobs l ON l.request_key=o.internal_key "
                    "WHERE o.execution_policy='local_only' AND (l.request_key IS NULL "
                    "OR l.state NOT IN ('completed','failed','expired','cancelled') "
                    "OR l.result_state IN ('pending','storage_failed'))"
                )
            ]
        can_queue = len(owners) < 8 and (project_id is None or owners.count(project_id) < 2)
        return {
            "target_id": self.backend.target.target_id,
            "provider": "selfhost",
            "model": self.backend.target.model,
            "capabilities": ["text_generation", "vision"],
            "execution_policy": "local_only",
            "execution_location": "local",
            "registered": True,
            "loaded": None,
            "can_queue": can_queue,
            "inflight": len(owners),
            "blocking_reasons": [] if can_queue else ["local_queue_full"],
            "can_execute_now": None,
            "readiness_basis": "not_live_probed",
            "max_context_tokens": 8192,
        }

    def explain(self, data: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        self.validate(data)
        candidate = self.catalog(project_id)
        return {
            "execution_policy": "local_only",
            "execution_location": "local",
            "selected_target_id": self.backend.target.target_id if candidate["can_queue"] else None,
            "model": self.backend.target.model,
            "candidates": [candidate],
            "temporary": not candidate["can_queue"],
            "permanent_rejection": False,
            "readiness_basis": "not_live_probed",
        }

    def submit(self, data: dict[str, Any]) -> dict[str, Any]:
        self.validate(data)
        key = data["request_key"]
        fingerprint = self.gateway._hmac(data)
        now = self.gateway.clock().timestamp()
        deadline = min(now + 900, datetime.fromisoformat(data["deadline"]).timestamp())
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT fingerprint FROM local_jobs WHERE request_key=?", (key,)
            ).fetchone()
            if row:
                if not hmac.compare_digest(row[0], fingerprint):
                    raise GatewayError("conflict", "local request identity differs")
            else:
                owner = con.execute(
                    "SELECT execution_policy,content_encrypted FROM gateway_project_objects WHERE internal_key=?",
                    (key,),
                ).fetchone()
                if not owner or owner != ("local_only", 1):
                    raise GatewayError("forbidden", "local project ownership required")
                con.execute(
                    "INSERT INTO local_jobs (request_key,fingerprint,target,downstream_key,state,created,deadline,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        fingerprint,
                        self.backend.target.identity,
                        "quota-" + uuid.uuid4().hex,
                        "queued",
                        now,
                        deadline,
                        self.storage._encrypt(data, key, "local-payload"),
                    ),
                )
        return self.status(key)

    def _row(self, con: sqlite3.Connection, key: str) -> sqlite3.Row:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM local_jobs WHERE request_key=?", (key,)).fetchone()
        if row is None:
            raise GatewayError("not_found", "local job not found")
        return row

    def _collect(self, con: sqlite3.Connection) -> None:
        con.execute(
            "UPDATE local_jobs SET payload=NULL,result=NULL WHERE expires<=? AND state!='unknown' AND result_state IN ('available','none')",
            (self.gateway.clock().timestamp(),),
        )

    def status(self, key: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db)) as con, con:
            self._collect(con)
            row = self._row(con, key)
            target = json.loads(row["target"])
            return {
                "request_key": key,
                "state": row["state"],
                "result_state": row["result_state"],
                "result_available": row["result"] is not None,
                "error_code": row["error_code"],
                "execution_policy": "local_only",
                "execution_location": "local",
                "target_id": target["target_id"],
                "model": target["model"],
            }

    def result(self, key: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db)) as con, con:
            self._collect(con)
            row = self._row(con, key)
            if row["result"] is None:
                raise GatewayError(
                    "queue_result_pending" if row["expires"] is None else "queue_content_expired",
                    "local result unavailable",
                )
            return self.storage._decrypt(row["result"], key, "local-result")

    def _authorized(self, con: sqlite3.Connection, key: str) -> bool:
        row = con.execute(
            "SELECT token_sha256,project_id,input_bytes,output_tokens,content_encrypted FROM gateway_project_objects WHERE internal_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return False
        try:
            principal = self.access.policy.authenticate_digest(row[0])
        except ValueError:
            return False
        return bool(
            principal
            and principal.project_id == row[1]
            and "execute" in principal.scopes
            and "local_only" in principal.allowed_execution
            and row[4]
            and row[2] <= principal.max_input_bytes
            and row[3] <= principal.max_output_tokens
        )

    def tick(self) -> None:
        with closing(sqlite3.connect(self.db)) as con, con:
            self._collect(con)
            keys = [
                row[0]
                for row in con.execute(
                    "SELECT request_key FROM local_jobs WHERE state NOT IN ('completed','failed','expired','cancelled') OR result_state IN ('pending','storage_failed') ORDER BY created LIMIT 8"
                )
            ]
        for key in keys:
            self._advance(key)

    def _advance(self, key: str) -> None:
        dispatch = None
        now = self.gateway.clock().timestamp()
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = self._row(con, key)
            if row["target"] != self.backend.target.identity:
                con.execute(
                    "UPDATE local_jobs SET state=?,error_code='local_configuration_changed',"
                    "payload=CASE WHEN intent=0 THEN NULL ELSE payload END,"
                    "ended=CASE WHEN intent=0 THEN ? ELSE ended END,"
                    "expires=CASE WHEN intent=0 THEN ? ELSE expires END WHERE request_key=?",
                    ("unknown" if row["intent"] else "failed", now, now, key),
                )
                return
            if not row["intent"]:
                if row["state"] != "queued":
                    return
                if now >= row["deadline"] or not self._authorized(con, key):
                    con.execute(
                        "UPDATE local_jobs SET state=?,error_code=?,payload=NULL,ended=?,expires=? WHERE request_key=?",
                        (
                            "expired" if now >= row["deadline"] else "failed",
                            "local_admission_stopped",
                            now,
                            now,
                            key,
                        ),
                    )
                    return
                data = self.storage._decrypt(row["payload"], key, "local-payload")
                dispatch = self.validate(data)
                dispatch["queue_timeout_seconds"] = max(1, int(row["deadline"] - now))
                # Persist before any POST. All future recovery uses GET, including
                # a crash in the following gap where no bytes were actually sent.
                con.execute(
                    "UPDATE local_jobs SET intent=1,state='unknown' WHERE request_key=?", (key,)
                )
        try:
            remote: dict[str, Any] | None
            if dispatch is not None:
                remote = self.backend.submit(row["downstream_key"], dispatch)
            elif row["downstream_id"] is None:
                remote = self.backend.lookup(row["downstream_key"])
            else:
                remote = self.backend.status(row["downstream_id"])
            if remote is None:
                raise ValueError("original job unavailable")
            self._record(key, remote)
            with closing(sqlite3.connect(self.db)) as con:
                pending_cancel = self._row(con, key)["cancel_intent"] == 1
            if pending_cancel:
                self.cancel(key)
        except (OSError, ValueError, TypeError, KeyError, HTTPException, GatewayError):
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute(
                    "UPDATE local_jobs SET state='unknown',error_code='local_outcome_unknown' WHERE request_key=? AND state NOT IN ('completed','failed','expired','cancelled')",
                    (key,),
                )

    def _record(self, key: str, remote: dict[str, Any]) -> None:
        if not isinstance(remote, dict):
            raise TypeError("invalid local receipt")
        jid, state, result_state = remote.get("id"), remote.get("state"), remote.get("result_state")
        states = {
            "queued": "queued",
            "blocked": "waiting",
            "running": "running",
            "dispatching": "running",
            "draining": "running",
            "canceled": "cancelled",
            "dependency_failed": "failed",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "expired": "expired",
            "unknown": "unknown",
        }
        if (
            not isinstance(jid, str)
            or not re.fullmatch(r"job_[0-9a-f]{32}", jid)
            or remote.get("deployment") != self.backend.target.deployment_id
            or state not in states
            or result_state not in {"none", "pending", "available", "storage_failed"}
        ):
            raise ValueError("invalid local receipt")
        # The submission receipt may already name the job while its result
        # download fails. Save that durable recovery handle before the next GET.
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            prior = self._row(con, key)
            if prior["downstream_id"] not in (None, jid):
                raise ValueError("downstream identity changed")
            con.execute("UPDATE local_jobs SET downstream_id=? WHERE request_key=?", (jid, key))
        result = None
        mapped = states[state]
        if result_state == "available":
            raw = self.backend.result(jid)
            if not isinstance(raw, dict):
                raise TypeError("invalid local result")
            # Keep the familiar outcome shape; no provider usage is invented.
            choices = raw.get("choices")
            if mapped == "completed":
                if (
                    not isinstance(choices, list)
                    or len(choices) != 1
                    or not isinstance(choices[0], dict)
                    or not isinstance(choices[0].get("message"), dict)
                    or not isinstance(choices[0]["message"].get("content"), str)
                ):
                    raise ValueError("invalid local result")
                result = {
                    "request_key": key,
                    "state": mapped,
                    "execution_policy": "local_only",
                    "execution_location": "local",
                    "model": self.backend.target.model,
                    "result": {"text": choices[0]["message"]["content"]},
                    "finish_reason": choices[0].get("finish_reason"),
                    "usage_source": "unreported",
                }
            else:
                result = {
                    "request_key": key,
                    "state": mapped,
                    "error_code": "local_generation_failed",
                    "execution_policy": "local_only",
                    "execution_location": "local",
                }
        now = self.gateway.clock().timestamp()
        ended = remote.get("ended")
        if ended is not None and (type(ended) not in {int, float} or not 0 <= ended <= now + 120):
            raise ValueError("invalid completion time")
        expires = (
            (ended if ended is not None else now) + 3600
            if mapped in TERMINAL and result_state in {"none", "available"}
            else None
        )
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            prior = self._row(con, key)
            if prior["downstream_id"] not in (None, jid):
                raise ValueError("downstream identity changed")
            con.execute(
                "UPDATE local_jobs SET downstream_id=?,state=?,result_state=?,ended=COALESCE(ended,?),expires=COALESCE(expires,?),result=?,error_code=? WHERE request_key=?",
                (
                    jid,
                    mapped,
                    result_state,
                    ended,
                    expires,
                    self.storage._encrypt(result, key, "local-result")
                    if result is not None
                    else None,
                    "local_generation_failed" if mapped in {"failed", "unknown"} else None,
                    key,
                ),
            )
            self._collect(con)

    def cancel(self, key: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row: sqlite3.Row | None = self._row(con, key)
            assert row is not None
            if row["target"] != self.backend.target.identity:
                raise GatewayError("local_unavailable", "original local target is unavailable")
            if not row["intent"]:
                con.execute(
                    "UPDATE local_jobs SET state='cancelled',payload=NULL WHERE request_key=?",
                    (key,),
                )
            elif row["state"] not in TERMINAL and row["cancel_intent"] != 2:
                con.execute(
                    "UPDATE local_jobs SET cancel_intent=? WHERE request_key=?",
                    (1 if row["downstream_id"] is None else 2, key),
                )
            else:
                row = None
        if row and row["intent"] and row["downstream_id"] is not None:
            try:
                self._record(key, self.backend.cancel(row["downstream_id"]))
            except (OSError, ValueError, TypeError, KeyError, HTTPException, GatewayError):
                pass  # Preserve the prior state and pinned identity; query-only recovery.
        return self.status(key)
