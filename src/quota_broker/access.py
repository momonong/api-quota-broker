"""Server-owned principals and durable project ownership, without a second ledger."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .core import BrokerError, canonical, stamp
from .execution_policy import POLICIES, execution_policy
from .gateway import Gateway, GatewayError

POLICY_BYTES = 65536
SCOPES = frozenset({"read", "plan", "execute", "admin"})
KEY = re.compile(r"[A-Za-z0-9._~-]{1,120}")


@dataclass(frozen=True)
class Principal:
    token_sha256: str = field(repr=False)
    project_id: str | None
    scopes: frozenset[str]
    max_input_bytes: int
    max_output_tokens: int
    max_inflight: int
    max_deadline_seconds: int
    expires_at: datetime | None = None
    allowed_execution: frozenset[str] = frozenset({"cloud_only"})
    default_execution: str = "cloud_only"
    encryption_required: bool = False


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid access policy")
        result[key] = value
    return result


class AccessPolicyFile:
    """Digest-only JSON, regular owner-private file; reloaded on every authentication.

    Deploy via an owner-controlled directory and atomic rename. The descriptor's
    inode is checked before reading; symlinks and group/other access fail closed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()

    def read(self) -> tuple[Principal, ...]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= POLICY_BYTES
                ):
                    raise ValueError
                raw = stream.read(POLICY_BYTES + 1)
            if len(raw) > POLICY_BYTES:
                raise ValueError
            data = json.loads(raw, object_pairs_hook=_unique)
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "principals"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or not isinstance(data["principals"], list)
                or len(data["principals"]) > 128
            ):
                raise ValueError
            result = []
            seen = set()
            bounds = {
                "max_input_bytes": 32 * 1024 * 1024,
                "max_output_tokens": 65536,
                "max_inflight": 10000,
                "max_deadline_seconds": 86400,
            }
            for item in data["principals"]:
                required_fields = {"token_sha256", "project_id", "scopes", "limits"}
                policy_fields = {"allowed_execution", "default_execution", "encryption_required"}
                if (
                    not isinstance(item, dict)
                    or not required_fields <= set(item)
                    or set(item) - required_fields - {"expires_at"} - policy_fields
                ):
                    raise ValueError
                digest, project, scopes, limits = (
                    item[k] for k in ("token_sha256", "project_id", "scopes", "limits")
                )
                if (
                    not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or digest in seen
                    or (
                        project is not None
                        and (
                            not isinstance(project, str)
                            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", project)
                        )
                    )
                    or not isinstance(scopes, list)
                    or not scopes
                    or any(not isinstance(s, str) or s not in SCOPES for s in scopes)
                    or len(set(scopes)) != len(scopes)
                    or ("execute" in scopes and project is None)
                    or ("admin" in scopes and project is not None)
                    or not isinstance(limits, dict)
                    or set(limits) != set(bounds)
                    or any(
                        type(limits[k]) is not int or not 1 <= limits[k] <= v
                        for k, v in bounds.items()
                    )
                ):
                    raise ValueError
                expiry = item.get("expires_at")
                if expiry is not None:
                    if not isinstance(expiry, str):
                        raise ValueError
                    expiry = datetime.fromisoformat(expiry)
                    if expiry.tzinfo is None:
                        raise ValueError
                local_policy: dict[str, Any] = {}
                if policy_fields & set(item):
                    if not policy_fields <= set(item):
                        raise ValueError
                    allowed = item["allowed_execution"]
                    default = execution_policy(item["default_execution"])
                    encrypted = item["encryption_required"]
                    if (
                        not isinstance(allowed, list)
                        or not allowed
                        or any(not isinstance(p, str) or p not in POLICIES for p in allowed)
                        or len(set(allowed)) != len(allowed)
                        or default not in allowed
                        or type(encrypted) is not bool
                        or ("local_only" in allowed and not encrypted)
                    ):
                        raise ValueError
                    local_policy = {
                        "allowed_execution": frozenset(allowed),
                        "default_execution": default,
                        "encryption_required": encrypted,
                    }
                seen.add(digest)
                result.append(
                    Principal(
                        digest,
                        project,
                        frozenset(scopes),
                        **limits,
                        expires_at=expiry,
                        **local_policy,
                    )
                )
            # A project's credentials cannot bypass each other's resource limits.
            by_project: dict[str, tuple[Any, ...]] = {}
            for principal in result:
                if principal.project_id is not None:
                    values = tuple(getattr(principal, k) for k in bounds) + (
                        principal.allowed_execution,
                        principal.default_execution,
                        principal.encryption_required,
                    )
                    if (
                        principal.project_id in by_project
                        and by_project[principal.project_id] != values
                    ):
                        raise ValueError
                    by_project[principal.project_id] = values
            return tuple(result)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise ValueError("access policy unavailable") from None

    def authenticate_digest(self, digest: str) -> Principal | None:
        for principal in self.read():
            if hmac.compare_digest(digest, principal.token_sha256) and (
                principal.expires_at is None or datetime.now(UTC) < principal.expires_at
            ):
                return principal
        return None

    def authenticate(self, token: str) -> Principal | None:
        if not 32 <= len(token) <= 4096 or not token.isascii():
            return None
        return self.authenticate_digest(hashlib.sha256(token.encode()).hexdigest())


