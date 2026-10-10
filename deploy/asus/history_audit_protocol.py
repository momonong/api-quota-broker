"""Pure, strict safe-output contract for a single fixed private history audit."""

import json
import re

LIMIT = 32768
PROVIDERS = ("nvidia", "google", "mistral", "cloudflare", "openrouter", "ocrspace", "groq")
TABLES = (
    "gateway_tasks",
    "gateway_attempts",
    "reservations",
    "charges",
    "execution_completion",
    "queue_jobs",
    "queue_attempts",
)
BOOT_FILES = (
    "source.tar",
    "enable_provider_pool.py",
    "provider_pool_plan.py",
    "build_asus_release.py",
    "policy.json",
)
CLASSES = (
    "known_groq_settled",
    "not_dispatched",
    "dispatched_known_result",
    "dispatch_or_settlement_unknown",
)
CHECKS = {
    "reader",
    "sandbox",
    "package",
    "config",
    "old_intents",
    "ledger",
    "bootstrap",
    "ops_policy",
    "release",
    "result",
}
CODES = {
    "audit_schema_missing_tables",
    "audit_schema_missing_columns",
    "audit_ledger_integrity",
    "audit_ledger_inconsistent",
    "audit_bounds_exceeded",
    "audit_legacy_scope_missing",
    "audit_queue_active",
    "audit_groq_evidence_mismatch",
    "audit_metadata_untrusted",
    "audit_input_changed",
    "audit_config_invalid",
    "audit_old_intent_invalid",
    "audit_package_provenance_mismatch",
    "audit_ops_pin_drift",
    "audit_required_input_missing",
    "audit_required_input_inaccessible",
    "audit_sqlite_unavailable",
    "audit_sql_timeout",
    "audit_readonly_guard",
    "audit_package_invalid",
    "audit_result_missing",
    "audit_result_untrusted",
    "audit_unit_failed",
    "audit_unit_wait_unknown",
    "audit_request_replayed",
    "audit_busy",
    "audit_storage_bound",
    "audit_internal_error",
    "audit_policy_expired",
    "audit_account_identity",
}
SOURCE_CODES = {
    "ledger_schema": "audit_schema_missing_tables",
    "ledger_integrity": "audit_ledger_integrity",
    "history_bound": "audit_bounds_exceeded",
    "history_consistency": "audit_ledger_inconsistent",
    "history_scope_unknown": "audit_legacy_scope_missing",
    "history_ambiguous": "audit_old_intent_invalid",
    "groq_evidence_missing": "audit_groq_evidence_mismatch",
    "private_metadata_untrusted": "audit_metadata_untrusted",
    "history_changed": "audit_input_changed",
    "package_unverified": "audit_package_provenance_mismatch",
    "ops_pin_drift": "audit_ops_pin_drift",
    "token_expired": "audit_policy_expired",
    "identity_changed": "audit_account_identity",
}


class Invalid(ValueError):
    pass


def need(ok):
    if not ok:
        raise Invalid("audit_result_untrusted")


def number(value, maximum=32768):
    need(type(value) is int and 0 <= value <= maximum)


def digest(value):
    need(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value))


def labels(value):
    need(
        type(value) is list
        and len(value) <= 7
        and all(type(x) is str and x in PROVIDERS for x in value)
        and value == sorted(set(value))
    )


def keys(value, names):
    need(type(value) is dict and set(value) == set(names))


def counters(value, names):
    keys(value, names)
    for count in value.values():
        number(count)


