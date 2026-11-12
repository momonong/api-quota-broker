"""On-demand quota evidence pipeline. No inference or billing mutation.

Use plan offline; run reads a fixed existing Doppler reference in memory and one
allowlisted OpenRouter GET. Official console imports use dashboard_refresh.py.
Every provider is accounted for; API limitations are never disguised as zeros.
"""

import argparse
import base64
import copy
import json
import os
import re
import resource
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dashboard_refresh import _accounts, _new_file, _read, _sync_directory
from provider_metadata_fetch import metadata_get, parse_reply, reject_proxy_environment
from provider_metadata_plan import CF_URL, SOURCES, parse_openrouter_key

from quota_broker.bounded_curl import _quote, metrics_template, parse_curl_result, run_curl
from quota_broker.dashboard_provider_metadata import KEY_URL, REFRESH_SOURCES, validate_report
from quota_broker.discovery_parsers import parse_models
from quota_broker.discovery_sources import MAX_SNAPSHOT_BYTES, validate_snapshot

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "accounts.json",
    "gateway.json",
    "provider-observations.json",
    "registry.json",
    "reviewed.json",
)
ALLOWED_CREDENTIALS = {
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "NVIDIA_API_KEY",
    "OCRSPACE_API_KEY",
    "HUGGINGFACE_API_TOKEN",
    "CEREBRAS_API_KEY",
    "SAMBANOVA_API_KEY",
}
DEFAULT_REVIEWED_TARGETS = (
    ("google", "models/gemini-2.5-flash"),
    ("groq", "openai/gpt-oss-20b"),
    ("mistral", "ministral-3b-latest"),
    ("nvidia", "nvidia/riva-translate-4b-instruct-v2"),
    ("cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
    ("openrouter", "openrouter/free"),
    ("ocrspace", "ocr.space/engine2"),
)
PROBE_URLS = {
    "openai_models": "https://api.openai.com/v1/models",
    "openai_costs": "https://api.openai.com/v1/organization/costs?start_time=1762387200&limit=1",
    "huggingface_whoami": "https://huggingface.co/api/whoami-v2",
    "cloudflare_graphql": "https://api.cloudflare.com/client/v4/graphql",
}


def doppler_secret(name):
    reject_proxy_environment()
    if name not in ALLOWED_CREDENTIALS:
        raise ValueError("credential_not_allowed")
    binary = Path.home() / ".local/bin/doppler"
    command = [
        str(binary),
        "--no-read-env",
        "--no-verify-tls=false",
        "--no-check-version",
        "--attempts",
        "1",
        "--api-host",
        "https://api.doppler.com",
        "--scope",
        str(ROOT),
        "secrets",
        "get",
        name,
        "--plain",
        "--project",
        "api-quota-broker",
        "--config",
        "dev",
    ]
    result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("credential_unavailable")
    value = result.stdout.decode("utf-8").strip()
    pattern = r"[a-fA-F0-9]{32}" if name == "CLOUDFLARE_ACCOUNT_ID" else r"[A-Za-z0-9_.~-]{8,4096}"
    if not re.fullmatch(pattern, value):
        raise ValueError("credential_unavailable")
    return value


def probe_http(
    url: str,
    headers: dict[str, str],
    timeout: float = 15,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Read-only HTTP probe for allowlisted account/catalog verification URLs."""
    reject_proxy_environment()
    if (
        url not in set(PROBE_URLS.values())
        or method not in {"GET", "POST"}
        or not 0 < timeout <= 20
    ):
        raise ValueError("probe_options_not_allowed")
    options = [
        "url = " + _quote(url),
        f'request = "{method}"',
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        "connect-timeout = " + _quote(str(min(10, timeout))),
        "max-time = " + _quote(str(timeout)),
        "include",
        "suppress-connect-headers",
        'header = "Accept: application/json"',
        "write-out = " + _quote(metrics_template()),
    ]
    for k, v in headers.items():
        options.append("header = " + _quote(f"{k}: {v}"))
    if body is not None:
        options.append("data-binary = " + _quote(body.decode("utf-8")))
    code, output = run_curl(
        ("\n".join(options) + "\n").encode(),
        timeout,
        output_bound=MAX_SNAPSHOT_BYTES + 16_384,
        deadline_grace=0,
    )
    status, _, raw_body, diagnostic = parse_curl_result(
        code, output, timeout, response_bound=MAX_SNAPSHOT_BYTES
    )
    if diagnostic["transport_code"] != "ok":
        raise ValueError("probe_transport_failed")
    return status, raw_body


def _extract_openrouter_usage(parsed: dict, observation: dict) -> dict | None:
    raw_data = parsed.get("data")
    if not isinstance(raw_data, dict):
        return None
    credits_obs = observation.get("credits")
    if not isinstance(credits_obs, dict):
        return None
    usage_keys = (
        ("usage", "total_usd"),
        ("usage_daily", "daily_usd"),
        ("usage_weekly", "weekly_usd"),
        ("usage_monthly", "monthly_usd"),
        ("byok_usage", "byok_total_usd"),
        ("byok_usage_daily", "byok_daily_usd"),
        ("byok_usage_weekly", "byok_weekly_usd"),
        ("byok_usage_monthly", "byok_monthly_usd"),
    )
    if not any(src_key in raw_data for src_key, _ in usage_keys) and "is_free_tier" not in raw_data:
        return None
    usage = {dst_key: credits_obs.get(src_key) for src_key, dst_key in usage_keys}
    if type(raw_data.get("is_free_tier")) is bool:
        usage["is_free_tier"] = raw_data["is_free_tier"]
    return usage


def _update_model_entry(
    models_by_key: dict[tuple[str, str], dict],
    provider: str,
    target_model: str,
    snapshot: dict,
) -> int:
    rows = [r for r in snapshot["models"] if r["model"] == target_model]
    unique_count = len({r["model"] for r in snapshot["models"]})
    status = (
        "documented"
        if provider == "ocrspace" and rows
        else "inactive"
        if rows and all("inactive" in r["features"] for r in rows)
        else "listed"
        if rows
        else "not_listed"
        if snapshot["complete"]
        else "unknown"
    )
    models_by_key[(provider, target_model)] = {
        "provider": provider,
        "model": target_model,
        "source": snapshot["source"],
        "checked_at": snapshot["checked_at"],
        "status": status,
    }
    return unique_count


def _update_provider_models(
    models_by_key: dict[tuple[str, str], dict],
    provider: str,
    target_models: set[str],
    snapshot: dict,
) -> int:
    for m in sorted(target_models):
        _update_model_entry(models_by_key, provider, m, snapshot)
    return len({r["model"] for r in snapshot["models"]})


def _probe_other_providers(
    report: dict,
    statuses: dict,
    *,
    resolver,
    get,
    http_probe,
    clock,
    reviewed_targets: tuple[tuple[str, str], ...],
) -> None:
    target_sets: dict[str, set[str]] = {}
    for prov, mod in reviewed_targets:
        target_sets.setdefault(prov, set()).add(mod)
    for row in report.get("models", []):
        target_sets.setdefault(row["provider"], set()).add(row["model"])
    models_by_key = {
        (r["provider"], r["model"]): copy.deepcopy(r) for r in report.get("models", [])
    }

    # 1. OpenRouter model catalog verification
    if statuses["openrouter"].get("credential_state") == "present":
        try:
            url = SOURCES["openrouter"] + "?output_modalities=all"
            raw = get(url, {}, 20)
            checked = clock()
            snap = validate_snapshot(
                parse_models("openrouter", parse_reply(raw), checked, output_modalities="all"),
                checked,
            )
            count = _update_provider_models(
                models_by_key, "openrouter", target_sets.get("openrouter", set()), snap
            )
            statuses["openrouter"]["model_probe_status"] = "verified"
            statuses["openrouter"]["verified_models"] = count
        except Exception:  # noqa: BLE001
            statuses["openrouter"]["model_probe_status"] = "query_failed"

    # 2. Catalog-verifiable target providers: google, groq, mistral, nvidia
    for provider, cred_name, header_name, scheme, url_suffix in (
        ("google", "GEMINI_API_KEY", "x-goog-api-key", None, "?pageSize=1000"),
        ("groq", "GROQ_API_KEY", "Authorization", "Bearer", ""),
        ("mistral", "MISTRAL_API_KEY", "Authorization", "Bearer", ""),
        ("nvidia", "NVIDIA_API_KEY", None, None, ""),
    ):
        entry = statuses[provider]
        balance_default = (
            "permission_required"
            if REFRESH_SOURCES[provider][0] == "permission_required"
            else "unsupported_source"
            if provider == "groq"
            else "console_required"
        )
        try:
            key = resolver(cred_name)
            entry["credential_state"] = "present"
        except Exception:  # noqa: BLE001
            entry.update(
                queried=False,
                checked_at=clock().isoformat(),
                credential_state="missing",
                model_probe_status="credential_unavailable",
                balance_probe_status="credential_unavailable",
                status="credential_unavailable",
            )
            continue
        try:
            url = SOURCES[provider] + url_suffix
            headers = (
                {} if header_name is None else {header_name: (f"{scheme} {key}" if scheme else key)}
            )
            raw = get(url, headers, 20)
            checked = clock()
            snap = validate_snapshot(
                parse_models(provider, parse_reply(raw), checked, output_modalities="all"),
                checked,
            )
            count = _update_provider_models(
                models_by_key, provider, target_sets.get(provider, set()), snap
            )
            entry.update(
                queried=True,
                checked_at=checked.isoformat(),
                model_probe_status="verified",
                verified_models=count,
                balance_probe_status=balance_default,
            )
        except Exception:  # noqa: BLE001
            entry.update(
                queried=True,
                checked_at=clock().isoformat(),
                model_probe_status="query_failed",
                balance_probe_status=balance_default,
            )

    # 3. Cloudflare Workers AI: model catalog search + GraphQL Neurons analytics probe
    cf_entry = statuses["cloudflare"]
    try:
        cf_acc = resolver("CLOUDFLARE_ACCOUNT_ID")
        cf_tok = resolver("CLOUDFLARE_API_TOKEN")
        cf_entry["credential_state"] = "present"
    except Exception:  # noqa: BLE001
        cf_entry.update(
            queried=False,
            checked_at=clock().isoformat(),
            credential_state="missing",
            model_probe_status="credential_unavailable",
            balance_probe_status="credential_unavailable",
            status="credential_unavailable",
        )
    else:
        try:
            cf_url = (
                CF_URL.replace("{account_id}", cf_acc)
                + "?page=1&per_page=100&include_deprecated=true"
            )
            raw = get(cf_url, {"Authorization": f"Bearer {cf_tok}"}, 20)
            checked = clock()
            snap = validate_snapshot(
                parse_models("cloudflare", parse_reply(raw), checked, output_modalities="all"),
                checked,
            )
            count = _update_provider_models(
                models_by_key, "cloudflare", target_sets.get("cloudflare", set()), snap
            )
            cf_entry.update(
                queried=True,
                checked_at=checked.isoformat(),
                model_probe_status="verified",
                verified_models=count,
            )
        except Exception:  # noqa: BLE001
            cf_entry.update(
                queried=True,
                checked_at=clock().isoformat(),
                model_probe_status="query_failed",
            )
        try:
            today = clock().astimezone(UTC).strftime("%Y-%m-%dT00:00:00Z")
            gql_payload = json.dumps(
                {
                    "query": (
                        "query ($accountTag: string!, $start: Time!) {"
                        " viewer { accounts(filter: {accountTag: $accountTag}) {"
                        " aiInferenceAdaptiveGroups(limit: 1, filter: {datetime_geq: $start}) {"
                        " sum { totalNeurons } } } } }"
                    ),
                    "variables": {"accountTag": cf_acc, "start": today},
                }
            ).encode("utf-8")
            st_code, gql_raw = http_probe(
                PROBE_URLS["cloudflare_graphql"],
                {"Authorization": f"Bearer {cf_tok}", "Content-Type": "application/json"},
                15,
                method="POST",
                body=gql_payload,
            )
            gql_doc = parse_reply(gql_raw) if st_code == 200 else {}
            errors = gql_doc.get("errors")
            cf_entry["balance_probe_status"] = (
                "permission_required" if errors or st_code in {401, 403} else "console_required"
            )
        except Exception:  # noqa: BLE001
            cf_entry["balance_probe_status"] = "permission_required"

    # 4. OCR.space: credential check + documented engine catalog
    ocr_entry = statuses["ocrspace"]
    try:
        resolver("OCRSPACE_API_KEY")
        checked = clock()
        snap = validate_snapshot(parse_models("ocrspace", {}, checked), checked)
        count = _update_provider_models(
            models_by_key, "ocrspace", target_sets.get("ocrspace", set()), snap
        )
        ocr_entry.update(
            queried=False,
            checked_at=checked.isoformat(),
            credential_state="present",
            model_probe_status="documented",
            verified_models=count,
            balance_probe_status="unsupported_source",
            status="console_required",
        )
    except Exception:  # noqa: BLE001
        ocr_entry.update(
            queried=False,
            checked_at=clock().isoformat(),
            credential_state="missing",
            model_probe_status="credential_unavailable",
            balance_probe_status="credential_unavailable",
            status="credential_unavailable",
        )

    # 5. OpenAI: API key model access check (/v1/models) + billing permission check (/v1/organization/costs)
    oa_entry = statuses["openai"]
    try:
        oa_key = resolver("OPENAI_API_KEY")
        oa_entry["credential_state"] = "present"
    except Exception:  # noqa: BLE001
        oa_entry.update(
            queried=False,
            checked_at=clock().isoformat(),
            credential_state="missing",
            model_probe_status="credential_unavailable",
            balance_probe_status="credential_unavailable",
            status="console_required",
        )
    else:
        try:
            st_mod, raw_mod = http_probe(
                PROBE_URLS["openai_models"], {"Authorization": f"Bearer {oa_key}"}, 15
            )
            checked = clock()
            if st_mod == 200:
                parsed_mod = parse_reply(raw_mod)
                data_list = parsed_mod.get("data", [])
                count = len(
                    {
                        m.get("id")
                        for m in data_list
                        if isinstance(m, dict) and isinstance(m.get("id"), str)
                    }
                )
                oa_entry.update(
                    queried=True,
                    checked_at=checked.isoformat(),
                    model_probe_status="verified",
                    verified_models=count,
                )
            else:
                oa_entry.update(
                    queried=True,
                    checked_at=checked.isoformat(),
                    model_probe_status="query_failed",
                )
        except Exception:  # noqa: BLE001
            oa_entry.update(
                queried=True,
                checked_at=clock().isoformat(),
                model_probe_status="query_failed",
            )
        try:
            st_cost, _ = http_probe(
                PROBE_URLS["openai_costs"], {"Authorization": f"Bearer {oa_key}"}, 15
            )
            oa_entry["balance_probe_status"] = (
                "permission_required" if st_cost in {401, 403} else "console_required"
            )
        except Exception:  # noqa: BLE001
            oa_entry["balance_probe_status"] = "permission_required"

    # 6. Hugging Face: read-only account plan check (/api/whoami-v2)
    hf_entry = statuses["huggingface"]
    try:
        hf_tok = resolver("HUGGINGFACE_API_TOKEN")
        hf_entry["credential_state"] = "present"
    except Exception:  # noqa: BLE001
        hf_entry.update(
            queried=False,
            checked_at=clock().isoformat(),
            credential_state="missing",
            model_probe_status="credential_unavailable",
            balance_probe_status="credential_unavailable",
            status="credential_unavailable",
        )
    else:
        try:
            st_hf, raw_hf = http_probe(
                PROBE_URLS["huggingface_whoami"], {"Authorization": f"Bearer {hf_tok}"}, 15
            )
            checked = clock()
            if st_hf == 200:
                doc_hf = parse_reply(raw_hf)
                plan_name = (
                    "pro"
                    if doc_hf.get("isPro") is True
                    else "free"
                    if doc_hf.get("isPro") is False
                    else "unknown"
                )
                hf_entry.update(
                    queried=True,
                    checked_at=checked.isoformat(),
                    model_probe_status="not_applicable",
                    balance_probe_status="console_required",
                    account_plan=plan_name,
                    status="console_required",
                )
            else:
                hf_entry.update(
                    queried=True,
                    checked_at=checked.isoformat(),
                    model_probe_status="not_applicable",
                    balance_probe_status="query_failed",
                    status="console_required",
                )
        except Exception:  # noqa: BLE001
            hf_entry.update(
                queried=True,
                checked_at=clock().isoformat(),
                model_probe_status="not_applicable",
                balance_probe_status="query_failed",
                status="console_required",
            )

    # 7. Cerebras & SambaNova: check Doppler credential availability (keep disabled)
    for provider, cred_name in (
        ("cerebras", "CEREBRAS_API_KEY"),
        ("sambanova", "SAMBANOVA_API_KEY"),
    ):
        entry = statuses[provider]
        try:
            resolver(cred_name)
            entry.update(
                queried=False,
                checked_at=clock().isoformat(),
                credential_state="present",
                model_probe_status="not_applicable",
                balance_probe_status="console_required",
                status="console_required",
            )
        except Exception:  # noqa: BLE001
            entry.update(
                queried=False,
                checked_at=clock().isoformat(),
                credential_state="missing",
                model_probe_status="credential_unavailable",
                balance_probe_status="credential_unavailable",
                status="credential_unavailable",
            )

    report["models"] = [models_by_key[k] for k in sorted(models_by_key)]


def collect(
    base,
    *,
    resolver=doppler_secret,
    get=metadata_get,
    http_probe=probe_http,
    clock=lambda: datetime.now(UTC),
    probe_all: bool = False,
    reviewed_targets: tuple[tuple[str, str], ...] = DEFAULT_REVIEWED_TARGETS,
):
    started = clock()
    validate_report(json.dumps(base).encode(), started)
    report = copy.deepcopy(base)
    report["schema_version"] = 2
    statuses = {
        provider: {
            "status": "permission_required"
            if method == "permission_required"
            else "console_required"
        }
        for provider, (method, *_) in REFRESH_SOURCES.items()
    }
    report["refresh"] = {"attempted_at": started.isoformat(), "providers": statuses}
    try:
        key = resolver("OPENROUTER_API_KEY")
        if probe_all:
            statuses["openrouter"]["credential_state"] = "present"
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        statuses["openrouter"]["status"] = "credential_unavailable"
        if probe_all:
            statuses["openrouter"].update(
                queried=False,
                checked_at=clock().isoformat(),
                credential_state="missing",
                model_probe_status="credential_unavailable",
                balance_probe_status="credential_unavailable",
            )
            _probe_other_providers(
                report,
                statuses,
                resolver=resolver,
                get=get,
                http_probe=http_probe,
                clock=clock,
                reviewed_targets=reviewed_targets,
            )
        return report
    try:
        raw = get(KEY_URL, {"Authorization": "Bearer " + key}, 20)
        checked = clock()
        # Only normalized numeric fields leave memory. No raw JSON or errors logged.
        parsed = parse_reply(raw)
        observation = parse_openrouter_key(parsed, checked)
        daily = observation["free_model_daily_requests"]
        usage = _extract_openrouter_usage(parsed, observation)
        if started.astimezone(UTC).date() != checked.astimezone(UTC).date():
            daily = None
        # Unknown latest results suppress current availability in projection but
        # retain the last successful reading with its original time.
        if daily is not None or base["openrouter"] is None:
            or_entry = {
                "source": KEY_URL,
                "checked_at": checked.isoformat(),
                "daily": daily,
            }
            if usage is not None:
                or_entry["usage"] = usage
            report["openrouter"] = or_entry
        statuses["openrouter"]["status"] = "updated" if daily is not None else "counter_unavailable"
        if probe_all:
            statuses["openrouter"].update(
                queried=True,
                checked_at=checked.isoformat(),
                balance_probe_status=statuses["openrouter"]["status"],
            )
        validate_report(json.dumps(report).encode(), checked)
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        report["openrouter"] = copy.deepcopy(base["openrouter"])
        statuses["openrouter"]["status"] = "query_failed"
        if probe_all:
            statuses["openrouter"].update(
                queried=True,
                checked_at=clock().isoformat(),
                balance_probe_status="query_failed",
            )
    if probe_all:
        _probe_other_providers(
            report,
            statuses,
            resolver=resolver,
            get=get,
            http_probe=http_probe,
            clock=clock,
            reviewed_targets=reviewed_targets,
        )
        validate_report(json.dumps(report).encode(), clock())
    return report


def run(metadata, output, accounts_input=None):
    # Read all inputs before any external request. Never replace an old generation.
    inputs = {name: _read(metadata / name, 4 * 1024**2) for name in FILES}
    now = datetime.now(UTC)
    base = validate_report(inputs["provider-observations.json"], now)
    if accounts_input is not None:
        raw, imported, _ = _accounts(accounts_input, now)
        previous = json.loads(inputs["accounts.json"])
        if len(imported["accounts"]) != len(previous["accounts"]) or any(
            new["provider"] != old["provider"]
            or datetime.fromisoformat(new["evidence"]["as_of"])
            <= datetime.fromisoformat(old["evidence"]["as_of"])
            for new, old in zip(imported["accounts"], previous["accounts"], strict=True)
        ):
            raise ValueError("account_import_not_newer")
        inputs["accounts.json"] = raw
    if output.exists() or output.is_symlink():
        raise ValueError("output_already_exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Ensure the output ancestry has no symlinks, before creating the generation.
    for parent in output.absolute().parents:
        if parent.is_symlink():
            raise ValueError("unsafe_output")
    output.mkdir(mode=0o700)
    reviewed_doc = json.loads(inputs["reviewed.json"])
    reviewed_targets = tuple(
        (t["provider"], t["model"])
        for t in reviewed_doc.get("targets", [])
        if isinstance(t, dict) and t.get("provider") in SOURCES and isinstance(t.get("model"), str)
    )
    report = collect(
        base, probe_all=True, reviewed_targets=reviewed_targets or DEFAULT_REVIEWED_TARGETS
    )
    inputs["provider-observations.json"] = json.dumps(report, ensure_ascii=False, indent=2).encode()
    for name, raw in inputs.items():
        _new_file(output / name, raw)
    _sync_directory(output)
    return {
        "status": "completed_with_gaps",
        "model_calls": 0,
        "payments": 0,
        "providers": report["refresh"]["providers"],
        "attempted_at": report["refresh"]["attempted_at"],
    }


def data_package(base, metadata, operation):
    """Reuse the exact published app; only observation files may change."""
    import hp_quota_client as publisher

    proof = publisher.validate(base)
    originals = {
        item["path"]: base64.b64decode(item["content_base64"], validate=True)
        for item in base["files"]
    }
    reader = originals["app/quota_broker/dashboard_provider_metadata.py"]
    if b"REFRESH_SCHEMA_VERSION = 2" not in reader:
        raise ValueError("active_app_requires_reviewed_upgrade")
    now = datetime.now(UTC)
    replacements = {
        "metadata/" + name: _read(metadata / name, 4 * 1024**2)
        for name in FILES
        if name != "accounts.json"
    }
    accounts_raw, accounts, _ = _accounts(metadata / "accounts.json", now)
    replacements["metadata/accounts.json"] = accounts_raw
    old_report = validate_report(originals["metadata/provider-observations.json"], now)
    new_report = validate_report(replacements["metadata/provider-observations.json"], now)
    old_models = {(r["provider"], r["model"]): r for r in old_report["models"]}
    new_models = {(r["provider"], r["model"]): r for r in new_report["models"]}
    for key, old_row in old_models.items():
        new_row = new_models.get(key)
        if new_row is None:
            raise ValueError("model_evidence_regressed")
        old_time = datetime.fromisoformat(old_row["checked_at"])
        new_time = datetime.fromisoformat(new_row["checked_at"])
        if new_time < old_time or (new_time == old_time and new_row != old_row):
            raise ValueError("model_evidence_regressed")
    old_counter, new_counter = old_report["openrouter"], new_report["openrouter"]
    if old_counter is not None and (
        new_counter is None
        or datetime.fromisoformat(new_counter["checked_at"])
        < datetime.fromisoformat(old_counter["checked_at"])
        or (
            datetime.fromisoformat(new_counter["checked_at"])
            == datetime.fromisoformat(old_counter["checked_at"])
            and new_counter != old_counter
        )
    ):
        raise ValueError("counter_evidence_regressed")
    if old_report.get("refresh") and (
        not new_report.get("refresh")
        or (
            new_report["refresh"] != old_report["refresh"]
            and datetime.fromisoformat(new_report["refresh"]["attempted_at"])
            <= datetime.fromisoformat(old_report["refresh"]["attempted_at"])
        )
    ):
        raise ValueError("refresh_evidence_regressed")
    old_accounts = json.loads(originals["metadata/accounts.json"])
    if len(accounts["accounts"]) != len(old_accounts["accounts"]):
        raise ValueError("account_scope_changed")
    for new, old in zip(accounts["accounts"], old_accounts["accounts"], strict=True):
        if new["provider"] != old["provider"] or (
            new != old
            and datetime.fromisoformat(new["evidence"]["as_of"])
            <= datetime.fromisoformat(old["evidence"]["as_of"])
        ):
            raise ValueError("account_evidence_regressed")
    result = copy.deepcopy(base)
    result.update(
        operation_id=operation,
        release_id="quota-data-" + operation,
        expected_current=base["release_id"],
        expected_manifest_sha256=proof["manifest_sha256"],
    )
    for item in result["files"]:
        if not item["path"].startswith("metadata/"):
            continue
        raw = replacements[item["path"]]
        if (
            item["path"] not in {"metadata/accounts.json", "metadata/provider-observations.json"}
            and publisher.digest(raw) != item["sha256"]
        ):
            raise ValueError("qualification_or_configuration_changed")
        item.update(
            size=len(raw),
            sha256=publisher.digest(raw),
            content_base64=base64.b64encode(raw).decode(),
        )
    publisher.validate(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--accounts-input", type=Path, help="validated official console observation"
    )
    parser.add_argument("--base-package", type=Path, help="exact active release; app bytes reused")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="publish observations through existing HP maintenance",
    )
    args = parser.parse_args(argv)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "providers": {
                        p: {"method": v[0], "console": v[1], "documentation": v[2], "reason": v[3]}
                        for p, v in REFRESH_SOURCES.items()
                    },
                    "model_calls": 0,
                    "payments": 0,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not args.metadata or not args.output:
        parser.error("run requires --metadata and --output")
    if args.publish and not args.base_package:
        parser.error("--publish requires --base-package")
    operation = None
    try:
        base = None
        if args.base_package:
            import hp_quota_client as publisher

            base = publisher.parse(
                publisher.read_file(args.base_package, publisher.MAX_JSON, private=True)
            )
            publisher.validate(base)
        result = run(args.metadata, args.output, args.accounts_input)
        if base is not None:
            operation = str(uuid.uuid4())
            payload = data_package(base, args.output, operation)
            publisher.write_once(args.output / "release.json", payload)
            result["publication"] = (
                publisher.publish(payload)
                if args.publish
                else {"status": "prepared_only", "operation_id": operation}
            )
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "reason": "inspect_output_and_original_receipt_do_not_replay"
                    if operation
                    else "input_or_output_invalid",
                    "operation_id": operation,
                    "model_calls": 0,
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
