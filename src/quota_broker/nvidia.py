"""Fixed-route NVIDIA execution. Payloads and credentials only live in this process."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Quota, Target
from .core import Broker, BrokerError, canonical, stamp, utcnow

MODEL = "google/gemma-4-31b-it"
URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MAX_PROMPT_BYTES = 32_768
CHAT_TEMPLATE_BUFFER_TOKENS = 256
INPUT_TOKEN_BOUND_PER_BYTE = 4
MAX_PROVIDER_BYTES = 262_144


class ExecutionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def doppler_resolver(token_file: str | Path, project: str, config: str) -> Callable[[str], str]:
    """Read a service token from a protected runtime credential file."""
    if not Path(token_file).is_file():
        raise ValueError("Doppler bootstrap configuration is incomplete")
    token = Path(token_file).read_text(encoding="utf-8").strip()
    return doppler_resolver_from_token(token, project, config)


def doppler_resolver_from_token(token: str, project: str, config: str) -> Callable[[str], str]:
    """Fetch one secret directly; no CLI cache, environment scan or fallback snapshot."""
    if (
        not project
        or not config
        or not re.fullmatch(r"dp\.st\.(?:[a-z0-9_-]{2,35}\.)?[A-Za-z0-9]{40,44}", token)
    ):
        raise ValueError("A config-scoped Doppler Service Token is required")
    opener = urllib.request.build_opener(NoRedirect())

    def resolve(name: str) -> str:
        if (
            not name
            or len(name) > 128
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in name)
        ):
            raise ExecutionError("secret_unavailable", "invalid secret reference")
        query = urllib.parse.urlencode({"project": project, "config": config, "name": name})
        request = urllib.request.Request(
            "https://api.doppler.com/v3/configs/config/secret?" + query,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        )
        try:
            with opener.open(request, timeout=5) as response:
                if response.status != 200:
                    raise ExecutionError("secret_unavailable", "secret service unavailable")
                raw = response.read(8193)
            if len(raw) > 8192:
                raise ExecutionError("secret_unavailable", "secret response too large")
            value = json.loads(raw)["value"]["raw"]
            if not isinstance(value, str) or not value:
                raise ValueError("empty secret")
            return value
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
            raise ExecutionError("secret_unavailable", "secret service unavailable") from exc

    return resolve


def nvidia_transport(key: str, prompt: str, max_tokens: int) -> tuple[int, dict[str, Any]]:
    body = canonical(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        raw = response.read(MAX_PROVIDER_BYTES + 1)
    if len(raw) > MAX_PROVIDER_BYTES:
        raise ExecutionError("provider_unknown", "provider response too large")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ExecutionError("provider_unknown", "invalid provider response") from exc
    if not isinstance(data, dict):
        raise ExecutionError("provider_unknown", "invalid provider response")
    return status, data


class NvidiaExecutor:
    def __init__(
        self,
        db: str | Path,
        digest_key: bytes,
        secret_resolver: Callable[[str], str],
        transport: Callable[[str, str, int], tuple[int, dict[str, Any]]] = nvidia_transport,
        doppler_project: str = "unknown",
        doppler_config: str = "unknown",
    ) -> None:
        if len(digest_key) < 32:
            raise ValueError("digest key must be at least 32 bytes and persist across restarts")
        self.db = str(db)
        self.digest_key = digest_key
        self.secret_resolver = secret_resolver
        self.transport = transport
        self.doppler_project = doppler_project
        self.doppler_config = doppler_config
        self._lock = threading.Lock()
        with sqlite3.connect(self.db) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS nvidia_profile (
                    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nvidia_executions (
                    request_key TEXT PRIMARY KEY, payload_digest TEXT NOT NULL,
                    reservation_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error_code TEXT,
                    usage_prompt_tokens INTEGER, usage_completion_tokens INTEGER
                );
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(nvidia_executions)")}
            for name in ("usage_prompt_tokens", "usage_completion_tokens"):
                if name not in columns:
                    con.execute(f"ALTER TABLE nvidia_executions ADD COLUMN {name} INTEGER")

    @staticmethod
    def validate_profile(data: dict[str, Any]) -> dict[str, Any]:
        required = {
            "secret_ref",
            "key_id",
            "key_name",
            "key_expires_kind",
            "key_expires_at",
            "scope",
            "verified_at",
            "eligibility_expires_at",
            "source",
            "enabled",
            "free_eligible",
            "billing_enabled",
            "rpm",
            "rpd",
            "input_tpm",
            "concurrency_limit",
            "max_output_tokens",
        }
        if set(data) != required:
            raise ExecutionError("invalid_request", "profile fields incomplete")
        for field in ("secret_ref", "key_id", "key_name", "scope", "source"):
            if not isinstance(data[field], str) or not 0 < len(data[field]) <= 256:
                raise ExecutionError("invalid_request", f"invalid {field}")
        if not all(c.isascii() and (c.isalnum() or c == "_") for c in data["secret_ref"]):
            raise ExecutionError("invalid_request", "invalid secret_ref")
        if data["key_expires_kind"] not in {"unknown", "never", "at"}:
            raise ExecutionError("invalid_request", "invalid key expiration kind")
        for field in ("verified_at", "eligibility_expires_at", "key_expires_at"):
            if data[field] is not None:
                try:
                    parsed = datetime.fromisoformat(data[field])
                    if parsed.tzinfo is None:
                        raise ValueError("timezone required")
                except (TypeError, ValueError) as exc:
                    raise ExecutionError("invalid_request", f"invalid {field}") from exc
        if data["key_expires_kind"] == "at" and data["key_expires_at"] is None:
            raise ExecutionError("invalid_request", "key expiration required")
        if data["key_expires_kind"] != "at" and data["key_expires_at"] is not None:
            raise ExecutionError("invalid_request", "unexpected key expiration")
        for field in ("enabled", "free_eligible", "billing_enabled"):
            if type(data[field]) is not bool:
                raise ExecutionError("invalid_request", f"invalid {field}")
        for field in ("rpm", "rpd", "input_tpm", "concurrency_limit", "max_output_tokens"):
            if type(data[field]) is not int or data[field] < 1:
                raise ExecutionError("invalid_request", f"invalid {field}")
        if data["max_output_tokens"] > 4096:
            raise ExecutionError("invalid_request", "max output exceeds model limit")
        return data

    def put_profile(self, data: dict[str, Any]) -> None:
        validated = self.validate_profile(data)
        with self._lock, sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO nvidia_profile(id,data) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (canonical(validated),),
            )

    def profile(self) -> dict[str, Any] | None:
        with sqlite3.connect(self.db) as con:
            row = con.execute("SELECT data FROM nvidia_profile WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def profile_state(self) -> str:
        profile = self.profile()
        if profile is None:
            return "待設定"
        now = utcnow()
        kind = profile["key_expires_kind"]
        if kind == "unknown":
            return "金鑰到期未知"
        if (
            kind == "at"
            and datetime.fromisoformat(profile["key_expires_at"]).astimezone(UTC) <= now
        ):
            return "金鑰已過期"
        verified = profile["verified_at"]
        expires = profile["eligibility_expires_at"]
        if not verified or not expires:
            return "免費資格待驗證"
        if datetime.fromisoformat(expires).astimezone(UTC) <= now:
            return "免費資格已過期"
        if datetime.fromisoformat(verified).astimezone(UTC) > now:
            return "驗證時間尚未生效"
        if profile["billing_enabled"]:
            return "已啟用計費，拒絕執行"
        if not profile["free_eligible"]:
            return "免費資格未確認"
        if not profile["enabled"]:
            return "已停用"
        return "有效（限本地中繼資料）"

    def _target(self, profile: dict[str, Any]) -> Target:
        now = utcnow()
        if profile["key_expires_kind"] == "unknown" or (
            profile["key_expires_kind"] == "at"
            and datetime.fromisoformat(profile["key_expires_at"]).astimezone(UTC) <= now
        ):
            raise ExecutionError("unavailable", "key expiration is unknown or elapsed")
        verified = (
            datetime.fromisoformat(profile["verified_at"]) if profile["verified_at"] else None
        )
        expires = (
            datetime.fromisoformat(profile["eligibility_expires_at"])
            if profile["eligibility_expires_at"]
            else None
        )
        return Target(
            id="nvidia:primary",
            provider="nvidia",
            model=MODEL,
            account_id="",
            enabled=profile["enabled"],
            free_eligible=profile["free_eligible"],
            billing_enabled=profile["billing_enabled"],
            verified_at=verified,
            expires_at=expires,
            quotas=(
                Quota("nvidia:primary:rpm", "requests", profile["rpm"], "rolling_minute"),
                Quota("nvidia:primary:rpd", "requests", profile["rpd"], "day"),
                Quota(
                    "nvidia:primary:input_tpm",
                    "input_tokens",
                    profile["input_tpm"],
                    "rolling_minute",
                ),
            ),
            concurrency_limit=profile["concurrency_limit"],
            max_output_tokens=profile["max_output_tokens"],
            priority=0,
            source=profile["source"],
        )

    def _status(self, request_key: str) -> dict[str, Any]:
        with sqlite3.connect(self.db) as con:
            row = con.execute(
                "SELECT reservation_id,state,error_code,created_at,updated_at,"
                "usage_prompt_tokens,usage_completion_tokens "
                "FROM nvidia_executions WHERE request_key=?",
                (request_key,),
            ).fetchone()
        if row is None:
            raise ExecutionError("not_found", "request not found")
        view = dict(
            zip(
                (
                    "reservation_id",
                    "state",
                    "error_code",
                    "created_at",
                    "updated_at",
                    "usage_prompt_tokens",
                    "usage_completion_tokens",
                ),
                row,
            )
        ) | {"request_key": request_key}
        view["usage"] = (
            {
                "prompt_tokens": view["usage_prompt_tokens"],
                "completion_tokens": view["usage_completion_tokens"],
            }
            if view["usage_prompt_tokens"] is not None
            else None
        )
        del view["usage_prompt_tokens"]
        del view["usage_completion_tokens"]
        view["accounted_input_tokens"] = None
        if view["reservation_id"]:
            with sqlite3.connect(self.db) as con:
                core_row = con.execute(
                    "SELECT state FROM reservations WHERE id=?", (view["reservation_id"],)
                ).fetchone()
                charge = con.execute(
                    "SELECT amount FROM charges WHERE reservation_id=? AND metric='input_tokens' LIMIT 1",
                    (view["reservation_id"],),
                ).fetchone()
            if core_row and core_row[0] in {"dispatched", "unknown", "completed", "failed"}:
                view["state"] = core_row[0]
            if charge:
                view["accounted_input_tokens"] = charge[0]
        return view

    def status(self, request_key: str) -> dict[str, Any]:
        if not isinstance(request_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._~-]{1,153}", request_key
        ):
            raise ExecutionError("invalid_request", "invalid request key")
        return self._status(request_key)

    def list_status(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT request_key FROM nvidia_executions ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return [self._status(row[0]) for row in rows]

    def _update(
        self,
        request_key: str,
        state: str,
        reservation_id: str | None = None,
        error_code: str | None = None,
        usage_prompt_tokens: int | None = None,
        usage_completion_tokens: int | None = None,
    ) -> None:
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE nvidia_executions SET state=?,reservation_id=COALESCE(?,reservation_id),"
                "updated_at=?,error_code=?,"
                "usage_prompt_tokens=COALESCE(?,usage_prompt_tokens),"
                "usage_completion_tokens=COALESCE(?,usage_completion_tokens) WHERE request_key=?",
                (
                    state,
                    reservation_id,
                    stamp(utcnow()),
                    error_code,
                    usage_prompt_tokens,
                    usage_completion_tokens,
                    request_key,
                ),
            )

    def execute(self, data: dict[str, Any]) -> dict[str, Any]:
        if set(data) != {"request_key", "prompt", "max_output_tokens"}:
            raise ExecutionError(
                "invalid_request", "expected request_key, prompt, max_output_tokens"
            )
        key, prompt, output = data["request_key"], data["prompt"], data["max_output_tokens"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{1,153}", key)
            or not isinstance(prompt, str)
            or not prompt
        ):
            raise ExecutionError("invalid_request", "invalid key or prompt")
        prompt_bytes = prompt.encode("utf-8")
        if (
            len(prompt_bytes) > MAX_PROMPT_BYTES
            or type(output) is not int
            or not 1 <= output <= 4096
        ):
            raise ExecutionError("invalid_request", "request exceeds bounds")
        payload_digest = hmac.new(
            self.digest_key, canonical(data).encode(), hashlib.sha256
        ).hexdigest()
        with self._lock, sqlite3.connect(self.db, timeout=15) as con:
            now = stamp(utcnow())
            claim = con.execute(
                "INSERT INTO nvidia_executions VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(request_key) DO NOTHING",
                (key, payload_digest, None, "preparing", now, now, None, None, None),
            )
            if claim.rowcount == 0:
                row = con.execute(
                    "SELECT payload_digest FROM nvidia_executions WHERE request_key=?", (key,)
                ).fetchone()
                assert row is not None
                if not secrets.compare_digest(row[0], payload_digest):
                    raise ExecutionError("conflict", "request key used with a different payload")
                return self._status(key)
        with self._lock:
            profile = self.profile()
            if profile is None:
                self._update(key, "rejected", error_code="unavailable")
                raise ExecutionError("unavailable", "NVIDIA profile missing")
            try:
                target = self._target(profile)
                if not target.available(utcnow()) or output > target.max_output_tokens:
                    raise ExecutionError("unavailable", "verified free capacity unavailable")
                secret = self.secret_resolver(profile["secret_ref"])
                if not secret:
                    raise ExecutionError("secret_unavailable", "secret unavailable")
                broker = Broker(self.db, (target,))
                # Admission estimate includes message framing and a wide tokenizer margin.
                # The provider's reported usage remains authoritative for reconciliation.
                input_bound = (
                    len(prompt_bytes) * INPUT_TOKEN_BOUND_PER_BYTE + CHAT_TEMPLATE_BUFFER_TOKENS
                )
                reservation = broker.reserve(
                    {
                        "request_key": "nvidia:" + key,
                        "capability": "text_generation",
                        "model": MODEL,
                        "input_token_bound": input_bound,
                        "max_output_tokens": output,
                    }
                )
                rid = reservation["reservation_id"]
                self._update(key, "reserved", rid)
                broker.dispatch(rid)
                self._update(key, "dispatched")
            except (BrokerError, ExecutionError) as exc:
                self._update(key, "rejected", error_code=exc.code)
                raise ExecutionError(exc.code, str(exc)) from exc
        status: int | None = None
        try:
            status, response = self.transport(secret, prompt, output)
            usage = response.get("usage")
            if (
                status == 200
                and isinstance(usage, dict)
                and type(usage.get("prompt_tokens")) is int
                and usage["prompt_tokens"] >= 0
                and type(usage.get("completion_tokens")) is int
                and usage["completion_tokens"] >= 0
            ):
                choices = response.get("choices")
                content = (
                    choices[0]["message"]["content"]
                    if isinstance(choices, list) and choices
                    else None
                )
                if not isinstance(content, str):
                    raise ExecutionError("provider_unknown", "missing text response")
                self._update(
                    key,
                    "dispatched",
                    usage_prompt_tokens=usage["prompt_tokens"],
                    usage_completion_tokens=usage["completion_tokens"],
                )
                broker.report(
                    {
                        "reservation_id": rid,
                        "report_key": "nvidia:" + key,
                        "state": "completed",
                        "usage": {"requests": 1, "input_tokens": usage["prompt_tokens"]},
                    }
                )
                self._update(key, "completed")
                return {
                    "request_key": key,
                    "state": "completed",
                    "model": MODEL,
                    "content": content,
                    "usage": {
                        "input_tokens": usage["prompt_tokens"],
                        "output_tokens": usage["completion_tokens"],
                    },
                }
            if (
                status >= 400
                and isinstance(usage, dict)
                and type(usage.get("prompt_tokens")) is int
                and usage["prompt_tokens"] >= 0
            ):
                self._update(key, "dispatched", usage_prompt_tokens=usage["prompt_tokens"])
                broker.report(
                    {
                        "reservation_id": rid,
                        "report_key": "nvidia:" + key,
                        "state": "failed",
                        "usage": {"requests": 1, "input_tokens": usage["prompt_tokens"]},
                        "error_status": status,
                    }
                )
                self._update(
                    key,
                    "failed",
                    error_code="provider_error",
                    usage_prompt_tokens=usage["prompt_tokens"],
                )
                raise ExecutionError("provider_error", "provider returned an error")
            raise ExecutionError("provider_unknown", "provider outcome or usage unknown")
        except Exception as exc:
            code = exc.code if isinstance(exc, ExecutionError) else "provider_unknown"
            if code != "provider_error":
                try:
                    unknown_report: dict[str, Any] = {
                        "reservation_id": rid,
                        "report_key": "nvidia:" + key,
                        "state": "unknown",
                    }
                    if status == 429:
                        unknown_report["error_status"] = 429
                    broker.report(unknown_report)
                except BrokerError:
                    pass
                self._update(key, "unknown", error_code="provider_unknown")
            raise ExecutionError(code, "provider request failed or outcome unknown") from exc
