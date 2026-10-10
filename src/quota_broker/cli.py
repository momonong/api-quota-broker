"""Local server and an entirely offline direct-call demonstration."""

import argparse
import base64
import binascii
import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from . import __version__
from .client import ClientError, DirectClient, _json_http
from .client_credentials import ClientCredentialError, read_runtime_client
from .config import Quota, Target, load_config, load_gateway_config, load_secret_inventory
from .core import Broker, utcnow
from .discovery import DiscoveryError, DiscoveryStore
from .families import FamilyError, FamilyRegistry, MediaLimits
from .gateway import Gateway
from .gateway_server import make_gateway_server
from .key_admin import DopplerCLIWriter, MetadataStore, make_key_admin_server
from .nvidia import NvidiaExecutor, doppler_resolver
from .nvidia_server import make_nvidia_server
from .queue import DurableQueue, prepare_queue_storage
from .registry import Registry
from .server import make_server


def demo() -> None:
    now = utcnow()
    target = Target(
        id="offline-google-fixture",
        provider="google",
        model="gemini-2.5-flash-lite",
        account_id="offline-project",
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=now,
        expires_at=now + timedelta(hours=1),
        quotas=(
            Quota("fixture-project-rpm", "requests", 2, "rolling_minute"),
            Quota("fixture-project-tpm", "input_tokens", 10_000, "rolling_minute"),
            Quota("fixture-project-rpd", "requests", 10, "day", "America/Los_Angeles"),
        ),
        concurrency_limit=2,
        max_output_tokens=256,
        priority=0,
        source="offline fixture only; no account assertion",
    )
    # Replace the duplicate metric's bucket is intentional: RPM and RPD share requests.
    with tempfile.TemporaryDirectory() as directory:
        broker = Broker(Path(directory) / "demo.db", (target,))
        server = make_server(broker, port=0, token="fixture-broker-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        calls = []

        def fixture_transport(
            url: str, headers: dict[str, str], payload: dict[str, object], _timeout: float
        ) -> tuple[int, dict[str, str], bytes]:
            calls.append(1)
            return (
                200,
                {},
                json.dumps(
                    {
                        "responseId": "fixture-response-1",
                        "usageMetadata": {"promptTokenCount": 11},
                        "candidates": [{"content": {"parts": [{"text": "fixture answer"}]}}],
                    }
                ).encode(),
            )

        try:
            client = DirectClient(
                f"http://127.0.0.1:{server.server_port}", "fixture-broker-token", fixture_transport
            )
            key = str(uuid.uuid4())
            outcome = client.run_text(
                "offline hello", key, "fixture-secret", model="gemini-2.5-flash-lite"
            )
            print(
                json.dumps(
                    {
                        "status": outcome["status"],
                        "state": outcome["state"],
                        "reservation_id": outcome["reservation_id"],
                        "provider_calls": len(calls),
                        "restart_state": Broker(Path(directory) / "demo.db", (target,)).status(
                            outcome["reservation_id"]
                        )["state"],
                    },
                    indent=2,
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


DISCOVERY_ACTIONS = {
    "coverage",
    "discovery-refresh",
    "discovery-attest",
    "discovery-candidates",
    "discovery-public",
}


def _discovery_stdin(limit: int = 5 * 1024 * 1024) -> dict[str, Any]:
    content = sys.stdin.read(limit + 1)
    if len(content.encode("utf-8")) > limit:
        raise DiscoveryError("invalid_request", "discovery metadata exceeds input bound")
    try:
        raw = json.loads(content)
    except ValueError as exc:
        raise DiscoveryError("invalid_request", "invalid discovery metadata") from exc
    if not isinstance(raw, dict):
        raise DiscoveryError("invalid_request", "invalid discovery metadata")
    return raw


def _local_discovery(args: argparse.Namespace) -> None:
    if args.action not in DISCOVERY_ACTIONS or not args.config or not args.db:
        raise DiscoveryError("invalid_request", "local discovery requires config and database")
    if args.token_file or args.token_stdin:
        raise DiscoveryError("invalid_request", "local discovery does not use HTTP credentials")
    registry = Registry.load(args.registry_file) if args.registry_file else Registry.builtin()
    targets = load_gateway_config(args.config, registry=registry)
    store = DiscoveryStore(args.db, registry, targets=targets, clock=utcnow)
    result: Any
    if args.action == "coverage":
        result = store.coverage(
            provider=args.provider,
            model=args.model,
            capability=args.capability,
            limit=args.limit,
            before=args.before,
        )
    elif args.action == "discovery-refresh":
        result = store.refresh(_discovery_stdin())
    elif args.action == "discovery-attest":
        result = store.attest(_discovery_stdin(65_536))
    else:
        policy = importlib.import_module("quota_broker.discovery_sources")
        if args.action == "discovery-public":
            snapshot = policy.fetch_public_snapshot(
                args.provider, output_modalities=args.output_modalities, clock=utcnow
            )
            result = store.refresh(snapshot)
        else:
            records = []
            before = None
            while True:
                page = store.coverage(provider=args.provider, limit=1000, before=before)
                records.extend(page["records"])
                before = page["next_before"]
                if before is None:
                    break
            result = policy.candidate_bundle(records, registry, targets)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


def _media_limits(args: argparse.Namespace) -> MediaLimits:
    defaults = MediaLimits()
    return MediaLimits(
        decoded_input_bytes=getattr(args, "max_media_input_bytes", defaults.decoded_input_bytes),
        request_bytes=getattr(args, "max_request_bytes", defaults.request_bytes),
        result_bytes=getattr(args, "max_result_bytes", defaults.result_bytes),
    )


def _media_flags(parser: argparse.ArgumentParser) -> None:
    defaults = MediaLimits()
    parser.add_argument("--max-media-input-bytes", type=int, default=defaults.decoded_input_bytes)
    parser.add_argument("--max-request-bytes", type=int, default=defaults.request_bytes)
    parser.add_argument("--max-result-bytes", type=int, default=defaults.result_bytes)


def _input_part(path: str, mime: str | None, maximum: int) -> dict[str, str]:
    if not isinstance(mime, str):
        raise TypeError("media file requires an explicit MIME type")
    if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        kind = "image"
    elif mime in {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/ogg",
        "audio/flac",
        "audio/mp4",
        "audio/webm",
        "audio/aac",
    }:
        kind = "audio"
    elif mime == "application/pdf":
        kind = "document"
    else:
        raise ValueError("unsupported media MIME type")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise ValueError("media file exceeds limit or is not regular")
            content = stream.read(maximum + 1)
        if not 0 < len(content) <= maximum:
            raise ValueError("media file exceeds limit")
    except OSError:
        raise ValueError("media file cannot be read") from None
    return {"type": kind, "mime_type": mime, "data": base64.b64encode(content).decode("ascii")}


def _attach_media(body: dict[str, Any], part: dict[str, str]) -> dict[str, Any]:
    body = dict(body)
    capability = body.get("capability")
    existing = body.get("input")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("media file conflicts with existing input")
    value = dict(existing or {})
    if capability in {"audio_transcription", "audio_translation", "ocr"}:
        key = "document" if capability == "ocr" else "audio"
        allowed = {"document", "image"} if capability == "ocr" else {"audio"}
        if part["type"] not in allowed or value:
            raise ValueError("media file conflicts with input or capability")
        value[key] = part
    elif capability in {"text_generation", "vision"}:
        if set(value) - {"messages"}:
            raise ValueError("media file conflicts with existing input")
        messages = value.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("invalid media messages")
        messages = [dict(message) if isinstance(message, dict) else message for message in messages]
        user = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            None,
        )
        if user is None:
            messages.append({"role": "user", "content": [part]})
        else:
            content = user.get("content")
            if isinstance(content, str):
                user["content"] = [{"type": "text", "text": content}, part]
            elif isinstance(content, list):
                user["content"] = [*content, part]
            else:
                raise ValueError("invalid media messages")
        value["messages"] = messages
    else:
        raise ValueError("media file is unsupported for capability")
    body["input"] = value
    return body


def _typed_task(args: argparse.Namespace, limits: MediaLimits) -> dict[str, Any]:
    content = sys.stdin.read(limits.request_bytes + 1)
    if len(content.encode("utf-8")) > limits.request_bytes:
        raise ValueError("task exceeds request limit")
    try:
        body = json.loads(content) if content.strip() else {}
    except (ValueError, RecursionError):
        raise ValueError("invalid typed task JSON") from None
    if not isinstance(body, dict):
        raise TypeError("invalid typed task JSON")
    for name in (
        "request_key",
        "capability",
        "provider",
        "model",
        "max_output_tokens",
        "source_language",
        "target_language",
        "neuron_bound",
        "priority",
        "deadline",
        "wait_policy",
        "max_attempts",
    ):
        value = getattr(args, name, None)
        if value is not None:
            if name in body and body[name] != value:
                raise ValueError("task JSON conflicts with CLI fields")
            body[name] = value
    if getattr(args, "require_feature", None):
        if "requirements" in body:
            raise ValueError("task JSON conflicts with CLI requirements")
        body["requirements"] = {"features": args.require_feature}
    path, mime = getattr(args, "input_file", None), getattr(args, "mime_type", None)
    if path:
        body = _attach_media(body, _input_part(path, mime, limits.decoded_input_bytes))
    elif mime is not None:
        raise ValueError("MIME type requires a media file")
    try:
        encoded_size = len(json.dumps(body).encode("utf-8"))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("invalid typed task JSON") from None
    if encoded_size > limits.request_bytes:
        raise ValueError("task exceeds request limit")
    return body


def _write_media_result(response: Any, path: str, limits: MediaLimits) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        raise TypeError("output file requires one typed media result")
    result = response["result"]
    nested = (
        "audio" not in result and "images" not in result and isinstance(result.get("result"), dict)
    )
    media = result["result"] if nested else result
    if "audio" in media and "images" not in media:
        family, part = "tts", media["audio"]
    elif (
        "images" in media
        and "audio" not in media
        and isinstance(media["images"], list)
        and len(media["images"]) == 1
    ):
        family, part = "image_generation", media["images"][0]
    else:
        raise ValueError("output file requires one typed media result")
    if not isinstance(part, dict) or not isinstance(part.get("data"), str):
        raise TypeError("invalid media result")
    data = part["data"]
    if len(data) > 4 * ((limits.result_bytes + 2) // 3):
        raise ValueError("media result exceeds limit")
    try:
        normalized = cast(
            dict[str, Any], FamilyRegistry.builtin().validate_result(family, media, limits)
        )
        normalized_part = normalized["audio"] if family == "tts" else normalized["images"][0]
        raw = base64.b64decode(normalized_part["data"], validate=True)
    except (FamilyError, ValueError, TypeError, binascii.Error):
        raise ValueError("invalid media result") from None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
    except OSError:
        raise ValueError("media output cannot be written exclusively") from None
    safe_part = {key: value for key, value in normalized_part.items() if key != "data"}
    safe_part["bytes"] = len(raw)
    safe_media = {"audio": safe_part} if family == "tts" else {"images": [safe_part]}
    safe_result = {**result, "result": safe_media} if nested else safe_media
    return {**response, "result": safe_result, "output_written": True}


def gateway_cli(args: argparse.Namespace) -> None:
    if (
        getattr(args, "config", None)
        or getattr(args, "db", None)
        or getattr(args, "registry_file", None)
    ):
        _local_discovery(args)
        return
    if args.action == "discovery-public":
        raise DiscoveryError(
            "invalid_request", "public discovery requires local config and database"
        )
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("gateway CLI requires loopback HTTP")
    if not args.token_stdin and not args.token_file and args.url != "http://127.0.0.1:18084":
        raise ClientCredentialError("client_runtime_endpoint_not_allowed")
    if args.token_stdin:
        if args.action in {
            "run",
            "explain",
            "submit",
            "observe-quota",
            "discovery-refresh",
            "discovery-attest",
        }:
            raise ValueError("task input and client token cannot share standard input")
        token = sys.stdin.readline().strip()
    elif args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    else:
        token = read_runtime_client()
    if len(token) < 32:
        raise ValueError("invalid gateway client token")
    if args.action in {"wait", "worker"} and (args.interval <= 0 or args.interval > 60):
        raise ValueError("poll interval must be within 0 to 60 seconds")
    if args.action == "wait" and not 0 <= args.timeout <= 86400:
        raise ValueError("wait timeout must be within 0 to 86400 seconds")
    media_limits = _media_limits(args)
    base = args.url.rstrip("/")
    headers = {"Authorization": "Bearer " + token}
    http_timeout = getattr(args, "http_timeout", 185.0)
    if not 0 < http_timeout <= 3600:
        raise ValueError("HTTP timeout must be 0 to 3600 seconds")

    def request(url: str, body: dict | None, headers: dict) -> Any:
        return _json_http(url, body, headers, timeout=http_timeout)

    result: Any
    if args.action in {"observe-quota", "reset-health"}:
        path = base + "/v1/admin/targets/" + args.target_id
        body = json.load(sys.stdin) if args.action == "observe-quota" else {}
        result = request(
            path + ("/quota" if args.action == "observe-quota" else "/health/reset"), body, headers
        )
    elif args.action in {"discovery-refresh", "discovery-attest"}:
        body = _discovery_stdin(5 * 1024 * 1024 if args.action == "discovery-refresh" else 65_536)
        path = "/v1/admin/discovery/" + (
            "refresh" if args.action == "discovery-refresh" else "attest"
        )
        result = request(base + path, body, headers)
    elif args.action == "coverage":
        query = urlencode(
            {
                key: value
                for key, value in {
                    "provider": args.provider,
                    "model": args.model,
                    "capability": args.capability,
                    "limit": args.limit,
                    "before": args.before,
                }.items()
                if value is not None
            }
        )
        result = request(base + "/v1/coverage?" + query, None, headers)
    elif args.action == "discovery-candidates":
        query = urlencode({"provider": args.provider}) if args.provider is not None else ""
        result = request(
            base + "/v1/coverage/candidates" + ("?" + query if query else ""), None, headers
        )
    elif args.action == "catalog":
        result = request(base + "/v1/catalog", None, headers)
    elif args.action == "diagnostics":
        result = request(base + "/v1/diagnostics", None, headers)
    elif args.action == "recent":
        query = urlencode(
            {
                k: v
                for k, v in {
                    "limit": args.limit,
                    "before": args.before,
                    "provider": args.provider,
                    "model": args.model,
                    "state": args.state,
                }.items()
                if v is not None
            }
        )
        result = request(base + "/v1/tasks?" + query, None, headers)
    elif args.action == "status":
        result = request(base + "/v1/tasks/" + args.request_key, None, headers)
    elif args.action == "usage":
        query = urlencode(
            {
                k: v
                for k, v in {
                    "provider": args.provider,
                    "model": args.model,
                    "from": args.from_at,
                    "to": args.to_at,
                }.items()
                if v is not None
            }
        )
        result = request(base + "/v1/usage" + ("?" + query if query else ""), None, headers)
    elif args.action in {"queue-status", "result", "cancel", "wait"}:
        path = base + "/v1/queue/" + args.request_key
        if args.action == "result":
            path += "/result"
        elif args.action == "cancel":
            path += "/cancel"
        result = request(path, {} if args.action == "cancel" else None, headers)
        if args.action == "wait":
            stop_at = time.monotonic() + args.timeout
            while (
                result["state"] in {"queued", "waiting", "running"} and time.monotonic() < stop_at
            ):
                time.sleep(min(args.interval, max(0, stop_at - time.monotonic())))
                result = request(path, None, headers)
    elif args.action == "worker":
        result = None
        try:
            while True:
                result = request(base + "/v1/queue/tick", {}, headers)
                if args.once:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return
    else:
        if getattr(args, "task_stdin", False) or getattr(args, "input_file", None):
            body = _typed_task(args, media_limits)
        else:
            if getattr(args, "mime_type", None):
                raise ValueError("MIME type requires a media file")
            limit = 48_000 if args.capability == "ocr" else 32_768
            content = sys.stdin.read(limit + 1)
            if len(content.encode("utf-8")) > limit:
                raise ValueError("input exceeds gateway limit")
            body = {
                "request_key": args.request_key,
                "capability": args.capability,
                "input": content,
                # The server resolves omitted bounds against matching limits.
                # An explicit caller bound is never clamped.
                "max_output_tokens": args.max_output_tokens,
                "provider": args.provider,
                "model": args.model,
                "source_language": args.source_language,
                "target_language": args.target_language,
                "neuron_bound": args.neuron_bound,
            }
            for name in ("priority", "deadline", "wait_policy", "max_attempts"):
                value = getattr(args, name, None)
                if value is not None:
                    body[name] = value
            if getattr(args, "require_feature", None):
                body["requirements"] = {"features": args.require_feature}
        path = {"run": "/v1/tasks", "submit": "/v1/queue", "explain": "/v1/routes/explain"}[
            args.action
        ]
        result = request(base + path, body, headers)
    if getattr(args, "output_file", None):
        result = _write_media_result(result, args.output_file, media_limits)
    if args.json or (isinstance(result, dict) and isinstance(result.get("result"), dict)):
        print(json.dumps(result, ensure_ascii=False))
    elif args.action in DISCOVERY_ACTIONS:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.action == "catalog":
        for item in result:
            print(
                item["target_id"],
                item["provider"],
                item["model"],
                "available" if item["available"] else "unavailable",
                "default_output="
                + str(item.get("request_limits", {}).get("default_output_tokens")),
                "max_output=" + str(item.get("request_limits", {}).get("max_output_tokens")),
                "max_input_bytes="
                + str(
                    item.get("request_limits", {}).get(
                        "max_legacy_input_bytes_for_requested_output"
                    )
                ),
            )
    elif args.action == "usage":
        for item in result:
            print(
                item["provider"],
                item["model"],
                "requests=" + str(item["requests"]),
                "input=" + str(item["reported_input_tokens"]),
                "input_estimate=" + str(item["estimated_input_tokens"]),
                "ledger_input=" + str(item["ledger_input_tokens"]),
                "input_unknown=" + str(item["input_unknown_count"]),
                "output=" + str(item["reported_output_tokens"]),
                "output_unknown=" + str(item["output_unknown_count"]),
                "quota_rejected=" + str(item["quota_rejected_count"]),
                "ledger_held=" + str(item["ledger_held_count"]),
                "neurons=" + str(item["reported_neurons"]),
                "neurons_unknown=" + str(item["neurons_unknown_count"]),
                "ledger_neurons=" + str(item["ledger_neurons"]),
                "outcome_unknown=" + str(item["outcome_unknown_count"]),
                "truncated=" + str(item["truncated_count"]),
                "image_bytes=" + str(item["input_bytes"]),
            )
    elif args.action == "explain":
        print("selected:", result["selected_target_id"] or "none")
        for item in result["candidates"]:
            print(item["target_id"], "eligible" if item["eligible"] else ",".join(item["reasons"]))
    elif args.action == "diagnostics":
        print("ready_targets=" + str(result["ready_targets"]), result["basis"])
        if "queue_worker" in result:
            print("queue_worker:", json.dumps(result["queue_worker"]))
        for item in result["targets"]:
            print(
                item["target_id"],
                item["state"],
                ",".join(item["reasons"]),
                "missing_names=" + str(item["credentials"]["missing_names"]),
                "cooldown_until=" + str(item["cooldown_until"]),
            )
    elif args.action == "recent":
        for item in result["tasks"]:
            print(
                item["request_key"],
                item["provider"],
                item["model"],
                item["state"],
                "attempts=" + str(len(item["attempts"])),
                "truncated=" + str(item["response_truncated"]),
                "latency_ms=" + str(item["latency_ms"]),
                "ledger=" + str(item["ledger_basis"]),
            )
        print("next_before:", result["next_before"] or "none")
    elif result is None:
        print("idle")
    else:
        print(
            result["state"],
            result.get("provider"),
            result.get("model"),
            "input=" + str(result.get("reported_input_tokens")),
            "output=" + str(result.get("reported_output_tokens")),
            "finish=" + str(result.get("finish_reason")),
            "truncated=" + str(result.get("response_truncated")),
            "ledger=" + str(result.get("ledger_basis")),
        )
        for attempt in result.get("attempts", []):
            print(
                "attempt=" + str(attempt["attempt_no"] + 1),
                attempt["provider"],
                attempt["model"],
                attempt["state"],
                "http=" + str(attempt["http_status"]),
                "latency_ms=" + str(attempt["latency_ms"]),
                "usage=" + str(attempt["usage_source"]),
            )
        if result.get("answer") is not None:
            print(result["answer"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Official API free-quota control plane")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--db", required=True)
    serve.add_argument("--port", type=int, default=18081)
    nvidia = sub.add_parser("serve-nvidia")
    nvidia.add_argument("--db", required=True)
    nvidia.add_argument("--port", type=int, default=18083)
    nvidia.add_argument("--digest-key-file", required=True)
    nvidia.add_argument("--client-token-file", required=True)
    nvidia.add_argument("--admin-token-file", required=True)
    nvidia.add_argument("--doppler-token-file", required=True)
    nvidia.add_argument("--doppler-project", required=True)
    nvidia.add_argument("--doppler-config", required=True)
    key_admin = sub.add_parser("key-admin-serve")
    key_admin.add_argument("--port", type=int, default=18085)
    key_admin.add_argument("--admin-token-file", required=True)
    key_admin.add_argument("--metadata-file", required=True)
    gateway_serve = sub.add_parser("gateway-serve")
    _media_flags(gateway_serve)
    gateway_serve.add_argument("--config", required=True)
    gateway_serve.add_argument("--db", required=True)
    gateway_serve.add_argument("--port", type=int, default=18084)
    gateway_serve.add_argument("--digest-key-file", required=True)
    gateway_serve.add_argument("--client-token-file", required=True)
    gateway_serve.add_argument("--doppler-token-file", required=True)
    gateway_serve.add_argument("--doppler-project", required=True)
    gateway_serve.add_argument("--doppler-config", required=True)
    gateway_serve.add_argument(
        "--admin-token-file", help="separate token for quota observation and health repair"
    )
    gateway_serve.add_argument("--registry-file", help="trusted administrator manifest")
    gateway_serve.add_argument(
        "--queue-key-file", help="explicit independent private persistent key"
    )
    gateway_serve.add_argument("--queue-ttl-seconds", type=int, default=86400)
    gateway_serve.add_argument("--no-queue-worker", action="store_true")
    gateway_serve.add_argument("--secret-names-file", help="optional expiring names-only inventory")
    gateway = sub.add_parser("gateway")
    gateway.add_argument("--version", action="version", version=__version__)
    _media_flags(gateway)
    gateway.add_argument("--url", default="http://127.0.0.1:18084")
    credential = gateway.add_mutually_exclusive_group()
    credential.add_argument("--token-file")
    credential.add_argument("--token-stdin", action="store_true")
    gateway.add_argument("--json", action="store_true")
    gateway.add_argument("--config", help="local discovery configuration; no provider execution")
    gateway.add_argument("--db", help="local discovery database")
    gateway.add_argument("--registry-file", help="trusted local discovery registry")
    gateway.add_argument(
        "--http-timeout", type=float, default=185, help="bounded client wait; timeout never replays"
    )
    actions = gateway.add_subparsers(dest="action", required=True)
    actions.add_parser("catalog")
    actions.add_parser("diagnostics")
    coverage = actions.add_parser("coverage")
    coverage.add_argument("--provider")
    coverage.add_argument("--model")
    coverage.add_argument("--capability")
    coverage.add_argument("--limit", type=int, default=100)
    coverage.add_argument("--before")
    actions.add_parser("discovery-refresh")
    actions.add_parser("discovery-attest")
    candidates = actions.add_parser("discovery-candidates")
    candidates.add_argument("--provider")
    public = actions.add_parser("discovery-public")
    public.add_argument("--provider", required=True, choices=("nvidia", "openrouter"))
    public.add_argument("--output-modalities", required=True, choices=("all", "text"))
    recent = actions.add_parser("recent")
    recent.add_argument("--limit", type=int, default=20)
    recent.add_argument("--before")
    recent.add_argument("--provider")
    recent.add_argument("--model")
    recent.add_argument("--state")
    status = actions.add_parser("status")
    status.add_argument("request_key")
    usage = actions.add_parser("usage")
    usage.add_argument("--provider")
    usage.add_argument("--model")
    usage.add_argument("--from", dest="from_at")
    usage.add_argument("--to", dest="to_at")
    for name in ("observe-quota", "reset-health"):
        admin = actions.add_parser(name)
        admin.add_argument("target_id")
    for name in ("queue-status", "result", "cancel", "wait"):
        item = actions.add_parser(name)
        item.add_argument("request_key")
        if name == "result":
            item.add_argument("--output-file")
        if name == "wait":
            item.add_argument("--timeout", type=float, default=60)
            item.add_argument("--interval", type=float, default=1)
    worker = actions.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=1)
    for name in ("run", "explain", "submit"):
        task = actions.add_parser(name)
        task.add_argument("--request-key", required=True)
        task.add_argument("--capability", required=True)
        task.add_argument("--task-stdin", action="store_true")
        task.add_argument("--input-file")
        task.add_argument("--mime-type")
        if name == "run":
            task.add_argument("--output-file")
        task.add_argument("--max-output-tokens", type=int)
        task.add_argument("--provider")
        task.add_argument("--model")
        task.add_argument("--source-language")
        task.add_argument("--target-language")
        task.add_argument("--neuron-bound", type=int)
        task.add_argument("--require-feature", action="append")
        task.add_argument("--priority", type=int)
        task.add_argument("--deadline")
        task.add_argument("--wait-policy", choices=("wait", "reject"))
        task.add_argument("--max-attempts", type=int)
    catalog = sub.add_parser("catalog")
    catalog.add_argument("--config", required=True)
    catalog.add_argument("--db", required=True)
    sub.add_parser("demo")
    args = parser.parse_args()
    if args.command == "key-admin-serve":
        token_path = Path(args.admin_token_file)
        descriptor = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("private administrator token file required")
            admin_token = stream.read().strip()
        writer = DopplerCLIWriter(str(Path.home() / ".local" / "bin" / "doppler"), Path.cwd())
        server = make_key_admin_server(
            writer, MetadataStore(Path(args.metadata_file)), admin_token, port=args.port
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    if args.command == "gateway":
        try:
            gateway_cli(args)
        except ClientCredentialError as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        "action": "check api-quota-broker-client unit; no automatic sudo or anonymous fallback",
                    }
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        except ClientError as error:
            print(json.dumps(error.detail or {"error": "broker_request_failed"}), file=sys.stderr)
            raise SystemExit(1) from None
        return
    if args.command == "gateway-serve":
        registry = Registry.load(args.registry_file) if args.registry_file else Registry.builtin()
        if args.queue_key_file:
            prepare_queue_storage(args.db, args.queue_key_file)
        inventory = (
            load_secret_inventory(args.secret_names_file, args.doppler_project, args.doppler_config)
            if args.secret_names_file
            else None
        )
        resolver = doppler_resolver(
            args.doppler_token_file, args.doppler_project, args.doppler_config
        )
        gateway_instance = Gateway(
            args.db,
            load_gateway_config(args.config, registry=registry),
            Path(args.digest_key_file).read_bytes(),
            resolver,
            secret_inventory=inventory,
            registry=registry,
            media_limits=_media_limits(args),
        )
        client_token = Path(args.client_token_file).read_text(encoding="utf-8").strip()
        queue = (
            DurableQueue(
                gateway_instance, Path(args.queue_key_file), ttl_seconds=args.queue_ttl_seconds
            )
            if args.queue_key_file
            else None
        )
        server = make_gateway_server(
            gateway_instance,
            client_token,
            port=args.port,
            queue=queue,
            worker=queue is not None and not args.no_queue_worker,
            admin_token=Path(args.admin_token_file).read_text().strip()
            if args.admin_token_file
            else None,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    if args.command == "demo":
        demo()
        return
    if args.command == "serve-nvidia":
        digest_key = Path(args.digest_key_file).read_bytes()
        client_token = Path(args.client_token_file).read_text(encoding="utf-8").strip()
        admin_token = Path(args.admin_token_file).read_text(encoding="utf-8").strip()
        resolver = doppler_resolver(
            args.doppler_token_file, args.doppler_project, args.doppler_config
        )
        executor = NvidiaExecutor(
            args.db,
            digest_key,
            resolver,
            doppler_project=args.doppler_project,
            doppler_config=args.doppler_config,
        )
        server = make_nvidia_server(executor, "127.0.0.1", args.port, client_token, admin_token)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    broker = Broker(args.db, load_config(args.config))
    if args.command == "catalog":
        print(json.dumps(broker.catalog(), indent=2))
    else:
        token = os.environ.get("BROKER_TOKEN")
        server = make_server(broker, port=args.port, token=token)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
