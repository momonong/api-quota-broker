"""Bounded private credit observations, independent of executable model targets."""

import copy
import json
import os
import re
import stat
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ConfigError
from .gateway_server import _json_depth

SOURCE_PATHS = {
    "platform.openai.com": {
        "/settings/organization/billing/overview",
        "/settings/organization/billing/credit-grants",
        "/settings/organization/billing/payment-methods",
        "/settings/organization/limits",
        "/usage",
        "/settings/organization/usage",
    },
    "developers.openai.com": {"/api/docs/guides/chatgpt-api-credits"},
}


def _object(value: Any, fields: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _instant(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result.astimezone(UTC)


def _date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError
    date.fromisoformat(value)
    return value


def _moment(value: Any) -> None:
    if value is None:
        return
    row = _object(value, "value precision")
    if row["precision"] == "date":
        _date(row["value"])
    elif row["precision"] == "instant":
        _instant(row["value"])
    else:
        raise ValueError


def _money(value: Any) -> None:
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,11})(?:\.\d{1,6})?", value)
    ):
        raise ValueError


def _list(value: Any, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    return value


def _note(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(c) < 32 for c in value)
        or re.search(
            r"(?i)bearer|api[_ -]?key|sk[-_]|token[=:]|password|secret[=:]|@|<|>|[A-Za-z0-9_/-]{48,}",
            value,
        )
    ):
        raise ValueError


def _account(value: Any) -> dict[str, Any]:
    row = _object(
        value,
        "provider qualification billing currency balance grants benefit model_limits evidence usage_page notes",
    )
    if (
        row["provider"] != "openai"
        or row["currency"] != "USD"
        or row["qualification"]
        not in {"linked_chatgpt_subscription_benefit", "free_trial", "unverified"}
        or row["billing"] not in {"not_started_no_payment_method", "unknown"}
    ):
        raise ValueError
    _money(row["balance"])
    for grant in _list(row["grants"], 32):
        grant = _object(grant, "amount remaining received expires state")
        _money(grant["amount"])
        _money(grant["remaining"])
        if (
            grant["amount"] is not None
            and grant["remaining"] is not None
            and Decimal(grant["remaining"]) > Decimal(grant["amount"])
        ):
            raise ValueError
        _moment(grant["received"])
        _moment(grant["expires"])
        if grant["state"] not in {"available", "expired", "unknown"}:
            raise ValueError
    benefit = _object(row["benefit"], "recurrence renewal_amount renews_at rollover")
    if benefit["recurrence"] not in {"conditional_on_eligible_subscription_renewal", "unknown"} or (
        benefit["rollover"] is not None and benefit["rollover"] is not False
    ):
        raise ValueError
    _money(benefit["renewal_amount"])
    if benefit["renews_at"] is not None:
        _instant(benefit["renews_at"])
    seen = set()
    for limit in _list(row["model_limits"], 32):
        limit = _object(
            limit,
            "model requests_per_minute tokens_per_minute images_per_minute batch_queue_tokens_per_day",
        )
        if (
            not isinstance(limit["model"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", limit["model"])
            or limit["model"] in seen
        ):
            raise ValueError
        _note(limit["model"])
        seen.add(limit["model"])
        for key in set(limit) - {"model"}:
            if limit[key] is not None and (
                type(limit[key]) is not int or not 0 <= limit[key] <= 10**15
            ):
                raise ValueError
    evidence = _object(row["evidence"], "provenance as_of valid_until time_precision sources")
    anchor, until = _instant(evidence["as_of"]), _instant(evidence["valid_until"])
    if (
        evidence["provenance"] != "observed"
        or evidence["time_precision"]
        not in {
            "account_pages_checked_during_same_audit_session",
            "owner_report_received_during_same_audit_session",
        }
        or not anchor < until <= anchor + timedelta(hours=24)
    ):
        raise ValueError
    for source in _list(evidence["sources"], 8):
        if not isinstance(source, str) or len(source) > 200:
            raise ValueError
        parsed = urlsplit(source)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in SOURCE_PATHS
            or parsed.path not in SOURCE_PATHS[parsed.hostname]
            or parsed.netloc != parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    usage = row["usage_page"]
    if usage is not None:
        usage = _object(usage, "state from_date through_date scope")
        if (
            usage["state"] not in {"no_data", "unknown"}
            or usage["scope"] != "all_api_sources_projects_keys"
        ):
            raise ValueError
        if _date(usage["from_date"]) > _date(usage["through_date"]):
            raise ValueError
    for note in _list(row["notes"], 16):
        _note(note)
    return copy.deepcopy(row)


class CreditAccounts:
    def __init__(self, path: str | Path) -> None:
        try:
            path = Path(path)
            info = path.parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ValueError
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError
            _json_depth(raw, maximum=16)
            doc = _object(json.loads(raw, object_pairs_hook=_unique), "schema_version accounts")
            if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
                raise ValueError
            self.rows = [_account(row) for row in _list(doc["accounts"], 8)]
            if len({row["provider"] for row in self.rows}) != len(self.rows):
                raise ValueError
        except Exception:  # noqa: BLE001 - never reflect private input
            raise ConfigError("invalid credit accounts") from None

    def snapshot(self, now: datetime) -> list[dict[str, Any]]:
        rows = copy.deepcopy(self.rows)
        for row in rows:
            evidence = row["evidence"]
            evidence["current"] = (
                _instant(evidence["as_of"]) <= now < _instant(evidence["valid_until"])
            )
            evidence["kind"] = (
                "owner_reported_credit_account"
                if evidence["time_precision"] == "owner_report_received_during_same_audit_session"
                else "manually_reviewed_credit_account"
            )
            row["balance_state"] = "current" if evidence["current"] else "stale"
            for grant in row["grants"]:
                expires = grant["expires"]
                if (
                    not expires
                    or grant["state"] != "available"
                    or grant["remaining"] is None
                    or Decimal(grant["remaining"]) == 0
                ):
                    continue
                observed = _instant(evidence["as_of"])
                if (
                    expires["precision"] == "instant"
                    and observed < _instant(expires["value"]) <= now
                ):
                    row["balance_state"] = "grant_expired"
                    break
                if expires["precision"] == "date":
                    # A date alone provides no exact cutoff. Stop presenting a
                    # current balance at the earliest possible timezone boundary.
                    earliest = datetime.fromisoformat(expires["value"]).replace(
                        tzinfo=UTC
                    ) - timedelta(hours=14)
                    latest = earliest + timedelta(hours=50)
                    if now >= earliest and observed < latest:
                        row["balance_state"] = "expiry_time_unknown"
            row["current_balance"] = row["balance"] if row["balance_state"] == "current" else None
            row["execution_allowed"] = row["paid_allowed"] = False
        return rows
