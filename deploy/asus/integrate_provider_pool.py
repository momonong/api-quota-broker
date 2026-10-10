"""One reviewed ASUS integration transaction; default only prints a zero-effect plan.

Private preflight uses readonly SQLite and public-intent files, before credentials,
Gateway construction, claims or mutation. Legacy claims are never replayed/deleted.
"""

import ctypes
import hashlib
import io
import json
import os
import re
import resource
import sqlite3
import stat
import sys
import tarfile
import time
from pathlib import Path

PROVIDERS = ("nvidia", "google", "mistral", "cloudflare", "openrouter", "ocrspace", "groq")
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
CONFIG = Path("/etc/api-quota-broker/gateway.json")
BACKUPS = Path("/var/backups/api-quota-broker")
OLD_CLAIM = BACKUPS / "provider-pool-2026-10-04-r1.claim.json"
OLD_JOURNAL = BACKUPS / "provider-pool-2026-10-04-r1.json"
OLD_BOOTSTRAP = BACKUPS / "pool-bootstrap-2026-10-04-r1"
OLD_REVIEW_TAR = Path(
    "/var/tmp/api-quota-broker-provider-pool-review-2026-10-04-r1/provider-pool-r1.tar"
)
GROQ_KEY = "asus-v01-groq-e2e-after-recovery-2026-10-04-once"
PREFIX = "asus-integrated-2026-10-07-r1-"
CLAIM = BACKUPS / "seven-route-integrated-2026-10-07-r1.claim.json"
JOURNAL = BACKUPS / "seven-route-integrated-2026-10-07-r1.json"
UNIT = "api-quota-broker-integrated.service"
TOTAL_SECONDS = 1200
CLEANUP_SECONDS = 240
POST_MAX = 7
RESTART_MAX = 4
BROKER_DOPPLER_GET_MAX = 9
OPS_DOPPLER_GET_MAX = 1
TABLES = (
    "gateway_tasks",
    "gateway_attempts",
    "reservations",
    "charges",
    "execution_completion",
    "queue_jobs",
    "queue_attempts",
)
STATES = {
    "completed",
    "completed_usage_unknown",
    "unknown",
    "dispatched",
    "reserved",
    "rejected",
    "pre_send_failed",
    "quota_rejected",
    "quota_exhausted",
    "cancelled",
    "expired",
}
CODES = {
    "private_metadata_untrusted",
    "ledger_integrity",
    "ledger_schema",
    "history_bound",
    "history_ambiguous",
    "history_scope_unknown",
    "history_consistency",
    "integration_claim_exists",
    "integration_authority_unverified",
    "budget_insufficient",
    "restart_budget",
    "post_budget",
    "ops_pin_drift",
    "ops_acceptance_failed",
    "integration_unverified",
    "readiness",
    "groq_evidence_missing",
    "history_changed",
    "routing_acceptance",
    "service_changes",
    "credentials_unavailable",
    "package_unverified",
    "unit_scope",
    "no_candidates",
}


class Blocked(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Blocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "private_metadata_untrusted")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(Blocked("private_metadata_untrusted")),
    )