def metadata(value: Any) -> Any:
    """Dashboard global metadata has no object handles or request/response content."""
    if isinstance(value, list):
        return [metadata(item) for item in value]
    if isinstance(value, dict):
        blocked = {
            "request_key",
            "reservation_id",
            "provider_request_id",
            "execution_key",
            "answer",
            "result",
            "input",
            "payload",
            "required_names",
            "missing_names",
        }
        return {
            key: None if key == "next_before" else metadata(item)
            for key, item in value.items()
            if key not in blocked
        }
    return value


def task_metadata(value: Any) -> dict[str, Any]:
    fields = {"provider", "model", "state", "created_at", "completed_at", "latency_ms"}
    return {
        "tasks": [{k: v for k, v in item.items() if k in fields} for item in value["tasks"]],
        "next_before": None,
    }


class ProjectAccess:
    """Maps stable external keys to opaque internal keys before any dispatch.

    Ownership is additive in the Gateway DB. It neither copies nor settles usage.
    Missing state remains an inflight claim, allowing only the same identity/key
    to reconcile after a crash; unknown work never frees capacity automatically.
    """

    def __init__(self, gateway: Gateway, policy: AccessPolicyFile) -> None:
        self.gateway, self.policy = gateway, policy
        with closing(sqlite3.connect(gateway.db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""CREATE TABLE IF NOT EXISTS gateway_project_objects (
                project_id TEXT NOT NULL, kind TEXT NOT NULL, external_key TEXT NOT NULL,
                internal_key TEXT NOT NULL UNIQUE, token_sha256 TEXT NOT NULL,
                input_bytes INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                deadline TEXT NOT NULL,
                PRIMARY KEY(project_id,kind,external_key))""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(gateway_project_objects)")}
            if "payload_hmac" not in columns:
                # Old claims cannot prove which payload their bounds described.
                # Keep identity/deadline intact and refuse dispatch until reconciled.
                con.execute("ALTER TABLE gateway_project_objects ADD COLUMN payload_hmac TEXT")
            if "execution_policy" not in columns:
                con.execute("ALTER TABLE gateway_project_objects ADD COLUMN execution_policy TEXT")
            if "content_encrypted" not in columns:
                con.execute(
                    "ALTER TABLE gateway_project_objects ADD COLUMN content_encrypted INTEGER NOT NULL DEFAULT 0"
                )
        # One shared Gateway can have two listeners. Never chain wrappers twice.
        installed = getattr(gateway, "_project_access", None)
        if installed is None:
            previous = gateway.broker.dispatch_policy

            def permit(con: sqlite3.Connection, target: Any, reservation_id: str) -> None:
                if previous is not None:
                    previous(con, target, reservation_id)
                self._permit(con, target, reservation_id)

            gateway.broker.dispatch_policy = permit
            gateway._project_access = self
        elif installed.policy.path != policy.path:
            raise ValueError("Gateway listeners must share access policy")

    @staticmethod
    def input_bytes(data: dict[str, Any]) -> int:
        """Canonical provider-bound content, including tools/schema/continuation options."""
        return len(canonical({"input": data["input"], "options": data.get("options", {})}).encode())

    def _payload_hmac(self, data: dict[str, Any]) -> str:
        return hmac.new(
            self.gateway.digest_key, canonical(data).encode(), hashlib.sha256
        ).hexdigest()

    def bounds(self, principal: Principal, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GatewayError("invalid_request", "invalid task fields")
        try:
            effective = execution_policy(body.get("execution_policy", principal.default_execution))
        except ValueError:
            raise GatewayError("invalid_request", "invalid execution policy") from None
        if effective not in principal.allowed_execution:
            raise GatewayError("forbidden", "execution policy is not authorized")
        data = self.gateway.validate_task({**body, "execution_policy": effective})
        size = self.input_bytes(data)
        if (
            size > principal.max_input_bytes
            or data["max_output_tokens"] > principal.max_output_tokens
        ):
            raise GatewayError("forbidden", "project task bounds exceeded")
        now = self.gateway.clock()
        limit = now + timedelta(seconds=principal.max_deadline_seconds)
        if data.get("deadline") is not None:
            deadline = datetime.fromisoformat(data["deadline"])
            if not now < deadline <= limit:
                raise GatewayError("forbidden", "project deadline exceeded")
        # This optional preflight performs provider I/O before Broker dispatch;
        # scoped execution currently requires a known cost without countTokens.
        if self.gateway.google_pdf_count_tokens:
            from .token_preflight import needs_count

            if needs_count(data):
                raise GatewayError("forbidden", "scoped token preflight unavailable")
        return data

    @staticmethod
    def _public_metadata(value: Any) -> Any:
        """Omit operator evidence locations and account/bucket identifiers."""
        if isinstance(value, list):
            return [ProjectAccess._public_metadata(item) for item in value]
        if isinstance(value, dict):
            return {
                key: ProjectAccess._public_metadata(item)
                for key, item in value.items()
                if key not in {"source", "scope", "bucket"}
            }
        return value

    def _capacity_reasons(
        self,
        target: Any,
        observations: list[dict[str, Any]],
        costs: dict[tuple[str, str], int] | None,
    ) -> list[str]:
        """Same evidence gate for project catalog, dry-run and actual dispatch.

        A catalog has no task cost: it only checks for positive current capacity.
        Absolute evidence validity works with every supported ledger timezone.
        Expired optional dimensions are historical data, not new requirements.
        """
        required = set(target.required_capacity) or {(q.metric, q.window) for q in target.quotas}
        required.update((o["metric"], o["window"]) for o in observations if o["current"])
        reasons = []
        if not target.available(self.gateway.clock()) or not required:
            reasons.append("project_capacity:unavailable")
        for metric, window in sorted(required):
            matches = [o for o in observations if (o["metric"], o["window"]) == (metric, window)]
            cost = 1 if costs is None else costs.get((metric, window))
            if (
                not matches
                or cost is None
                or any(
                    not o["current"]
                    or o["provenance"] not in {"official", "observed"}
                    or o["confidence"]
                    not in {"provider_reported", "administrator_verified", "configured_evidence"}
                    or o["effective_remaining"] is None
                    or o["effective_remaining"] < cost
                    for o in matches
                )
            ):
                reasons.append(f"project_capacity:{metric}:{window}")
        return reasons

    def catalog(self, principal: Principal) -> list[dict[str, Any]]:
        fields = {
            "target_id",
            "provider",
            "model",
            "capabilities",
            "variants",
            "features",
            "input_modalities",
            "output_modalities",
            "context_tokens",
            "max_output_tokens",
            "free_kind",
            "use_restrictions",
            "enabled",
            "available",
            "request_limits",
            "quota_basis",
            "quotas",
            "local_safety_caps",
            "provider_quota_facts",
            "required_capacity",
            "capacity",
            "quota_observations",
            "local_ledger_usage",
        }
        targets = {t.id: t for t in self.gateway.targets}
        result = []
        for item in self.gateway.catalog():
            row = self._public_metadata(
                {key: value for key, value in item.items() if key in fields}
            )
            reasons = self._capacity_reasons(
                targets[item["target_id"]], item["quota_observations"], None
            )
            if "cloud_only" not in principal.allowed_execution:
                reasons.append("execution_policy:cloud_target")
            row.update(execution_policy="cloud_only", execution_location="cloud")
            row["project_reasons"] = reasons
            row["available"] = row["available"] and not reasons
            row["max_output_tokens"] = min(row["max_output_tokens"], principal.max_output_tokens)
            result.append(row)
        return result

    def explain(self, principal: Principal, body: dict[str, Any]) -> dict[str, Any]:
        data = self.bounds(principal, body)
        plan = self.gateway.explain(data)
        targets = {t.id: t for t in self.gateway.targets}
        for row in plan["candidates"]:
            bound, output = plan["estimated_input_tokens"] or 0, data["max_output_tokens"]
            resources = row["resource_cost_bounds"]
            if resources is None:
                resources = {
                    "input_tokens": bound,
                    "output_tokens": output,
                    "total_tokens": bound + output,
                }
                if row["effective_neuron_bound"] is not None:
                    resources["neurons"] = row["effective_neuron_bound"]
            costs = {}
            for o in row["quota_observations"]:
                metric = o["metric"]
                cost = (
                    1
                    if metric == "requests"
                    else resources.get("total_tokens" if metric == "tokens" else metric)
                )
                if cost is not None:
                    costs[(metric, o["window"])] = cost
            reasons = self._capacity_reasons(
                targets[row["target_id"]], row["quota_observations"], costs
            )
            if reasons:
                row["reasons"].extend(reasons)
                row.update(eligible=False, temporary=False, next_retry_at=None)
        selected = next((row["target_id"] for row in plan["candidates"] if row["eligible"]), None)
        temporary = any(row["temporary"] for row in plan["candidates"])
        retries = [row["next_retry_at"] for row in plan["candidates"] if row["next_retry_at"]]
        plan.update(
            selected_target_id=selected,
            temporary=selected is None and temporary,
            permanent_rejection=selected is None and not temporary,
            next_retry_at=min(retries) if retries else None,
        )
        return self._public_metadata(plan)

    def claim(
        self, principal: Principal, kind: str, body: dict[str, Any], *, encrypted: bool = False
    ) -> tuple[str, dict[str, Any]]:
        if principal.encryption_required and not encrypted:
            raise GatewayError("encryption_required", "encrypted transport required")
        data = self.bounds(principal, body)
        key = data["request_key"]
        if kind == "jobs":
            data.setdefault("priority", 0)
            data.setdefault("wait_policy", "wait")
            data.setdefault("max_attempts", 32)
        with (
            closing(sqlite3.connect(self.gateway.db, timeout=15, isolation_level=None)) as con,
            con,
        ):
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute(
                "SELECT internal_key,deadline,payload_hmac FROM gateway_project_objects "
                "WHERE project_id=? AND kind=? AND external_key=?",
                (principal.project_id, kind, key),
            ).fetchone()
            if prior:
                internal, deadline, fingerprint = prior
                if data.get("deadline") is not None and data["deadline"] != deadline:
                    raise GatewayError("conflict", "project key already has another deadline")
                data.update(request_key=internal, deadline=deadline)
                if not isinstance(fingerprint, str) or not hmac.compare_digest(
                    fingerprint, self._payload_hmac(data)
                ):
                    raise GatewayError(
                        "conflict", "project key already bound or requires reconciliation"
                    )
            else:
                rows = con.execute(
                    "SELECT kind,internal_key,execution_policy FROM gateway_project_objects WHERE project_id=?",
                    (principal.project_id,),
                ).fetchall()
                active = 0
                terminal = {
                    "completed",
                    "failed",
                    "cancelled",
                    "expired",
                    "rejected",
                    "quota_rejected",
                    "quota_exhausted",
                    "pre_send_failed",
                }
                tables = {
                    r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for object_kind, object_key, object_policy in rows:
                    table = (
                        "local_jobs"
                        if object_policy == "local_only"
                        else "queue_jobs"
                        if object_kind == "jobs"
                        else "gateway_tasks"
                    )
                    row = (
                        con.execute(
                            f"SELECT state,{('result_state' if object_policy == 'local_only' else 'NULL')} FROM {table} WHERE request_key=?",
                            (object_key,),
                        ).fetchone()
                        if table in tables
                        else None
                    )
                    if (
                        row is None
                        or row[1] in {"pending", "storage_failed"}
                        or (
                            row[0] not in terminal
                            and not (
                                row[0] == "unknown"
                                and object_policy != "local_only"
                                and self.gateway.broker.object_unknown_slots_acknowledged(
                                    con, object_kind, object_key
                                )
                            )
                        )
                    ):
                        active += 1
                if active >= principal.max_inflight:
                    raise GatewayError("project_limit", "project inflight limit reached")
                if data.get("execution_policy") == "local_only":
                    local_rows = con.execute(
                        "SELECT project_id,internal_key FROM gateway_project_objects WHERE execution_policy='local_only'"
                    ).fetchall()
                    total = project_total = 0
                    for owner, owned in local_rows:
                        state = (
                            con.execute(
                                "SELECT state,result_state FROM local_jobs WHERE request_key=?",
                                (owned,),
                            ).fetchone()
                            if "local_jobs" in tables
                            else None
                        )
                        if (
                            state is None
                            or state[0] not in terminal
                            or state[1] in {"pending", "storage_failed"}
                        ):
                            total += 1
                            project_total += owner == principal.project_id
                    if total >= 8 or project_total >= 2:
                        raise GatewayError("project_limit", "local inflight limit reached")
                internal = "qp-" + uuid.uuid4().hex
                deadline = data.get("deadline") or stamp(
                    self.gateway.clock() + timedelta(seconds=principal.max_deadline_seconds)
                )
                data.update(request_key=internal, deadline=deadline)
                con.execute(
                    "INSERT INTO gateway_project_objects "
                    "(project_id,kind,external_key,internal_key,token_sha256,input_bytes,output_tokens,deadline,payload_hmac,execution_policy,content_encrypted) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        principal.project_id,
                        kind,
                        key,
                        internal,
                        principal.token_sha256,
                        self.input_bytes(data),
                        data["max_output_tokens"],
                        deadline,
                        self._payload_hmac(data),
                        data.get("execution_policy", "cloud_only"),
                        int(encrypted),
                    ),
                )
            # The persisted deadline is stable across retry, preserving payload HMAC.
            data.update(request_key=internal, deadline=deadline)
            return internal, data

    def lookup(self, principal: Principal, kind: str, key: str) -> str:
        if not KEY.fullmatch(key):
            raise GatewayError("not_found", "task not found")
        with closing(sqlite3.connect(self.gateway.db)) as con:
            row = con.execute(
                "SELECT internal_key FROM gateway_project_objects WHERE project_id=? AND kind=? AND external_key=?",
                (principal.project_id, kind, key),
            ).fetchone()
        if row is None:
            raise GatewayError("not_found", "task not found")
        return row[0]

    def execution(self, internal: str) -> str:
        with closing(sqlite3.connect(self.gateway.db)) as con:
            row = con.execute(
                "SELECT execution_policy FROM gateway_project_objects WHERE internal_key=?",
                (internal,),
            ).fetchone()
        if row is None:
            raise GatewayError("not_found", "project object missing")
        return row[0] or "cloud_only"

    @staticmethod
    def external(value: dict[str, Any], internal: str, key: str) -> dict[str, Any]:
        """Project only known Broker envelopes; answer/result remain opaque business content."""
        hidden = {"execution_key", "reservation_id", "provider_request_id"}
        result = {name: item for name, item in value.items() if name not in hidden}
        if "request_key" in result:
            result["request_key"] = key
        if isinstance(result.get("attempts"), list):
            result["attempts"] = [
                {
                    name: key if name == "request_key" else item
                    for name, item in attempt.items()
                    if name not in hidden
                }
                for attempt in result["attempts"]
            ]
        if "result" not in result and isinstance(result.get("answer"), str):
            result["result"] = {"text": result["answer"]}
        return result

    def recent(
        self, principal: Principal, kind: str, fetch: Any, query: dict[str, list[str]]
    ) -> dict[str, Any]:
        allowed = {"limit", "before", "provider", "model", "state"} if kind == "tasks" else set()
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise GatewayError("invalid_request", "invalid project task filters")
        raw_limit = query.get("limit", ["20" if kind == "tasks" else "100"])[0]
        if not raw_limit.isascii() or not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 100:
            raise GatewayError("invalid_request", "invalid task limit")
        before = query.get("before", [None])[0]
        with closing(sqlite3.connect(self.gateway.db)) as con:
            rows = con.execute(
                "SELECT internal_key,external_key FROM gateway_project_objects WHERE project_id=? AND kind=? ORDER BY rowid DESC",
                (principal.project_id, kind),
            ).fetchall()
        result = []
        passed = before is None
        if before is not None and before not in {r[1] for r in rows}:
            raise GatewayError("not_found", "task cursor not found")
        for internal, key in rows:
            if not passed:
                passed = key == before
                continue
            try:
                item = fetch(internal)
            except GatewayError as error:
                if error.code == "not_found":
                    continue
                raise
            if any(
                item.get(name) != values[0]
                for name, values in query.items()
                if name in {"provider", "model", "state"}
            ):
                continue
            result.append(self.external(item, internal, key))
            if len(result) > int(raw_limit):
                break
        return {
            "tasks": result[: int(raw_limit)],
            "next_before": result[int(raw_limit) - 1]["request_key"]
            if len(result) > int(raw_limit)
            else None,
        }

    def _permit(self, con: sqlite3.Connection, target: Any, reservation_id: str) -> None:
        task = con.execute(
            "SELECT request_key FROM gateway_attempts WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if task is None:
            reservation = con.execute(
                "SELECT request_key FROM reservations WHERE id=?", (reservation_id,)
            ).fetchone()
            if reservation is not None and reservation[0].startswith("gw:"):
                raise BrokerError("unavailable", "Gateway attempt ownership missing")
            return
        key = task[0]
        owner_query = (
            "SELECT project_id,kind,external_key,internal_key,token_sha256,input_bytes,"
            "output_tokens,deadline,payload_hmac,execution_policy,content_encrypted "
            "FROM gateway_project_objects WHERE internal_key=?"
        )
        owner = con.execute(owner_query, (key,)).fetchone()
        if owner is None:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "queue_attempts" in tables:
                job = con.execute(
                    "SELECT request_key FROM queue_attempts WHERE execution_key=?", (key,)
                ).fetchone()
                if job is not None:
                    owner = con.execute(owner_query, (job[0],)).fetchone()
                    if owner is None and job[0].startswith("qp-"):
                        raise BrokerError("unavailable", "project ownership missing")
            if owner is None:
                if key.startswith("qp-"):
                    raise BrokerError("unavailable", "project ownership missing")
                return
        try:
            principal = self.policy.authenticate_digest(owner[4])
        except ValueError:
            principal = None
        if (
            owner[8] is None
            or principal is None
            or principal.project_id != owner[0]
            or "execute" not in principal.scopes
            or (owner[9] or "cloud_only") not in principal.allowed_execution
            or (owner[9] or "cloud_only") != "cloud_only"
            or (principal.encryption_required and not owner[10])
            or owner[5] > principal.max_input_bytes
            or owner[6] > principal.max_output_tokens
            or stamp(self.gateway.clock()) >= owner[7]
        ):
            raise BrokerError("unavailable", "project execution no longer authorized")
        observations = self.gateway._quota_observations(
            con, target, self.gateway.clock(), reservation_id
        )
        costs = {
            (row[0], row[1]): row[2]
            for row in con.execute(
                "SELECT metric,window,amount FROM observed_holds WHERE reservation_id=?",
                (reservation_id,),
            )
        }
        if self._capacity_reasons(target, observations, costs):
            raise BrokerError("unavailable", "current known free quota required")