def validate_summary(s):
    keys(
        s,
        {
            "classification",
            "blocked_providers",
            "not_history_quarantined_providers",
            "table_counts",
            "provider_attempt_counts",
            "provider_dispatch_counts",
            "provider_usage",
            "intent_providers",
            "intents_without_ledger",
            "unsettled_shared_scope_count",
            "config",
            "old_files",
            "old_bootstrap",
            "ops_pins",
            "broker_pins",
            "probe_permission",
            "resume_permission",
            "query_only",
            "credential_reads",
            "provider_posts",
            "db_write",
        },
    )
    counters(s["classification"], CLASSES)
    counters(s["table_counts"], TABLES)
    counters(s["provider_attempt_counts"], PROVIDERS)
    counters(s["provider_dispatch_counts"], PROVIDERS)
    keys(s["provider_usage"], PROVIDERS)
    total_dimensions = 0
    for usage in s["provider_usage"].values():
        keys(
            usage,
            {
                "reported_input_tokens_sum",
                "reported_output_tokens_sum",
                "rows_with_input_tokens",
                "rows_with_output_tokens",
                "completed_execution_attempts",
                "quota_unsettled_reservations",
                "quota_dimensions",
            },
        )
        for name in ("reported_input_tokens_sum", "reported_output_tokens_sum"):
            if usage[name] is not None:
                number(usage[name], 2**63 - 1)
        for name in (
            "rows_with_input_tokens",
            "rows_with_output_tokens",
            "completed_execution_attempts",
            "quota_unsettled_reservations",
        ):
            number(usage[name], 8192)
        need(type(usage["quota_dimensions"]) is list and len(usage["quota_dimensions"]) <= 64)
        total_dimensions += len(usage["quota_dimensions"])
        seen = set()
        for row in usage["quota_dimensions"]:
            keys(
                row,
                {
                    "bucket_sha256",
                    "metric",
                    "settled_rows",
                    "settled_amount_sum",
                    "unsettled_rows",
                    "unsettled_amount_sum",
                    "released_rows",
                    "released_amount_sum",
                },
            )
            digest(row["bucket_sha256"])
            need(
                row["bucket_sha256"] not in seen
                and row["metric"] in {"requests", "input_tokens", "neurons", "other"}
            )
            seen.add(row["bucket_sha256"])
            number(row["settled_rows"])
            number(row["unsettled_rows"])
            number(row["released_rows"])
            number(row["settled_amount_sum"], 2**63 - 1)
            number(row["unsettled_amount_sum"], 2**63 - 1)
            number(row["released_amount_sum"], 2**63 - 1)
    need(total_dimensions <= 64)
    for name in (
        "blocked_providers",
        "not_history_quarantined_providers",
        "intent_providers",
        "intents_without_ledger",
    ):
        labels(s[name])
    number(s["unsettled_shared_scope_count"], 8192)
    keys(s["config"], {"sha256", "target_count", "providers"})
    digest(s["config"]["sha256"])
    number(s["config"]["target_count"], 128)
    labels(s["config"]["providers"])
    keys(s["old_files"], {"claim", "journal"})
    for row in s["old_files"].values():
        keys(row, {"present", "sha256"})
        need(type(row["present"]) is bool)
        if row["present"]:
            digest(row["sha256"])
        else:
            need(row["sha256"] is None)
    boot = s["old_bootstrap"]
    need(type(boot) is dict and type(boot.get("present")) is bool)
    if boot["present"]:
        keys(
            boot,
            {
                "present",
                "files",
                "internal_policy_hashes_match",
                "source_matches_published_r1",
                "original_outer_bundle_present",
                "historical_bundle_provenance_verified",
                "resume_permission",
            },
        )
        keys(boot["files"], BOOT_FILES)
        for name, row in boot["files"].items():
            keys(row, {"sha256", "uid", "gid", "mode", "inode", "bytes", "nlink"})
            digest(row["sha256"])
            need(
                type(row["uid"]) is int
                and type(row["gid"]) is int
                and row["uid"] == row["gid"] == 0
                and row["mode"] == "0600"
                and type(row["nlink"]) is int
                and row["nlink"] == 1
            )
            number(row["inode"], 2**64 - 1)
            number(row["bytes"], 4194304 if name == "source.tar" else 131072)
        for name in (
            "internal_policy_hashes_match",
            "source_matches_published_r1",
            "original_outer_bundle_present",
            "historical_bundle_provenance_verified",
        ):
            need(type(boot[name]) is bool)
    else:
        keys(boot, {"present", "resume_permission"})
    need(boot["resume_permission"] is False)
    ops = s["ops_pins"]
    keys(
        ops,
        {
            "policy_sha256",
            "entry_sha256",
            "entry_matches_installed_manifest",
            "config_pin_matches",
            "restart_enabled",
            "expiry_matches_r14",
            "credential_opened",
        },
    )
    digest(ops["policy_sha256"])
    digest(ops["entry_sha256"])
    need(
        ops["restart_enabled"] is False
        and ops["credential_opened"] is False
        and ops["expiry_matches_r14"] is True
    )
    need(
        type(ops["entry_matches_installed_manifest"]) is bool
        and type(ops["config_pin_matches"]) is bool
    )
    pins = s["broker_pins"]
    keys(
        pins,
        {"release", "manifest_sha256", "unit_sha256", "release_matches_r14", "unit_matches_r14"},
    )
    need(
        type(pins["release"]) is str
        and re.fullmatch(r"releases/release-[a-f0-9]{64}", pins["release"])
    )
    digest(pins["manifest_sha256"])
    digest(pins["unit_sha256"])
    need(type(pins["release_matches_r14"]) is bool and type(pins["unit_matches_r14"]) is bool)
    need(
        s["probe_permission"] is False
        and s["resume_permission"] is False
        and s["query_only"] is True
    )
    for name in ("credential_reads", "provider_posts", "db_write"):
        need(type(s[name]) is int and s[name] == 0)
    return s


