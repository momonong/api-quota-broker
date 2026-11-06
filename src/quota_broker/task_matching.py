"""Deterministic capability matching, followed by verified free-route ranking.

Catalog entries are discovery metadata, never execution or account authority.
Only a successful Dashboard dry-run for the SAME task can make a row safe.
"""

import math
import re
from datetime import datetime
from itertools import groupby
from typing import Any
from urllib.parse import urlsplit


def validate_match(raw: dict[str, Any]) -> None:
    fields = {"mode", "task", "inputs", "outputs", "features", "provider", "model", "route_task"}
    if set(raw) - fields or raw.get("mode") != "match":
        raise ValueError("invalid_task")
    if not isinstance(raw.get("task"), str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,63}", raw["task"]
    ):
        raise ValueError("invalid_task")
    for field in ("inputs", "outputs", "features"):
        values = raw.get(field, [] if field == "features" else None)
        if (
            not isinstance(values, list)
            or len(values) > 16
            or (field != "features" and not values)
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", v)
                for v in values
            )
            or len(set(values)) != len(values)
        ):
            raise ValueError("invalid_task")
    for field in ("provider", "model"):
        if field in raw and (
            not isinstance(raw[field], str)
            or not re.fullmatch(r"[A-Za-z0-9@/:._-]{1,160}", raw[field])
        ):
            raise ValueError("invalid_task")
    if "route_task" in raw:
        task = raw["route_task"]
        # This form submits actual text only. Do not fabricate text for media work.
        if (
            not isinstance(task, dict)
            or task.get("capability") != raw["task"]
            or raw["task"] not in {"text_generation", "translation"}
            or raw["inputs"] != ["text"]
            or raw["outputs"] not in (["text"], ["json"])
            or not isinstance(task.get("input"), (str, dict))
        ):
            raise ValueError("invalid_task")
        content = task["input"]
        if isinstance(content, dict):
            messages = content.get("messages")
            if (
                set(content) != {"messages"}
                or not isinstance(messages, list)
                or not 1 <= len(messages) <= 32
                or any(
                    not isinstance(m, dict)
                    or set(m) != {"role", "content"}
                    or not isinstance(m.get("content"), str)
                    for m in messages
                )
            ):
                raise ValueError("invalid_task")
        features = task.get("requirements", {}).get("features", [])
        if "json_schema" in task.get("options", {}):
            raise ValueError("invalid_task")
        if not set(raw.get("features", [])) <= set(features):
            raise ValueError("invalid_task")
        if raw["outputs"] == ["json"] and (
            "json_output" not in features
            or task.get("options", {}).get("response_format") != "json_object"
        ):
            raise ValueError("invalid_task")
        if raw["outputs"] != ["json"] and task.get("options", {}).get("response_format") in {
            "json_object",
            "json_schema",
        }:
            raise ValueError("invalid_task")
        for field in ("provider", "model"):
            if field in raw and task.get(field) != raw[field]:
                raise ValueError("invalid_task")


def _number(value: Any) -> float | None:
    return (
        float(value)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0
        else None
    )


def _rank(row: dict[str, Any]) -> tuple[Any, ...]:
    ranking = row["ranking"]
    seconds = ranking["value_loss_seconds"]
    headroom = ranking["observed_headroom"]
    return (
        not row["capability_verified"],
        not row["safe"],
        not (row["safe"] or row["free_plan_verified"]),
        not (row["safe"] or row["free_plan_basis"] == "account_plan"),
        seconds is None,
        seconds or 0,
        headroom is None,
        -(headroom or 0),
        row["provider"],
        row["model"],
        row["target_id"] or "",
    )


