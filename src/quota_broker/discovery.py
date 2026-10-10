"""Durable normalized discovery evidence, separate from account/live attestations.

Public catalogs describe models, not current account access, free eligibility or
successful execution. Queries never fetch a provider or inspect execution logs.
"""

import base64
import hashlib
import importlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Target
from .core import canonical, stamp, utcnow
from .registry import Registry

MODEL_FIELDS = (
    "model",
    "capability",
    "hosting",
    "endpoint",
    "protocol",
    "free_eligibility",
    "free_source",
    "status",
    "context_tokens",
    "max_output_tokens",
    "features",
)
ATTEST_FIELDS = (
    "provider",
    "model",
    "capability",
    "account_availability",
    "account_scope",
    "account_checked_at",
    "account_valid_until",
    "live_result",
    "live_checked_at",
    "receipt_id",
)


class DiscoveryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_snapshot(raw: dict[str, Any], now: datetime) -> dict[str, Any]:
    policy = importlib.import_module("quota_broker.discovery_sources")
    return dict(policy.validate_snapshot(raw, now))


def _validate_attestation(raw: dict[str, Any], now: datetime) -> dict[str, Any]:
    policy = importlib.import_module("quota_broker.discovery_sources")
    return dict(policy.validate_attestation(raw, now))


def _identity(value: dict[str, Any], provider: str | None = None) -> tuple[str, str, str]:
    return (provider or value["provider"], value["model"], value["capability"])


def _reference(key: tuple[str, str, str]) -> dict[str, str]:
    return dict(zip(("provider", "model", "capability"), key, strict=True))


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DiscoveryError("invalid_request", "discovery evidence requires an aware timestamp")
    return parsed