def envelope(request_id, status):
    need(type(request_id) is str and re.fullmatch(r"[a-f0-9]{32}", request_id))
    return {
        "status": status,
        "operation": "history_audit",
        "request_id": request_id,
        "service": "api-quota-broker.service",
        "automatic_retry": False,
    }


def passed(raw, request_id):
    s = {
        name: raw[name]
        for name in (
            "classification",
            "blocked_providers",
            "not_history_quarantined_providers",
            "table_counts",
            "provider_attempt_counts",
            "provider_dispatch_counts",
            "provider_usage",
            "unsettled_shared_scope_count",
            "old_files",
            "old_bootstrap",
            "broker_pins",
        )
    }
    s.update(
        intent_providers=raw["old_intents_without_replay"],
        intents_without_ledger=raw["old_intents_without_ledger"],
        config={
            "sha256": raw["config_sha256"],
            "target_count": raw["config_targets"],
            "providers": raw["config_providers"],
        },
        ops_pins=raw["r14_pins"],
        probe_permission=False,
        resume_permission=False,
        query_only=True,
        credential_reads=0,
        provider_posts=0,
        db_write=0,
    )
    return validate({**envelope(request_id, "passed"), "summary": s}, request_id)


def blocked(code, check, request_id):
    code = SOURCE_CODES.get(code, code)
    need(code in CODES and check in CHECKS)
    return validate(
        {
            **envelope(request_id, "blocked"),
            "code": code,
            "check": check,
            "private_values_exposed": False,
            "provider_posts": 0,
            "db_write": 0,
        },
        request_id,
    )


def validate(value, request_id):
    need(type(value) is dict and value.get("status") in {"passed", "blocked"})
    need(
        {k: value.get(k) for k in envelope(request_id, value["status"])}
        == envelope(request_id, value["status"])
    )
    if value["status"] == "passed":
        keys(value, {*envelope(request_id, "passed"), "summary"})
        validate_summary(value["summary"])
    else:
        keys(
            value,
            {
                *envelope(request_id, "blocked"),
                "code",
                "check",
                "private_values_exposed",
                "provider_posts",
                "db_write",
            },
        )
        need(
            type(value["code"]) is str
            and value["code"] in CODES
            and value["check"] in CHECKS
            and value["private_values_exposed"] is False
            and type(value["provider_posts"]) is int
            and value["provider_posts"] == 0
            and type(value["db_write"]) is int
            and value["db_write"] == 0
        )
    need(len(json.dumps(value, separators=(",", ":")).encode()) <= LIMIT)
    return value
