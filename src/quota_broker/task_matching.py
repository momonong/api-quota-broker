"""Deterministic capability matching, followed by verified free-route ranking.

Catalog entries are discovery metadata, never execution or account authority.
Only a successful Dashboard dry-run for the SAME task can make a row safe.
"""

import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
        not row.get("account_grant_verified", False),
        not row.get("configured_free_allocation", False),
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


def _account_grant_info(
    provider: str,
    model: str,
    accounts_by_provider: dict[str, dict[str, Any]],
    now_str: Any,
) -> tuple[bool, dict[str, Any] | None]:
    acc = accounts_by_provider.get(provider)
    if not isinstance(acc, dict):
        return False, None
    if (
        acc.get("auto_recharge_enabled") is True
        or acc.get("balance_state") != "current"
        or (acc.get("evidence") or {}).get("current") is not True
        or acc.get("qualification") not in {"free_trial", "linked_chatgpt_subscription_benefit"}
    ):
        return False, None
    model_limit = next(
        (
            m
            for m in (acc.get("model_limits") or [])
            if isinstance(m, dict) and m.get("model") == model
        ),
        None,
    )
    active_grants = [
        g
        for g in (acc.get("grants") or [])
        if isinstance(g, dict) and g.get("state") == "available"
    ]
    if not active_grants:
        return False, None
    try:
        bal = Decimal(str(acc.get("current_balance") or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return False, None
    if not bal.is_finite() or bal <= 0:
        return False, None
    if isinstance(now_str, str) and len(now_str) >= 10:
        today_str = now_str[:10]
        for g in active_grants:
            exp = (g.get("expires") or {}).get("value")
            if isinstance(exp, str) and len(exp) == 10 and exp < today_str:
                return False, None
    return True, {"account": acc, "model_limit": model_limit, "grant": active_grants[0]}


def _build_quota_summary(
    provider: str,
    model: str,
    configured: list[dict[str, Any]],
    grant_info: dict[str, Any] | None,
    provider_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    # Runtime observations are already reduced by account-wide reservations.
    # Account-page snapshots must never overwrite that executable allowance.
    current = [
        q
        for t in configured
        for q in (t.get("observations") or [])
        if q.get("current") is True
        and type(q.get("effective_remaining")) is int
        and q["effective_remaining"] >= 0
    ]
    if current:

        def amount(q: dict[str, Any]) -> str:
            value = q["effective_remaining"]
            if q.get("metric") == "credits_usd_micros":
                return f"{Decimal(value) / 1000000:f} USD"
            return f"{value} {q.get('metric')}"

        deadlines = sorted(
            str(value)
            for value in [
                *(t.get("credit_expires_at") for t in configured),
                *(q.get("reset_at") for q in current),
            ]
            if value
        )
        return {
            "state": "broker_conservative_remaining",
            "headline": "Broker 保守可用："
            + " · ".join(f"{amount(q)} / {q.get('window')}" for q in current),
            "limits_text": "上限與剩餘分開；本次成本由任務檢查計算",
            "reset_or_expiry": "已核對執行截止／重置：" + deadlines[0]
            if deadlines
            else "截止／重置未知；不自行展延證據",
            "sharing_scope": "已扣除同一配額池的 Broker 保留；仍須本次任務檢查",
        }
    if grant_info is not None:
        acc = grant_info["account"]
        lim = grant_info["model_limit"]
        grant = grant_info["grant"]
        exp = (grant.get("expires") or {}).get("value") or "未知"
        bal = acc.get("current_balance") or acc.get("balance") or "未知"
        curr = bool((acc.get("evidence") or {}).get("current"))
        parts = []
        if isinstance(lim, dict):
            if lim.get("requests_per_minute"):
                parts.append(f"{lim['requests_per_minute']} RPM")
            if lim.get("tokens_per_minute"):
                parts.append(f"{lim['tokens_per_minute']} TPM")
            if lim.get("images_per_minute"):
                parts.append(f"{lim['images_per_minute']} IPM")
            if lim.get("batch_queue_tokens_per_day"):
                parts.append(f"Batch {lim['batch_queue_tokens_per_day']} TPD")
        return {
            "state": "current_account_observation" if curr else "last_observed",
            "headline": f"帳戶最後觀測餘額 {bal} {acc.get('currency', 'USD')}（到期日 {exp}）",
            "limits_text": " · ".join(parts) if parts else "此模型個別速率上限待核對",
            "reset_or_expiry": f"贈送額度 {exp} 到期（僅提供日期，不展延）",
            "sharing_scope": f"帳戶級共享贈送 Credits（所有可用模型共用同一筆 {bal} {acc.get('currency', 'USD')} grant）",
        }
    if configured:
        t = configured[0]
        rev = t.get("reviewed_account") or {}
        p_limits = t.get("provider_limits") or []
        lim_parts = [
            f"{pl.get('value')} {pl.get('metric')}/{pl.get('window')}"
            for pl in p_limits
            if pl.get("value") is not None
        ]
        if provider == "openrouter" and isinstance(provider_metadata, dict):
            or_meta = provider_metadata.get("openrouter") or {}
            daily = or_meta.get("daily") or or_meta.get("last_observed_daily")
            if isinstance(daily, dict):
                is_curr = or_meta.get("state") == "current"
                return {
                    "state": "current_remaining" if is_curr else "last_observed",
                    "headline": f"免費日額度{'剩餘' if is_curr else '最後觀測剩餘'} {daily.get('remaining')} / {daily.get('limit')} requests（已用 {daily.get('used')}）",
                    "limits_text": " · ".join(lim_parts) or "模型速率上限待核對",
                    "reset_or_expiry": f"每日 UTC 00:00 重置（下次重置 {or_meta.get('reset_at') or '00:00 UTC'}）",
                    "sharing_scope": "金鑰級共享免費日計數；模型適用資格仍須檢查",
                }
        derived = rev.get("derived_remaining")
        if isinstance(derived, dict):
            obs = (t.get("observations") or [{}])[0]
            rem_val = obs.get("remaining")
            if rem_val is None:
                rem_val = derived.get("remaining")
            return {
                "state": "current_remaining" if obs.get("current") else "last_observed",
                "headline": (
                    f"每日免費額度推算剩餘 {rem_val} / {derived.get('limit')} {derived.get('metric')}（已用 {derived.get('used')}）"
                    if rem_val is not None
                    else "每日免費額度剩餘未知；上限不代表剩餘"
                ),
                "limits_text": " · ".join(lim_parts) or "模型上限待核對",
                "reset_or_expiry": "每日 UTC 00:00 重置",
                "sharing_scope": "帳戶級共享每日免費 Neurons（所有 Workers AI 模型共用）",
            }
        raw_facts = rev.get("display_facts") or []
        facts: dict[str, Any] = {}
        if isinstance(raw_facts, dict):
            facts = dict(raw_facts)
        elif isinstance(raw_facts, list):
            for f in raw_facts:
                if isinstance(f, dict) and isinstance(f.get("kind"), str):
                    facts[f["kind"]] = f.get("value")
        if "monthly_allowance_usd" in facts:
            used_val = facts.get("used_allowance_usd", facts.get("monthly_used_usd", "未知"))
            return {
                "state": "verified_limit",
                "headline": f"免費月度額度 {facts.get('monthly_allowance_usd')} USD（最後核對已用 {used_val} USD）",
                "limits_text": " · ".join(lim_parts),
                "reset_or_expiry": "月度額度；精確重置規則須核對帳戶證據",
                "sharing_scope": "Workspace 共享月度額度；個別模型上限另列",
            }
        if lim_parts:
            resets = [
                pl.get("reset")
                for pl in p_limits
                if pl.get("reset") and pl.get("reset") not in {"unknown", "unspecified"}
            ]
            return {
                "state": "verified_limit",
                "headline": f"已核對免費上限：{' · '.join(lim_parts[:3])}",
                "limits_text": " · ".join(lim_parts),
                "reset_or_expiry": f"重置規則：{', '.join(dict.fromkeys(resets))}"
                if resets
                else "滾動視窗／日額度重置",
                "sharing_scope": "專案／組織模型上限（即時剩餘未由唯讀 API 提供）",
            }
    return {
        "state": "catalog_unverified",
        "headline": "僅型錄列出（免費額度與模型專屬上限未綁定）",
        "limits_text": "未核對",
        "reset_or_expiry": "未知",
        "sharing_scope": "不可直接繼承帳戶或其他模型額度",
    }


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
    accounts_by_provider = {
        a["provider"]: a
        for a in (snapshot.get("accounts") or [])
        if isinstance(a, dict) and isinstance(a.get("provider"), str)
    }
    provider_metadata = snapshot.get("provider_metadata")
    now_str = (snapshot.get("source") or {}).get("fetched_at")
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
        has_grant, grant_info = _account_grant_info(provider, model, accounts_by_provider, now_str)
        has_free_alloc = bool(
            configured
            and any(
                t.get("free_eligible") is True and bool(t.get("provider_limits"))
                for t in configured
            )
        )
        quota_summary = _build_quota_summary(
            provider, model, configured, grant_info, provider_metadata
        )
        access_stage = (
            "broker_configured_verified"
            if configured and any(t.get("free_eligible") is True for t in configured)
            else "account_grant_verified"
            if has_grant
            else "broker_configured_cap_only"
            if configured
            else "catalog_only"
        )
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
                    "free_plan_verified": bool(free_plans) or has_grant,
                    "free_plan_basis": "account_plan"
                    if "account_plan" in plan_bases
                    else "public_conditions"
                    if "public_conditions" in plan_bases
                    else "credit_grant"
                    if has_grant
                    else None,
                    "account_grant_verified": has_grant,
                    "configured_free_allocation": has_free_alloc,
                    "access_stage": access_stage,
                    "quota_summary": quota_summary,
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