class DiscoveryStore:
    def __init__(
        self,
        db: str | Path,
        registry: Registry,
        targets: tuple[Target, ...] = (),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = str(db)
        self.registry = registry
        self.targets = targets
        self.clock = clock
        with self._tx() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS discovery_snapshots (
                    snapshot_id TEXT PRIMARY KEY, provider TEXT NOT NULL, source TEXT NOT NULL,
                    checked_at TEXT NOT NULL, complete INTEGER NOT NULL, applied INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL, diff_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_sources (
                    provider TEXT NOT NULL, source TEXT NOT NULL, checked_at TEXT NOT NULL,
                    complete INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
                    last_complete_at TEXT, PRIMARY KEY(provider,source)
                );
                CREATE TABLE IF NOT EXISTS discovery_models (
                    provider TEXT NOT NULL, source TEXT NOT NULL, model TEXT NOT NULL,
                    capability TEXT NOT NULL, checked_at TEXT NOT NULL,
                    listing_checked_at TEXT NOT NULL, listing_status TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    PRIMARY KEY(provider,source,model,capability)
                );
                CREATE TABLE IF NOT EXISTS discovery_attestations (
                    attestation_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    capability TEXT NOT NULL, metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_accounts (
                    provider TEXT NOT NULL, model TEXT NOT NULL, capability TEXT NOT NULL,
                    account_scope TEXT NOT NULL, account_checked_at TEXT NOT NULL,
                    account_valid_until TEXT NOT NULL, account_availability TEXT NOT NULL,
                    live_result TEXT NOT NULL, live_checked_at TEXT, receipt_id TEXT,
                    PRIMARY KEY(provider,model,capability,account_scope)
                );
                CREATE INDEX IF NOT EXISTS discovery_model_identity
                    ON discovery_models(provider,model,capability);
            """)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            yield con

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db, timeout=15)) as con, con:
            con.row_factory = sqlite3.Row
            # Identity-only temporary seed table does not modify the durable DB.
            con.execute(
                "CREATE TEMP TABLE registry_identities(provider TEXT,model TEXT,capability TEXT,PRIMARY KEY(provider,model,capability))"
            )
            con.executemany(
                "INSERT INTO registry_identities VALUES(?,?,?)",
                (
                    (spec.provider, spec.model, capability)
                    for spec in self.registry.models.values()
                    for capability in (spec.capabilities or (spec.capability,))
                ),
            )
            con.execute("PRAGMA query_only=ON")
            yield con

    def refresh(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        checked = _validate_snapshot(snapshot, self.clock())
        # Persist only the public normalized schema, even if a validator later
        # adds transport diagnostics or other private implementation fields.
        models = [{field: item[field] for field in MODEL_FIELDS} for item in checked["models"]]
        for item in models:
            item["features"] = sorted(set(item["features"]))
        models.sort(key=lambda item: (item["model"], item["capability"]))
        normalized = {
            "schema_version": 1,
            "provider": checked["provider"],
            "source": checked["source"],
            "checked_at": stamp(_instant(checked["checked_at"])),
            "complete": checked["complete"],
            "models": models,
        }
        serialized = canonical(normalized)
        snapshot_id = hashlib.sha256(serialized.encode()).hexdigest()
        provider, source, checked_at = (
            normalized[name] for name in ("provider", "source", "checked_at")
        )
        diff: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "provider": provider,
            "source": source,
            "checked_at": checked_at,
            "complete": normalized["complete"],
            "applied": True,
            "idempotent": False,
            "stale": False,
            "added": [],
            "changed": [],
            "no_longer_listed": [],
            "explicitly_retired": [],
        }
        with self._tx() as con:
            prior = con.execute(
                "SELECT diff_json FROM discovery_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if prior:
                return {**json.loads(prior[0]), "applied": False, "idempotent": True}
            latest = con.execute(
                "SELECT * FROM discovery_sources WHERE provider=? AND source=?", (provider, source)
            ).fetchone()
            if latest and checked_at <= latest["checked_at"]:
                diff.update(applied=False, stale=True)
            else:
                previous = {
                    (row["model"], row["capability"]): row
                    for row in con.execute(
                        "SELECT * FROM discovery_models WHERE provider=? AND source=?",
                        (provider, source),
                    )
                }
                seen = set()
                for item in models:
                    identity = (item["model"], item["capability"])
                    seen.add(identity)
                    old = previous.get(identity)
                    reference = _reference((provider, *identity))
                    metadata = canonical(item)
                    if old is None:
                        diff["added"].append(reference)
                    elif (
                        old["metadata_json"] != metadata or old["listing_status"] != item["status"]
                    ):
                        diff["changed"].append(reference)
                    if item["status"] == "retired" and (
                        old is None or old["listing_status"] != "retired"
                    ):
                        diff["explicitly_retired"].append(reference)
                    con.execute(
                        "INSERT INTO discovery_models VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(provider,source,model,capability) DO UPDATE SET "
                        "checked_at=excluded.checked_at,listing_checked_at=excluded.listing_checked_at,"
                        "listing_status=excluded.listing_status,snapshot_id=excluded.snapshot_id,metadata_json=excluded.metadata_json",
                        (
                            provider,
                            source,
                            *identity,
                            checked_at,
                            checked_at,
                            item["status"],
                            snapshot_id,
                            metadata,
                        ),
                    )
                if normalized["complete"]:
                    for identity, old in previous.items():
                        if identity not in seen and old["listing_status"] != "not_listed":
                            diff["no_longer_listed"].append(_reference((provider, *identity)))
                            con.execute(
                                "UPDATE discovery_models SET listing_status='not_listed',listing_checked_at=?,snapshot_id=? "
                                "WHERE provider=? AND source=? AND model=? AND capability=?",
                                (checked_at, snapshot_id, provider, source, *identity),
                            )
                con.execute(
                    "INSERT INTO discovery_sources VALUES(?,?,?,?,?,?) ON CONFLICT(provider,source) DO UPDATE SET "
                    "checked_at=excluded.checked_at,complete=excluded.complete,snapshot_id=excluded.snapshot_id,"
                    "last_complete_at=coalesce(excluded.last_complete_at,discovery_sources.last_complete_at)",
                    (
                        provider,
                        source,
                        checked_at,
                        int(normalized["complete"]),
                        snapshot_id,
                        checked_at if normalized["complete"] else None,
                    ),
                )
            con.execute(
                "INSERT INTO discovery_snapshots VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    provider,
                    source,
                    checked_at,
                    int(normalized["complete"]),
                    int(diff["applied"]),
                    serialized,
                    canonical(diff),
                ),
            )
        return diff

    def attest(self, raw: dict[str, Any]) -> dict[str, Any]:
        checked = _validate_attestation(raw, self.clock())
        data = {field: checked[field] for field in ATTEST_FIELDS}
        for field in ("account_checked_at", "account_valid_until", "live_checked_at"):
            if data[field] is not None:
                data[field] = stamp(_instant(data[field]))
        identity = _identity(data)
        serialized = canonical(data)
        attestation_id = hashlib.sha256(serialized.encode()).hexdigest()
        with self._tx() as con:
            prior = con.execute(
                "SELECT 1 FROM discovery_attestations WHERE attestation_id=?", (attestation_id,)
            ).fetchone()
            if not prior:
                con.execute(
                    "INSERT INTO discovery_attestations VALUES(?,?,?,?,?)",
                    (attestation_id, *identity, serialized),
                )
                old = con.execute(
                    "SELECT * FROM discovery_accounts WHERE provider=? AND model=? AND capability=? AND account_scope=?",
                    (*identity, data["account_scope"]),
                ).fetchone()
                account_newer = (
                    old is None or data["account_checked_at"] > old["account_checked_at"]
                )
                live_newer = data["live_checked_at"] is not None and (
                    old is None
                    or old["live_checked_at"] is None
                    or data["live_checked_at"] > old["live_checked_at"]
                )
                account = data if account_newer else dict(old)
                live = (
                    data
                    if live_newer
                    else dict(old)
                    if old is not None
                    else {
                        "live_result": "unverified",
                        "live_checked_at": None,
                        "receipt_id": None,
                    }
                )
                con.execute(
                    "INSERT INTO discovery_accounts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,model,capability,account_scope) DO UPDATE SET "
                    "account_checked_at=excluded.account_checked_at,account_valid_until=excluded.account_valid_until,"
                    "account_availability=excluded.account_availability,live_result=excluded.live_result,"
                    "live_checked_at=excluded.live_checked_at,receipt_id=excluded.receipt_id",
                    (
                        *identity,
                        data["account_scope"],
                        account["account_checked_at"],
                        account["account_valid_until"],
                        account["account_availability"],
                        live["live_result"],
                        live["live_checked_at"],
                        live["receipt_id"],
                    ),
                )
        result = self.lookup(*identity)
        result["attestation_id"] = attestation_id
        result["idempotent"] = bool(prior)
        return result

    def _seed_record(self, identity: tuple[str, str, str]) -> dict[str, Any]:
        spec = self.registry.models.get(identity[:2])
        if spec is None or not self.registry.supports_family(spec, identity[2]):
            return {
                **_reference(identity),
                "hosting": "unknown",
                "endpoint": None,
                "protocol": None,
                "free_eligibility": "unknown",
                "free_source": None,
                "status": "unknown",
                "context_tokens": None,
                "max_output_tokens": None,
                "features": [],
                "source": None,
                "source_kind": "unobserved",
                "checked_at": None,
            }
        return {
            **_reference(identity),
            "hosting": "unknown",
            "endpoint": spec.endpoint_template,
            "protocol": spec.adapter,
            "free_eligibility": "unknown",
            "free_source": None,
            "status": "unknown",
            "context_tokens": spec.context_tokens,
            "max_output_tokens": spec.max_output_tokens,
            "features": list(spec.features),
            "source": None,
            "source_kind": "registry_historical",
            "checked_at": None,
        }

    def _support(self, record: dict[str, Any]) -> tuple[str, list[str]]:
        spec = self.registry.models.get((record["provider"], record["model"]))
        if spec is not None and self.registry.supports_family(spec, record["capability"]):
            adapter_id = dict(spec.family_adapters).get(record["capability"], spec.adapter)
            supported_features = self.registry.adapter_features(adapter_id)
            return "supported", sorted(
                set(spec.features)
                if supported_features is None
                else set(spec.features) & supported_features
            )
        protocol = record["protocol"]
        if protocol is None:
            return "unknown", []
        supported = self.registry.adapter_support(protocol, record["capability"])
        if supported is True:
            return "compatible_unregistered", sorted(self.registry.adapter_features(protocol) or ())
        return ("unknown" if supported is None else "unsupported"), []

    def _record(self, con: sqlite3.Connection, identity: tuple[str, str, str]) -> dict[str, Any]:
        source_rows = con.execute(
            "SELECT * FROM discovery_models WHERE provider=? AND model=? AND capability=? "
            "ORDER BY listing_checked_at DESC,source",
            identity,
        ).fetchall()
        account_rows = con.execute(
            "SELECT * FROM discovery_accounts WHERE provider=? AND model=? AND capability=? "
            "ORDER BY account_scope",
            identity,
        ).fetchall()
        if source_rows:
            latest = source_rows[0]
            record = {
                **_reference(identity),
                **json.loads(latest["metadata_json"]),
                "source": latest["source"],
                "source_kind": "official_snapshot",
                "checked_at": latest["checked_at"],
                "listing_checked_at": latest["listing_checked_at"],
                "status": latest["listing_status"],
            }
        else:
            record = self._seed_record(identity)
        record["sources"] = [
            {
                **json.loads(row["metadata_json"]),
                "source": row["source"],
                "checked_at": row["checked_at"],
                "listing_checked_at": row["listing_checked_at"],
                "status": row["listing_status"],
                "declared_status": json.loads(row["metadata_json"])["status"],
                "free_checked_at": row["checked_at"]
                if json.loads(row["metadata_json"])["free_source"] is not None
                else None,
                "snapshot_id": row["snapshot_id"],
            }
            for row in source_rows
        ]
        now = self.clock()
        record["accounts"] = [
            {
                "account_scope": row["account_scope"],
                "account_checked_at": row["account_checked_at"],
                "account_valid_until": row["account_valid_until"],
                "declared_availability": row["account_availability"],
                "live_result": row["live_result"],
                "live_checked_at": row["live_checked_at"],
                "receipt_id": row["receipt_id"],
                "account_availability": row["account_availability"]
                if _instant(row["account_checked_at"]) <= now < _instant(row["account_valid_until"])
                else "unknown",
            }
            for row in account_rows
        ]
        values = {row["account_availability"] for row in record["accounts"]}
        record["account_availability"] = (
            "allowed" if "allowed" in values else "blocked" if values == {"blocked"} else "unknown"
        )
        live = sorted(
            (row for row in account_rows if row["live_checked_at"]),
            key=lambda row: (row["live_checked_at"], row["account_scope"]),
            reverse=True,
        )
        record.update(
            live_result=live[0]["live_result"] if live else "unverified",
            live_checked_at=live[0]["live_checked_at"] if live else None,
            receipt_id=live[0]["receipt_id"] if live else None,
        )
        record["live_scope"] = live[0]["account_scope"] if live else None
        support, features = self._support(record)
        record["adapter_support"] = support
        record["supported_features"] = features
        record["free_checked_at"] = (
            record["checked_at"] if record["free_source"] is not None else None
        )
        record["evidence_eligible"] = any(
            account["account_availability"] == "allowed"
            and account["live_result"] == "passed"
            and account["live_checked_at"] is not None
            and _instant(account["live_checked_at"]) <= now
            for account in record["accounts"]
        )
        record["evidence_ready"] = (
            record["evidence_eligible"]
            and record["source_kind"] == "official_snapshot"
            and record["status"] == "listed"
            and record["hosting"] in {"hosted", "both"}
            and record["free_eligibility"] == "free"
            and support == "supported"
            and record["endpoint"] is not None
        )
        record["readiness_basis"] = (
            "catalog_and_current_account_and_historical_live_same_scope;not_runtime_admission"
        )
        record["configured_target_ids"] = sorted(
            target.id
            for target in self.targets
            if (target.provider, target.model) == identity[:2]
            and (spec := self.registry.models.get((target.provider, target.model))) is not None
            and self.registry.supports_family(spec, identity[2])
        )
        return record

    def _identities(
        self,
        con: sqlite3.Connection,
        provider: str | None = None,
        model: str | None = None,
        capability: str | None = None,
        cursor: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> Iterator[tuple[str, str, str]]:
        clauses: list[str] = []
        values: list[Any] = []
        for name, value in (("provider", provider), ("model", model), ("capability", capability)):
            if value is not None:
                clauses.append(name + "=?")
                values.append(value)
        if cursor is not None:
            clauses.append("(provider,model,capability)>(?,?,?)")
            values.extend(cursor)
        sql = (
            "SELECT provider,model,capability FROM (SELECT provider,model,capability FROM discovery_models "
            "UNION SELECT provider,model,capability FROM discovery_accounts "
            "UNION SELECT provider,model,capability FROM registry_identities)"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY provider,model,capability"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        for row in con.execute(sql, values):
            yield row[0], row[1], row[2]

    def lookup(self, provider: str, model: str, capability: str) -> dict[str, Any]:
        with self._read() as con:
            identity = next(self._identities(con, provider, model, capability, limit=1), None)
            if identity is not None:
                return self._record(con, identity)
        raise DiscoveryError("not_found", "discovery identity not found")

    def record(self, provider: str, model: str, capability: str) -> dict[str, Any]:
        return self.lookup(provider, model, capability)

    def coverage(
        self,
        provider: str | None = None,
        model: str | None = None,
        capability: str | None = None,
        limit: int = 100,
        before: str | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DiscoveryError("invalid_request", "coverage limit must be 1..1000")
        for value in (provider, model, capability):
            if value is not None and (
                not isinstance(value, str)
                or not 0 < len(value) <= 256
                or any(ord(char) <= 32 for char in value)
            ):
                raise DiscoveryError("invalid_request", "invalid coverage filter")
        cursor = None
        if before is not None:
            try:
                if (
                    not isinstance(before, str)
                    or len(before) > 2048
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", before)
                ):
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 6
                    or decoded[:3] != [provider, model, capability]
                    or any(not isinstance(value, str) for value in decoded[3:])
                ):
                    raise ValueError
                cursor = tuple(decoded[3:])
            except (ValueError, TypeError) as exc:
                raise DiscoveryError("invalid_request", "invalid coverage cursor") from exc
        with self._read() as con:
            identities = list(self._identities(con, provider, model, capability, cursor, limit + 1))
            page = [self._record(con, identity) for identity in identities[:limit]]
            next_before = None
            if len(identities) > limit:
                next_before = (
                    base64.urlsafe_b64encode(
                        canonical([provider, model, capability, *_identity(page[-1])]).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            dimensions = {
                "status": ("listed", "retired", "not_listed", "unknown"),
                "hosting": ("hosted", "selfhost", "both", "unknown"),
                "free_eligibility": ("free", "paid", "restricted", "unknown"),
                "account_availability": ("allowed", "blocked", "unknown"),
                "adapter_support": (
                    "supported",
                    "compatible_unregistered",
                    "unsupported",
                    "unknown",
                ),
                "live_result": ("passed", "failed", "unknown", "unverified"),
            }
            counters: dict[str, Counter[str]] = {name: Counter() for name in dimensions}
            provider_ids = {provider} if provider else set()
            total = 0
            eligible = ready = 0
            unique_models = 0
            previous_model = None
            # Aggregate the complete filtered population one identity at a time;
            # only the requested page materializes full metadata records.
            for identity in self._identities(con, provider, model, capability):
                record = self._record(con, identity)
                total += 1
                model_identity = identity[:2]
                if model_identity != previous_model:
                    unique_models += 1
                    previous_model = model_identity
                eligible += int(record["evidence_eligible"])
                ready += int(record["evidence_ready"])
                provider_ids.add(identity[0])
                for dimension in dimensions:
                    counters[dimension][record[dimension]] += 1
            summary: dict[str, Any] = {
                "total": total,
                "unique_models": unique_models,
                "model_capability_records": total,
                "evidence_eligible": eligible,
                "evidence_ready": ready,
                "readiness_basis": "catalog_and_current_account_and_historical_live_same_scope;not_runtime_admission",
            }
            for name, states in dimensions.items():
                summary[name] = {state: counters[name][state] for state in states}
            providers = []
            for name in sorted(provider_ids):
                sources = [
                    dict(row)
                    for row in con.execute(
                        "SELECT source,checked_at,complete,snapshot_id,last_complete_at FROM discovery_sources WHERE provider=? ORDER BY source",
                        (name,),
                    )
                ]
                for source in sources:
                    source["complete"] = bool(source["complete"])
                providers.append(
                    {
                        "provider": name,
                        "source_catalog_completeness": "unobserved"
                        if not sources
                        else "complete"
                        if all(source["complete"] for source in sources)
                        else "partial",
                        "sources": sources,
                        "provider_capability_completeness": "unknown",
                    }
                )
        return {
            "records": page,
            "next_before": next_before,
            "summary": summary,
            "providers": providers,
            "denominator": {
                "state": "unknown",
                "count": None,
                "scope": "currently_free_account_accessible_capabilities",
            },
            "coverage_percent": None,
        }