def trusted_read(path, *, uid=0, gid=0, mode=0o600, limit=131072):
    path = Path(path)
    for parent in reversed(tuple(path.parents)):
        s = parent.lstat()
        need(
            stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
            "private_metadata_untrusted",
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        a = os.fstat(f.fileno())
        need(
            stat.S_ISREG(a.st_mode)
            and a.st_uid == uid
            and a.st_gid == gid
            and a.st_nlink == 1
            and stat.S_IMODE(a.st_mode) == mode
            and a.st_size <= limit,
            "private_metadata_untrusted",
        )
        raw = f.read(limit + 1)
        b = os.fstat(f.fileno())
        need(
            len(raw) == a.st_size
            and (a.st_dev, a.st_ino, a.st_mtime_ns, a.st_ctime_ns)
            == (b.st_dev, b.st_ino, b.st_mtime_ns, b.st_ctime_ns),
            "private_metadata_untrusted",
        )
        return raw


def present(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def inspect_old_intents(claim, journal):
    """Only fixed provider labels are projected; no raw paths/errors/results."""
    intended = set()
    for value in (claim, journal):
        if value is None:
            continue
        need(
            type(value) is dict and value.get("mode") == "asus_provider_pool_r1",
            "history_ambiguous",
        )
        labels = value.get("posts_intended")
        need(
            type(labels) is list
            and len(labels) <= 6
            and all(type(p) is str and p in PROVIDERS for p in labels)
            and len(set(labels)) == len(labels),
            "history_ambiguous",
        )
        intended.update(labels)
    return intended


def classify_ledger(con, intended, candidate_scopes=None):
    """Bounded metadata only. Result does not grant admission or replay permission."""
    con.row_factory = sqlite3.Row
    need(con.execute("PRAGMA query_only").fetchone()[0] == 1, "ledger_integrity")
    need(con.execute("PRAGMA quick_check").fetchone()[0] == "ok", "ledger_integrity")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    need(set(TABLES) <= names, "ledger_schema")
    bounds = {table: 8192 for table in TABLES}
    bounds.update(gateway_tasks=4096, charges=32768)
    counts = {table: con.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in TABLES}
    need(all(counts[table] <= limit for table, limit in bounds.items()), "history_bound")
    need(
        con.execute("SELECT coalesce(sum(length(target_snapshot)),0) FROM reservations").fetchone()[
            0
        ]
        <= 16777216,
        "history_bound",
    )
    # Bound variable metadata before fetching it; never select prompts, results,
    # ciphertext, payload HMACs, request IDs, or error prose.
    for table, lengths in (
        ("gateway_tasks", {"request_key": 120, "model": 256, "reservation_id": 128}),
        ("gateway_attempts", {"request_key": 120, "reservation_id": 128}),
        ("reservations", {"id": 128, "target_snapshot": 32768, "shared_scope": 512}),
    ):
        for column, bound in lengths.items():
            need(
                con.execute(
                    "SELECT 1 FROM " + table + " WHERE length(" + column + ")>? LIMIT 1", (bound,)
                ).fetchone()
                is None,
                "history_bound",
            )
    tasks = con.execute(
        "SELECT request_key,state,provider,model,reservation_id,dispatched_at,http_status,"
        "reported_input_tokens,reported_output_tokens FROM gateway_tasks"
    ).fetchall()
    attempts = con.execute(
        "SELECT request_key,attempt_no,reservation_id,provider,state,dispatched_at,"
        "http_status,reported_input_tokens,reported_output_tokens FROM gateway_attempts"
    ).fetchall()
    reservations = con.execute(
        "SELECT id,target_id,state,target_snapshot,shared_scope,dispatched_at FROM reservations"
    ).fetchall()
    done = {r[0] for r in con.execute("SELECT reservation_id FROM execution_completion")}
    charges = {}
    for row in con.execute("SELECT reservation_id,bucket,metric,amount FROM charges"):
        need(type(row["amount"]) is int and row["amount"] >= 0, "history_consistency")
        charges.setdefault(row["reservation_id"], []).append(row)
    lookup = {r["id"]: r for r in reservations}
    need(
        len(lookup) == len(reservations)
        and done <= lookup.keys()
        and charges.keys() <= lookup.keys(),
        "history_consistency",
    )
    task_lookup = {t["request_key"]: t for t in tasks}
    need(len(task_lookup) == len(tasks), "history_consistency")
    bykey, owners, scope_owners, row_scope = {}, {}, {}, {}
    for a in attempts:
        need(
            a["request_key"] in task_lookup
            and a["reservation_id"] in lookup
            and a["provider"] in PROVIDERS
            and a["state"] in STATES
            and type(a["attempt_no"]) is int
            and a["attempt_no"] >= 0,
            "history_consistency",
        )
        bykey.setdefault(a["request_key"], []).append(a)
        owners.setdefault(a["reservation_id"], set()).add(a["provider"])
    for r in reservations:
        need(r["state"] in STATES, "history_consistency")
        if r["target_snapshot"] is not None:
            snapshot = strict_json(r["target_snapshot"])
            need(
                type(snapshot) is dict and snapshot.get("provider") in PROVIDERS,
                "history_scope_unknown",
            )
            provider = snapshot["provider"]
            need(not owners.get(r["id"]) or owners[r["id"]] == {provider}, "history_consistency")
            owners.setdefault(r["id"], set()).add(provider)
        scope = r["shared_scope"]
        if scope is not None:
            need(type(scope) is str and bool(scope), "history_scope_unknown")
            row_scope[r["id"]] = scope
            scope_owners.setdefault(scope, set()).update(owners.get(r["id"], set()))
    for provider, scopes in (candidate_scopes or {}).items():
        need(provider in PROVIDERS, "history_consistency")
        for scope in scopes:
            scope_owners.setdefault(scope, set()).add(provider)
    classes = {
        "known_groq_settled": 0,
        "not_dispatched": 0,
        "dispatched_known_result": 0,
        "dispatch_or_settlement_unknown": 0,
    }
    blocked, observed, unsettled_scopes = set(), set(), set()
    provider_attempts = {provider: 0 for provider in PROVIDERS}
    provider_dispatches = {provider: 0 for provider in PROVIDERS}
    for attempt in attempts:
        provider_attempts[attempt["provider"]] += 1
        if attempt["dispatched_at"] or lookup[attempt["reservation_id"]]["dispatched_at"]:
            provider_dispatches[attempt["provider"]] += 1
    for r in reservations:
        if r["state"] in {"reserved", "dispatched", "unknown"}:
            need(
                r["id"] in row_scope
                and len(owners.get(r["id"], set())) == 1
                and r["target_snapshot"] is not None,
                "history_scope_unknown",
            )
            unsettled_scopes.add(row_scope[r["id"]])
            blocked.update(owners[r["id"]])
    for t in tasks:
        need(
            type(t["request_key"]) is str
            and t["state"] in STATES
            and (t["provider"] is None or t["provider"] in PROVIDERS),
            "history_consistency",
        )
        rows = sorted(bykey.get(t["request_key"], []), key=lambda a: a["attempt_no"])
        need(len({a["attempt_no"] for a in rows}) == len(rows), "history_consistency")
        if rows:
            need(
                t["reservation_id"] == rows[-1]["reservation_id"]
                and t["provider"] == rows[-1]["provider"]
                and (
                    t["state"] == rows[-1]["state"]
                    or (t["state"] == "quota_exhausted" and rows[-1]["state"] == "quota_rejected")
                    or (t["state"] == "rejected" and rows[-1]["state"] == "pre_send_failed")
                ),
                "history_consistency",
            )
        known = bool(rows)
        sent = bool(t["dispatched_at"])
        for a in rows:
            r = lookup[a["reservation_id"]]
            dispatched = bool(a["dispatched_at"] or r["dispatched_at"])
            sent |= dispatched
            if a["state"] in {"completed", "completed_usage_unknown"}:
                need(dispatched, "history_consistency")
                known &= a["reservation_id"] in done
                if a["state"] == "completed":
                    known &= r["state"] == "completed" and bool(charges.get(r["id"]))
                else:
                    need(r["state"] == "unknown", "history_consistency")
            elif a["state"] in {"quota_rejected", "pre_send_failed"}:
                known &= r["state"] in {"quota_rejected", "cancelled"}
            else:
                known = False
        for provider in PROVIDERS:
            if t["request_key"] == "asus-pool-2026-10-04-r1-" + provider + "-a1":
                observed.add(provider)
        if t["request_key"] == GROQ_KEY:
            values = charges.get(rows[0]["reservation_id"], []) if len(rows) == 1 else []
            charge_basis = sorted((c["metric"], c["amount"]) for c in values)
            need(
                t["provider"] == "groq"
                and t["model"] == "openai/gpt-oss-20b"
                and t["state"] == "completed"
                and len(rows) == 1
                and known
                and sent
                and t["http_status"] == rows[0]["http_status"] == 200
                and t["reported_input_tokens"] == rows[0]["reported_input_tokens"] == 76
                and t["reported_output_tokens"] == rows[0]["reported_output_tokens"] == 25
                and charge_basis == [("input_tokens", 76), ("requests", 1), ("requests", 1)]
                and all(c["bucket"].startswith("groq:asus-dev-key:") for c in values),
                "groq_evidence_missing",
            )
            classes["known_groq_settled"] += 1
        elif not sent:
            need(
                t["state"] not in {"completed", "completed_usage_unknown", "dispatched", "unknown"},
                "history_consistency",
            )
            classes["not_dispatched"] += 1
        elif known:
            classes["dispatched_known_result"] += 1
        else:
            classes["dispatch_or_settlement_unknown"] += 1
            need(t["provider"] in PROVIDERS, "history_scope_unknown")
            blocked.add(t["provider"])
    need(classes["known_groq_settled"] == 1, "groq_evidence_missing")
    blocked.update(intended - observed)
    for scope in unsettled_scopes:
        blocked.update(scope_owners[scope])
    need(
        con.execute(
            "SELECT count(*) FROM queue_jobs WHERE state NOT IN "
            "('completed','cancelled','rejected','expired')"
        ).fetchone()[0]
        == 0,
        "history_ambiguous",
    )
    return {
        "classification": classes,
        "blocked_providers": sorted(blocked),
        "table_counts": counts,
        "old_intents_without_replay": sorted(intended),
        "old_intents_without_ledger": sorted(intended - observed),
        "provider_attempt_counts": provider_attempts,
        "provider_dispatch_counts": provider_dispatches,
        "unsettled_shared_scope_count": len(unsettled_scopes),
        "not_history_quarantined_providers": sorted(set(PROVIDERS) - {"groq"} - blocked),
        "query_only": True,
        "old_groq_replay": False,
        "probe_permission": False,
    }


def read_old_review_bundle(path=OLD_REVIEW_TAR):
    """One fixed nonsecret archive under a pinned sticky /var/tmp hierarchy."""
    path = Path(path)
    need(path == OLD_REVIEW_TAR, "package_unverified")
    for parent in (Path("/"), Path("/var")):
        s = parent.lstat()
        need(
            stat.S_ISDIR(s.st_mode) and s.st_uid == s.st_gid == 0 and not s.st_mode & 0o022,
            "package_unverified",
        )
    s = Path("/var/tmp").lstat()
    need(
        stat.S_ISDIR(s.st_mode) and s.st_uid == s.st_gid == 0 and stat.S_IMODE(s.st_mode) == 0o1777,
        "package_unverified",
    )
    s = path.parent.lstat()
    need(
        stat.S_ISDIR(s.st_mode)
        and s.st_uid == s.st_gid == 1000
        and stat.S_IMODE(s.st_mode) == 0o700,
        "package_unverified",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == 1000
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1
            and before.st_size <= 4194304,
            "package_unverified",
        )
        raw = stream.read(4194305)
        after = os.fstat(stream.fileno())
        need(
            len(raw) == before.st_size
            and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
            "history_changed",
        )
        return raw


def audit_bootstrap(
    *, root=OLD_BOOTSTRAP, read=trusted_read, exists=present, bundle_read=read_old_review_bundle
):
    """Hash fixed code/policy files only. No import, extraction, or old entry execution."""
    if not exists(root):
        return {"present": False, "resume_permission": False}
    meta = Path(root).lstat()
    need(
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == 0
        and stat.S_IMODE(meta.st_mode) == 0o700,
        "private_metadata_untrusted",
    )
    names = {
        "source.tar",
        "enable_provider_pool.py",
        "provider_pool_plan.py",
        "build_asus_release.py",
        "policy.json",
    }
    need({p.name for p in Path(root).iterdir()} == names, "package_unverified")
    rows, raw = {}, {}
    for name in sorted(names):
        path = Path(root) / name
        raw[name] = read(path, limit=4194304 if name == "source.tar" else 131072)
        info = path.lstat()
        rows[name] = {
            "sha256": hashlib.sha256(raw[name]).hexdigest(),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": "0600",
            "inode": info.st_ino,
            "bytes": info.st_size,
            "nlink": info.st_nlink,
        }
    policy = strict_json(raw["policy.json"])
    need(type(policy) is dict, "package_unverified")
    for name, field in (
        ("source.tar", "source_archive_sha256"),
        ("enable_provider_pool.py", "operator_sha256"),
        ("provider_pool_plan.py", "planner_sha256"),
        ("build_asus_release.py", "verifier_sha256"),
    ):
        need(
            type(policy.get(field)) is str
            and re.fullmatch(r"[a-f0-9]{64}", policy[field])
            and rows[name]["sha256"] == policy[field],
            "package_unverified",
        )
    published = "64fe2714f2bc0f837cff9f0d6f97f1efa772cb222b3388e012363954e16f99ec"
    provenance = False
    outer_present = exists(OLD_REVIEW_TAR)
    if outer_present:
        outer = bundle_read()
        expected = "76556abc1f334df60c217406e3f38683ccab84b20481800fe2987f03e52964ad"
        need(hashlib.sha256(outer).hexdigest() == expected, "package_unverified")
        with tarfile.open(fileobj=io.BytesIO(outer), mode="r:") as archive:
            members = archive.getmembers()
            need(len(members) == 5 and {m.name for m in members} == names, "package_unverified")
            for member in members:
                need(
                    member.isfile()
                    and member.uid == member.gid == 0
                    and member.mode == 0o600
                    and member.mtime == 0
                    and not member.pax_headers
                    and member.size == len(raw[member.name]),
                    "package_unverified",
                )
                need(archive.extractfile(member).read() == raw[member.name], "package_unverified")
        provenance = True
    return {
        "present": True,
        "files": rows,
        "internal_policy_hashes_match": True,
        "source_matches_published_r1": rows["source.tar"]["sha256"] == published,
        "original_outer_bundle_present": outer_present,
        "historical_bundle_provenance_verified": provenance,
        "resume_permission": False,
    }


def project_ops_pins(config_sha, *, read=trusted_read):
    """Never import the privileged worker or open its encrypted credential."""
    policy = read(Path("/etc/api-quota-broker-ops/policy.json"), mode=0o644)
    entry = read(Path("/usr/local/lib/api-quota-broker-ops/ops_entry.py"), mode=0o644)
    value = strict_json(policy)
    need(
        type(value) is dict
        and type(value.get("enabled")) is bool
        and value.get("schema") == 1
        and value.get("ops_uid") == 994
        and value.get("ops_gid") == 981
        and value.get("peer_uid") == 1000
        and type(value.get("config_sha256")) is str
        and re.fullmatch(r"[a-f0-9]{64}", value["config_sha256"]),
        "ops_pin_drift",
    )
    expected_entry = "0f2e75149567b27e2184ccbc89ba0e7da222a2a9cb6aa20268a6b757062492dc"
    need(value.get("expires_at") == "2026-11-05T04:33:31+00:00", "ops_pin_drift")
    return {
        "policy_sha256": hashlib.sha256(policy).hexdigest(),
        "entry_sha256": hashlib.sha256(entry).hexdigest(),
        "entry_matches_r14": hashlib.sha256(entry).hexdigest() == expected_entry,
        "config_pin_matches": value["config_sha256"] == config_sha,
        "restart_enabled": value["enabled"],
        "expiry_matches_r14": True,
        "credential_opened": False,
    }


def project_release_pins(*, read=trusted_read):
    selector = Path("/opt/api-quota-broker/current")
    meta = selector.lstat()
    need(stat.S_ISLNK(meta.st_mode) and meta.st_uid == meta.st_gid == 0, "package_unverified")
    release = str(selector.readlink())
    need(re.fullmatch(r"releases/release-[a-f0-9]{64}", release), "package_unverified")
    manifest = read(Path("/opt/api-quota-broker") / release / "release-manifest.json", mode=0o644)
    need(hashlib.sha256(manifest).hexdigest() == release.rsplit("-", 1)[1], "package_unverified")
    unit = read(Path("/etc/systemd/system/api-quota-broker.service"), mode=0o644)
    need(str(selector.readlink()) == release, "history_changed")
    return {
        "release": release,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "unit_sha256": hashlib.sha256(unit).hexdigest(),
        "release_matches_r14": release
        == "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770",
        "unit_matches_r14": hashlib.sha256(unit).hexdigest()
        == "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060",
    }


def private_preflight(
    *,
    read=trusted_read,
    db=DB,
    config=CONFIG,
    claim=OLD_CLAIM,
    journal=OLD_JOURNAL,
    exists=present,
    bootstrap=audit_bootstrap,
    ops_pins=project_ops_pins,
    release_pins=project_release_pins,
):
    """First phase: exact readonly audit; no credential, Gateway, claim, or migration."""
    need(not exists(CLAIM) and not exists(JOURNAL), "integration_claim_exists")
    original = read(config, gid=982, mode=0o640)
    parsed = strict_json(original)
    need(
        type(parsed) is dict
        and type(parsed.get("targets")) is list
        and len(parsed["targets"]) <= 128,
        "private_metadata_untrusted",
    )
    scopes, providers = {}, set()
    for target in parsed["targets"]:
        need(
            type(target) is dict and target.get("provider") in PROVIDERS,
            "private_metadata_untrusted",
        )
        provider, scope = target["provider"], target.get("shared_concurrency_scope")
        providers.add(provider)
        if scope is not None:
            need(type(scope) is str and 0 < len(scope) <= 512, "private_metadata_untrusted")
            scopes.setdefault(provider, set()).add(scope)
    old_rows = {}
    for name, path in (("claim", claim), ("journal", journal)):
        raw = read(path) if exists(path) else None
        old_rows[name] = (strict_json(raw) if raw is not None else None, raw)
    intended = inspect_old_intents(old_rows["claim"][0], old_rows["journal"][0])
    for parent in reversed(tuple(Path(db).parents)):
        meta = parent.lstat()
        need(
            stat.S_ISDIR(meta.st_mode) and meta.st_uid in {0, 995} and not meta.st_mode & 0o022,
            "private_metadata_untrusted",
        )
    meta = Path(db).lstat()
    need(
        stat.S_ISREG(meta.st_mode)
        and meta.st_uid == 995
        and meta.st_gid == 982
        and stat.S_IMODE(meta.st_mode) == 0o600
        and meta.st_nlink == 1
        and meta.st_size <= 67108864,
        "private_metadata_untrusted",
    )
    identity = meta.st_dev, meta.st_ino
    con = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=5)
    deadline = time.monotonic() + 60
    con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        after = Path(db).lstat()
        need(
            identity == (after.st_dev, after.st_ino) and stat.S_ISREG(after.st_mode),
            "history_changed",
        )
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        report = classify_ledger(con, intended, scopes)
        config_sha = hashlib.sha256(original).hexdigest()
        report.update(
            config_targets=len(parsed["targets"]),
            config_providers=sorted(providers),
            config_sha256=config_sha,
            old_files={
                name: {
                    "present": raw is not None,
                    "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
                }
                for name, (_, raw) in old_rows.items()
            },
            old_bootstrap=bootstrap(read=read, exists=exists),
            r14_pins=ops_pins(config_sha, read=read),
            broker_pins=release_pins(read=read),
            private_read_only_before_credentials=True,
        )
        need(read(config, gid=982, mode=0o640) == original, "history_changed")
        for name, path in (("claim", claim), ("journal", journal)):
            need((read(path) if exists(path) else None) == old_rows[name][1], "history_changed")
        return report, original
    except sqlite3.OperationalError:
        raise Blocked(
            "history_bound" if time.monotonic() >= deadline else "ledger_integrity"
        ) from None
    finally:
        con.close()


class Budget:
    def __init__(self, start=None, *, clock=time.monotonic):
        self.clock = clock
        self.end = (clock() if start is None else start) + TOTAL_SECONDS
        self.posts = 0
        self.restarts = 0

    def before_post(self, provider):
        seconds = 150 if provider in ("nvidia", "automatic") else 60
        need(self.posts < POST_MAX, "post_budget")
        need(self.end - self.clock() >= seconds + CLEANUP_SECONDS + 60, "budget_insufficient")
        self.posts += 1
        return seconds

    def before_restart(self, *, rollback=False):
        need(self.restarts < RESTART_MAX, "restart_budget")
        need(
            self.end - self.clock() >= (25 if rollback else CLEANUP_SECONDS + 25),
            "budget_insufficient",
        )
        self.restarts += 1


def plan():
    return {
        "mode": "asus_history_readonly_plan",
        "apply": False,
        "provider_posts_max": 0,
        "provider_model_metadata_gets": 0,
        "broker_doppler_gets_max": 0,
        "ops_doppler_gets_max": 0,
        "broker_restarts_max_including_rollback": 0,
        "deadline_seconds": 150,
        "production_db_writes": 0,
        "automatic_retry": False,
        "new_token_creation": 0,
        "private_preflight_before_credentials": True,
        "old_claim_replay": False,
        "r14_scope": "readonly pins projection only",
        "conditional_apply_supported": False,
        "future_integration_proposal": {
            "provider_posts_max": POST_MAX,
            "broker_doppler_gets_max": BROKER_DOPPLER_GET_MAX,
            "ops_doppler_gets_max": OPS_DOPPLER_GET_MAX,
            "broker_restarts_max_including_rollback": RESTART_MAX,
            "deadline_seconds": TOTAL_SECONDS,
            "cleanup_reserve_seconds": CLEANUP_SECONDS,
            "approved": False,
        },
    }


def protected_audit_main():
    # No LoadCredentialEncrypted is present in this unit or wrapper.
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server", "unit_scope")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    need(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0), "unit_scope")
    libc = ctypes.CDLL(None)
    need(
        libc.prctl(4, 0, 0, 0, 0) == 0
        and libc.prctl(3, 0, 0, 0, 0) == 0
        and libc.prctl(39, 0, 0, 0, 0) == 1,
        "unit_scope",
    )
    name = "api-quota-broker-history-audit.service"
    need(Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + name, "unit_scope")
    group = Path("/sys/fs/cgroup/system.slice") / name
    need(
        (group / "memory.max").read_text().strip() == "402653184"
        and (group / "memory.swap.max").read_text().strip() == "0",
        "unit_scope",
    )
    need(
        all(os.statvfs(path).f_flag & os.ST_RDONLY for path in (DB, BACKUPS, CONFIG)), "unit_scope"
    )
    report, _private_config = private_preflight()
    # Read-only evidence is never an admission/authority grant.
    report.update(
        status="passed",
        mode="private_history_readonly",
        apply=False,
        credential_access=0,
        provider_posts=0,
        provider_model_metadata_gets=0,
        db_write=0,
        old_claim_replay=False,
        conditional_apply_ready=False,
        next_gate="current admission and exact integration policy required",
    )
    return report


def main():
    if sys.argv[1:] == []:
        print(json.dumps(plan(), sort_keys=True))
        return 0
    try:
        need(sys.argv[1:] == ["--audit-only"], "integration_authority_unverified")
        report = protected_audit_main()
    except BaseException as error:  # noqa: BLE001 - no raw private exception output.
        code = (
            error.args[0]
            if type(error) is Blocked
            and error.args
            and type(error.args[0]) is str
            and error.args[0] in CODES
            else "integration_unverified"
        )
        report = {
            "status": "blocked",
            "mode": "private_history_readonly",
            "code": code,
            "apply": False,
            "credential_access": 0,
            "provider_posts": 0,
            "db_write": 0,
            "old_claim_replay": False,
            "automatic_retry": False,
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