def _current_free_plan(target: dict[str, Any], now: Any) -> bool:
    """Qualification is useful for choosing a model, but is never a balance."""
    try:
        if target.get("billing_state") == "enabled" or target.get("billing_enabled") is True:
            return False
        observed, expires, current = (
            datetime.fromisoformat(str(value))
            for value in (
                target.get("free_verified_at"),
                target.get("free_evidence_expires_at"),
                now,
            )
        )
        if target.get("credit_expires_at") is not None:
            credit_expiry = datetime.fromisoformat(str(target["credit_expires_at"]))
            if credit_expiry.tzinfo is None or credit_expiry <= current:
                return False
        return bool(
            target.get("free_eligible") is True
            and all(t.tzinfo is not None for t in (observed, expires, current))
            and observed <= current < expires
        )
    except (TypeError, ValueError, AttributeError):
        return False


def _documentation(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


def match_models(
    raw: dict[str, Any], snapshot: dict[str, Any], advice: dict[str, Any] | None
) -> dict[str, Any]:
    """No network, secrets, state mutation, provider-specific rules or paid fallback."""
    validate_match(raw)
    models: dict[tuple[str, str], dict[str, Any]] = {}
    for provider in (snapshot.get("provider_inventory") or {}).get("providers", []):
        for catalog in provider.get("catalogs", []):
            for model in catalog.get("models", []):
                key = (provider["id"], model["id"])
                entry = models.setdefault(
                    key,
                    {
                        "variants": [],
                        "excluded": False,
                        "basis": "provider_catalog",
                        "documentation_url": _documentation(model.get("source"))
                        or _documentation(catalog.get("source")),
                        "restricted": model.get("status") in {"restricted", "unknown"},
                    },
                )
                entry["excluded"] |= model.get("status") in {"retired", "deprecated", "self_hosted"}
                entry["restricted"] |= model.get("status") in {"restricted", "unknown"}
                entry["variants"].extend(model.get("variants", []))
    for model in [*snapshot.get("models", []), *snapshot.get("targets", [])]:
        key = (model["provider"], model["model"])
        if key in models and model.get("variants"):
            # Each registry variant is independent proof, never a union of features/modalities.
            # The catalog lifecycle exclusion continues to dominate all registry evidence.
            models[key]["variants"].extend(model["variants"])
        # A configured entry must not erase lifecycle or combine incompatible catalog variants.
        if key not in models:
            models[key] = {
                "excluded": False,
                "basis": "configured_registry",
                "variants": model.get("variants")
                or [
                    {
                        "task": capability,
                        "inputs": model.get("input_modalities", []),
                        "outputs": model.get("output_modalities", []),
                        "features": model.get("features", []),
                    }
                    for capability in model.get("capabilities", [])
                    if len(model.get("capabilities", [])) == 1
                ],
            }
    routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in (advice or {}).get("candidates", []):
        routes.setdefault((row["provider"], row["model"]), []).append(row)
    targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in snapshot.get("targets", []):
        targets.setdefault((row["provider"], row["model"]), []).append(row)
    candidates = []
    for (provider, model), entry in models.items():
        if (
            entry["excluded"]
            or (raw.get("provider", provider) != provider)
            or raw.get("model", model) != model
        ):
            continue
        output = set(raw["outputs"])
        formatted_text = (output - {"json"}) | {"text"}
        variants = [
            v
            for v in entry["variants"]
            if v.get("task") == raw["task"]
            and set(raw["inputs"]) <= set(v.get("inputs", []))
            and (
                output <= set(v.get("outputs", []))
                or (
                    "json" in output
                    and raw["task"] == "text_generation"
                    and formatted_text <= set(v.get("outputs", []))
                )
            )
        ]
        if not variants:
            continue
        features_verified = any(
            (
                set(raw.get("features", []))
                | ({"json_output"} if not output <= set(v.get("outputs", [])) else set())
            )
            <= set(v.get("features", []))
            for v in variants
        )
        configured = targets.get((provider, model), [])
        choices: list[dict[str, Any] | None] = list(routes.get((provider, model), [])) or [None]
        for route in choices:
            reasons = list(route["reasons"]) if route else ["task_cost_not_checked"]
            target = next(
                (t for t in configured if route and t["target_id"] == route["target_id"]), None
            )
            if not configured:
                reasons.append("route_not_configured")
            if not features_verified:
                reasons.append("features_unverified")
            if route and (route.get("safe") is not True or target is None):
                reasons.append("free_route_not_verified")
            if route is None and configured:
                reasons.extend(reason for t in configured for reason in t.get("reasons", []))
                reasons.append("quota_not_verified_for_task")
            ranking = (route or {}).get("ranking", {})
            basis = ranking.get("value_loss_basis")
            exact = basis in {"credit_expiry", "quota_reset"}
            free_plans = [
                t for t in configured if _current_free_plan(t, snapshot["source"].get("fetched_at"))
            ]
            plan_bases = [
                (t.get("reviewed_account") or {}).get("qualification_basis") for t in free_plans
            ]
            capability_blockers = [
                r
                for r in reasons
                if r
                in {
                    "features_unverified",
                    "features_mismatch",
                    "capability_mismatch",
                    "context_limit",
                    "output_limit",
                    "adapter_admission",
                    "provider_constraint",
                    "model_constraint",
                }
            ]
            candidates.append(
                {
                    "provider": provider,
                    "model": model,
                    "target_id": (route or {}).get("target_id"),
                    "suitability": "capability_match",
                    "capability_basis": entry["basis"],
                    "capability_verified": not capability_blockers,
                    "capability_blockers": capability_blockers,
                    "free_plan_verified": bool(free_plans),
                    "free_plan_basis": "account_plan"
                    if "account_plan" in plan_bases
                    else "public_conditions"
                    if "public_conditions" in plan_bases
                    else None,
                    "documentation_url": entry.get("documentation_url"),
                    "access_requires_confirmation": entry.get("restricted", False),
                    "execution_status": "verified_free_route"
                    if route and route.get("safe") is True and not reasons
                    else "reviewed_snapshot"
                    if snapshot["source"].get("kind") == "reviewed"
                    else "verification_required",
                    "safe": bool(route and route.get("safe") is True and not reasons),
                    "reasons": list(dict.fromkeys(reasons)),
                    # Do not copy provider/account totals to models or sum incompatible quota units.
                    "remaining": route.get("remaining") if route else None,
                    "ranking": {
                        "value_loss_seconds": _number(ranking.get("value_loss_seconds"))
                        if exact
                        else None,
                        "value_loss_basis": basis if exact else None,
                        "observed_headroom": _number(ranking.get("observed_headroom")),
                    },
                }
            )
    candidates.sort(key=_rank)
    selected = next((row for row in candidates if row["safe"]), None)
    # A capability shortlist remains useful without a prompt or a live Broker.
    # Keep it separate from selected_target/decision, which retain admission semantics.
    eligible_models = [
        r for r in candidates if r["capability_verified"] and not r["access_requires_confirmation"]
    ]
    shortlist: list[dict[str, Any]] = []
    # Variety breaks genuine evidence ties only; never replace a better free route
    # with an unknown provider just to make a visually diverse list.
    for _, group in groupby(eligible_models, key=lambda r: _rank(r)[:-3]):
        peers = list(group)
        seen: set[str] = set()
        first: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for row in peers:
            (rest if row["provider"] in seen else first).append(row)
            seen.add(row["provider"])
        for row in first + rest:
            if len(shortlist) < 5 and not any(
                (r["provider"], r["model"]) == (row["provider"], row["model"]) for r in shortlist
            ):
                shortlist.append(row)
    return {
        "execution_allowed": False,
        "paid_allowed": False,
        "mode": "match",
        "policy": "capability_then_verified_free_capacity_then_expiry_then_headroom",
        "source": advice["source"] if advice else snapshot["source"],
        "decision": "recommend" if selected else "ask_user",
        "selected_target": selected,
        "recommended_model": eligible_models[0] if eligible_models else None,
        "shortlist": shortlist,
        "capability_matches": sum(r["capability_verified"] for r in candidates),
        "candidates": candidates,
        "matched_models": len({(r["provider"], r["model"]) for r in candidates}),
        "reasons": ["current_authoritative_free_capacity"]
        if selected
        else ["no_confirmed_safe_target"],
        "quality_basis": "capabilities_only_not_quality_benchmark",
    }
