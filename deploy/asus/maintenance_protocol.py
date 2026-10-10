"""Closed wire schema for the fixed Broker maintenance profile (no paths/argv)."""

import json
import re

OPERATIONS = frozenset({"preflight", "deploy", "restart", "rollback", "operation_status"})
STATES = frozenset({"pending", "running", "passed", "blocked", "unknown", "absent"})
CODES = frozenset(
    {
        "ok",
        "accepted",
        "receipt_absent",
        "interrupted",
        "busy",
        "request_conflict",
        "baseline_changed",
        "package_invalid",
        "queue_not_quiescent",
        "legacy_claim_present",
        "release_exists",
        "runtime_invalid",
        "backup_invalid",
        "native_failed",
        "scope_invalid",
        "expiry_invalid",
        "foreign_change",
        "rollback_unverified",
        "storage_bound",
        "internal_error",
    }
)


def require(ok, code="package_invalid"):
    if not ok:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False))


def request(value):
    require(type(value) is dict and set(value) == {"operation", "request_id"})
    require(type(value["operation"]) is str and value["operation"] in OPERATIONS)
    require(type(value["request_id"]) is str and re.fullmatch("[a-f0-9]{32}", value["request_id"]))
    return value


def receipt(req, state, code, *, restored=False, legacy_clear=False, queue_jobs=None):
    return validate(
        {
            "status": "passed",
            "operation": req["operation"],
            "request_id": req["request_id"],
            "service": "api-quota-broker.service",
            "maintenance_state": state,
            "code": code,
            "automatic_retry": False,
            "provider_posts": 0,
            "database_restored": False,
            "original_restored": restored,
            "legacy_claim_clear": legacy_clear,
            "queue_jobs": queue_jobs,
        },
        req,
    )


def validate(value, req):
    request(req)
    require(
        type(value) is dict
        and set(value)
        == {
            "status",
            "operation",
            "request_id",
            "service",
            "maintenance_state",
            "code",
            "automatic_retry",
            "provider_posts",
            "database_restored",
            "original_restored",
            "legacy_claim_clear",
            "queue_jobs",
        }
    )
    require(
        value["status"] == "passed"
        and value["operation"] == req["operation"]
        and value["request_id"] == req["request_id"]
        and value["service"] == "api-quota-broker.service"
    )
    require(value["maintenance_state"] in STATES and value["code"] in CODES)
    require(
        value["automatic_retry"] is False
        and value["database_restored"] is False
        and type(value["provider_posts"]) is int
        and value["provider_posts"] == 0
    )
    require(type(value["original_restored"]) is bool and type(value["legacy_claim_clear"]) is bool)
    require(
        value["queue_jobs"] is None
        or (type(value["queue_jobs"]) is int and 0 <= value["queue_jobs"] <= 1000000)
    )
    return value
