"""Prepare or explicitly execute the fixed metadata GET batch.

Default mode is offline. --execute requires separate human authorization before
use; preparing this runner does not grant it. No provider inference is performed.
Raw replies, page tokens, account identifiers and credentials stay in memory.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NoReturn
from urllib.parse import urlencode

import provider_metadata_plan as plan

from quota_broker.bounded_curl import _quote, metrics_template, parse_curl_result, run_curl
from quota_broker.discovery import DiscoveryError
from quota_broker.discovery_parsers import parse_models
from quota_broker.discovery_sources import MAX_SNAPSHOT_BYTES, validate_snapshot
from quota_broker.gateway_providers import ProviderError

Resolver = Callable[[str], str]
CredentialFactory = Callable[[], tuple[Resolver, float]]
Getter = Callable[[str, dict[str, str], float], bytes]
SAFE_DIAGNOSTICS = {
    "parse_models": frozenset(
        {
            "schema",
            "timestamp",
            "model_id",
            "model_id_type",
            "model_id_empty",
            "model_id_length",
            "model_id_characters",
            "model_id_url",
            "model_id_secret_pattern",
            "model_id_provider_format",
            "context_bound",
            "collection_schema",
            "item_schema",
            "options",
            "duplicate_model",
            "row_count",
        }
    ),
    "validate_evidence": frozenset(
        {
            "model_id_type",
            "model_id_empty",
            "model_id_length",
            "model_id_characters",
            "model_id_url",
            "model_id_secret_pattern",
        }
    ),
    "metadata_plan": frozenset(
        {
            "options",
            "fixture_path",
            "fixture_size",
            "fixture_schema",
            "secret_pattern",
            "timestamp",
            "key_schema",
            "quota_counter",
            "credits",
        }
    ),
}


class FetchError(DiscoveryError):
    def __init__(self, phase: str, reason: str):
        super().__init__("invalid_request", "metadata batch stopped")
        self.phase = (
            phase
            if phase in {"credentials", "budget", "transport", "normalize", "options"}
            else "normalize"
        )
        self.reason = (
            reason
            if reason
            in {
                "unavailable",
                "expiry",
                "deadline",
                "request_limit",
                "response",
                "http_status",
                "schema",
                "pagination",
                "duplicate",
                "proxy_environment",
                "options",
                "reflection",
            }
            else "schema"
        )


def safe_diagnostic(exc: Exception, fallback_phase: str) -> tuple[str, str]:
    """Only fixed, locally defined diagnostic tokens can reach stdout."""
    if isinstance(exc, FetchError):
        return exc.phase, exc.reason
    if isinstance(exc, DiscoveryError):
        phase = getattr(exc, "phase", None)
        reason = getattr(exc, "reason", None)
        if (
            isinstance(phase, str)
            and isinstance(reason, str)
            and reason in SAFE_DIAGNOSTICS.get(phase, frozenset())
        ):
            return phase, reason
    return fallback_phase, "schema" if fallback_phase == "normalize" else "unavailable"


def reject_reflection(value: Any, cache: dict[str, str]) -> None:
    """Inspect safe output semantically and as JSON, never include a match."""
    secrets = tuple(cache.values())
    pending = [value]
    while pending:
        part = pending.pop()
        if isinstance(part, str):
            if any(secret in part for secret in secrets):
                raise FetchError("normalize", "reflection")
        elif isinstance(part, dict):
            pending.extend(part.keys())
            pending.extend(part.values())
        elif isinstance(part, (list, tuple)):
            pending.extend(part)
    for ensure_ascii in (True, False):
        serialized = json.dumps(value, ensure_ascii=ensure_ascii, allow_nan=False)
        if any(
            secret in serialized
            or json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1] in serialized
            for secret in secrets
        ):
            raise FetchError("normalize", "reflection")


def parse_reply(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT_BYTES:
        raise FetchError("normalize", "response")
    try:
        text = raw.decode("utf-8")
        if plan.SECRET_PATTERN.search(text):
            raise ValueError
        result = json.loads(
            text, object_pairs_hook=plan._pairs, parse_constant=plan._invalid_constant
        )
        if not isinstance(result, dict) or plan.SECRET_PATTERN.search(json.dumps(result)):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise FetchError("normalize", "schema") from None


def credential_factory() -> tuple[Resolver, float]:
    # Exact existing readonly config-token mechanism; no login, renewal, key file,
    # cache file, secret environment variable or persisted subprocess output.
    from verify_doppler_executor_read import cli, create_service_token

    from quota_broker.nvidia import doppler_resolver_from_token

    reject_proxy_environment()
    binary = cli()
    issued_before = time.monotonic()  # Conservative: creation latency counts too.
    token = create_service_token(binary)
    return doppler_resolver_from_token(token, "api-quota-broker", "dev"), issued_before


def reject_proxy_environment() -> None:
    # Default curl must not inherit a user-selected proxy. Do not output values.
    if any(name.lower() in {"http_proxy", "https_proxy", "all_proxy"} for name in os.environ):
        raise FetchError("transport", "proxy_environment")


def metadata_get(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Private transport; only run_batch constructs URLs from the fixed plan."""
    reject_proxy_environment()
    expected_header = None
    matched = False
    for provider in plan.build_plan()["providers"]:
        for request in provider["requests"]:
            pattern = re.escape(request["url"])
            if provider["provider"] == "google":
                pattern += r"(?:&pageToken=[A-Za-z0-9%_.~+-]{1,12288})?"
            elif provider["provider"] == "cloudflare":
                pattern = pattern.replace(re.escape("{account_id}"), r"[a-fA-F0-9]{32}").replace(
                    "page=1", "page=[1-5]"
                )
            if re.fullmatch(pattern, url):
                matched = True
                auth = request["authentication"]
                expected_header = auth["header"] if auth else None
    if (
        not matched
        or not 0 < timeout <= 20
        or set(headers) != ({expected_header} if expected_header else set())
    ):
        raise FetchError("transport", "options")
    options = [
        "url = " + _quote(url),
        'request = "GET"',
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
    options.extend("header = " + _quote(name + ": " + value) for name, value in headers.items())
    try:
        code, output = run_curl(
            ("\n".join(options) + "\n").encode(),
            timeout,
            output_bound=MAX_SNAPSHOT_BYTES + 16_384,
            deadline_grace=0,
        )
        status, _, body, diagnostic = parse_curl_result(
            code,
            output,
            timeout,
            response_bound=MAX_SNAPSHOT_BYTES,
        )
        if diagnostic["transport_code"] != "ok":
            raise FetchError("transport", "response")
        if status != 200:
            raise FetchError("transport", "http_status")
        return body
    except FetchError:
        raise
    except (ProviderError, OSError, ValueError, subprocess.SubprocessError):
        raise FetchError("transport", "response") from None


def run_batch(
    *,
    credentials: CredentialFactory,
    get: Getter,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Serial bounded execution; dependency injection is for offline fixtures."""
    contract = plan.build_plan()
    result: dict[str, Any] = {
        "schema_version": 1,
        "state": "completed",
        "request_count": 0,
        "snapshots": [],
        "account_metadata": [],
        "summaries": [],
    }
    phase = "credentials"
    try:
        resolver, issued_before = credentials()
        cache: dict[str, str] = {}
        for provider in contract["providers"]:
            for request in provider["requests"]:
                auth = request["authentication"]
                refs = ([auth["credential_ref"]] if auth else []) + (
                    [request["account_id_ref"]] if "account_id_ref" in request else []
                )
                for name in refs:
                    if name not in cache:
                        if monotonic() >= issued_before + 270:
                            raise FetchError("credentials", "expiry")
                        value = resolver(name)
                        pattern = (
                            r"[a-fA-F0-9]{32}"
                            if name == "CLOUDFLARE_ACCOUNT_ID"
                            else r"[A-Za-z0-9._~-]{8,256}"
                        )
                        if not isinstance(value, str) or not re.fullmatch(pattern, value):
                            raise FetchError("credentials", "unavailable")
                        cache[name] = value
        started = monotonic()
        if issued_before + 300 - started < 270:
            raise FetchError("credentials", "expiry")
        deadline = min(started + contract["max_wall_seconds"], issued_before + 270)
        for provider in contract["providers"]:
            name = provider["provider"]
            if not provider["requests"]:  # OCR.space: documented candidate, no GET.
                documented = validate_snapshot(parse_models(name, {}, clock()), clock())
                reject_reflection(documented, cache)
                result["snapshots"].append(documented)
                result["summaries"].append(
                    {"provider": name, "requests": 0, "complete": documented["complete"]}
                )
                continue
            for request in provider["requests"]:
                rows: dict[tuple[str, str], dict[str, Any]] = {}
                seen_tokens: set[str] = set()
                token: str | None = None
                cf_total: tuple[int, int] | None = None
                item_count = 0
                complete = False
                snapshot: dict[str, Any] | None = None
                pages = 0
                for page in range(1, request["max_pages"] + 1):
                    phase = "budget"
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise FetchError("budget", "deadline")
                    if result["request_count"] >= contract["max_requests"]:
                        raise FetchError("budget", "request_limit")
                    url = request["url"]
                    if name == "google" and token is not None:
                        url += "&" + urlencode({"pageToken": token})
                    elif name == "cloudflare":
                        url = url.replace("{account_id}", cache["CLOUDFLARE_ACCOUNT_ID"])
                        url = url.replace("page=1&", f"page={page}&")
                    auth = request["authentication"]
                    headers = (
                        {}
                        if auth is None
                        else {
                            auth["header"]: ("Bearer " if auth["scheme"] == "Bearer" else "")
                            + cache[auth["credential_ref"]]
                        }
                    )
                    phase = "transport"
                    result["request_count"] += 1
                    raw = get(url, headers, min(20, remaining))
                    if monotonic() > deadline:
                        raise FetchError("budget", "deadline")
                    phase = "normalize"
                    data = parse_reply(raw)
                    checked_at = clock()
                    if request["purpose"] == "current_key_metadata":
                        account_metadata = plan.parse_openrouter_key(data, checked_at)
                        reject_reflection(account_metadata, cache)
                        result["account_metadata"].append(account_metadata)
                        break
                    snapshot = parse_models(name, data, checked_at, output_modalities="all")
                    for row in snapshot["models"]:
                        identity = (row["model"], row["capability"])
                        if identity in rows:
                            raise FetchError("normalize", "duplicate")
                        rows[identity] = row
                    # Enforce the production aggregate row/byte limits each page,
                    # before admitting further pagination into memory.
                    validate_snapshot(
                        {**snapshot, "models": list(rows.values()), "complete": False}, checked_at
                    )
                    pages += 1
                    if name == "google":
                        token = data.get("nextPageToken")
                        if token in (None, ""):
                            complete = bool(rows) and snapshot["complete"]
                            break
                        if (
                            not isinstance(token, str)
                            or len(token) > 4096
                            or not token.isascii()
                            or any(ord(c) < 32 or ord(c) == 127 for c in token)
                            or token in seen_tokens
                        ):
                            raise FetchError("normalize", "pagination")
                        seen_tokens.add(token)
                    elif name == "cloudflare":
                        info = data.get("result_info")
                        if not isinstance(info, dict) or any(
                            type(info.get(key)) is not int
                            for key in ("page", "total_pages", "total_count")
                        ):
                            raise FetchError("normalize", "pagination")
                        total = (info["total_pages"], info["total_count"])
                        if (
                            info["page"] != page
                            or not page <= total[0]
                            or total[1] < 0
                            or (cf_total is not None and cf_total != total)
                            or len(data["result"]) > 100
                        ):
                            raise FetchError("normalize", "pagination")
                        cf_total = total
                        item_count += len(data["result"])
                        if item_count > total[1]:
                            raise FetchError("normalize", "pagination")
                        if page == total[0]:
                            if item_count != total[1]:
                                raise FetchError("normalize", "pagination")
                            complete = bool(rows)
                            break
                    else:
                        complete = snapshot["complete"]
                        break
                if snapshot is not None:
                    snapshot["models"] = sorted(
                        rows.values(), key=lambda row: (row["model"], row["capability"])
                    )
                    snapshot["complete"] = complete
                    snapshot = validate_snapshot(snapshot, clock())
                    reject_reflection(snapshot, cache)
                    if monotonic() > deadline:
                        raise FetchError("budget", "deadline")
                    result["snapshots"].append(snapshot)
                    result["summaries"].append(
                        {
                            "provider": name,
                            "requests": pages,
                            "complete": complete,
                            "model_capability_records": len(rows),
                            "unique_models": len({identity[0] for identity in rows}),
                        }
                    )
        reject_reflection(result, cache)
        return result
    except (
        ValueError,
        OSError,
        RuntimeError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        result["state"] = "stopped"
        result["phase"], result["reason"] = safe_diagnostic(exc, phase)
        if result["reason"] == "reflection":
            # Clear the output collection too: final-result checks include fixed
            # summaries, and failure must never echo the matching value.
            result["snapshots"] = []
            result["account_metadata"] = []
            result["summaries"] = []
        return result


class SafeParser(plan.SafeArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(json.dumps({"error": "invalid_request", "phase": "options", "reason": "options"}))
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Execute only after separate human authorization"
    )
    parser.add_argument("--normalize-stdin", choices=plan.PROVIDERS)
    parser.add_argument("--fixture-kind", choices=("models", "key"), default="models")
    args = parser.parse_args(argv)
    try:
        if (
            args.execute
            and args.normalize_stdin
            or args.fixture_kind != "models"
            and args.normalize_stdin != "openrouter"
        ):
            raise FetchError("options", "options")
        if args.normalize_stdin:
            raw = parse_reply(sys.stdin.buffer.read(MAX_SNAPSHOT_BYTES + 1))
            now = datetime.now(UTC)
            result = (
                plan.parse_openrouter_key(raw, now)
                if args.fixture_kind == "key"
                else validate_snapshot(
                    parse_models(args.normalize_stdin, raw, now, output_modalities="all"), now
                )
            )
        elif args.execute:
            result = run_batch(credentials=credential_factory, get=metadata_get)
        else:
            result = plan.build_plan()
    except (DiscoveryError, OSError, ValueError) as exc:
        phase, reason = safe_diagnostic(exc, "normalize")
        print(
            json.dumps(
                {
                    "error": "invalid_request",
                    "phase": phase,
                    "reason": reason,
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 2 if result.get("state") == "stopped" else 0


if __name__ == "__main__":
    raise SystemExit(main())
