"""Local server and an entirely offline direct-call demonstration."""

import argparse
import json
import os
import stat
import sys
import tempfile
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .client import DirectClient, _json_http
from .config import Quota, Target, load_config, load_gateway_config, load_secret_inventory
from .core import Broker, utcnow
from .gateway import Gateway
from .gateway_server import make_gateway_server
from .key_admin import DopplerCLIWriter, MetadataStore, make_key_admin_server
from .nvidia import NvidiaExecutor, doppler_resolver
from .nvidia_server import make_nvidia_server
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


def gateway_cli(args: argparse.Namespace) -> None:
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("gateway CLI requires loopback HTTP")
    if args.token_stdin:
        if args.action in {"run", "explain"}:
            raise ValueError("task input and client token cannot share standard input")
        token = sys.stdin.readline().strip()
    else:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("invalid gateway client token")
    base = args.url.rstrip("/")
    headers = {"Authorization": "Bearer " + token}
    if args.action == "catalog":
        result = _json_http(base + "/v1/catalog", None, headers)
    elif args.action == "diagnostics":
        result = _json_http(base + "/v1/diagnostics", None, headers)
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
        result = _json_http(base + "/v1/tasks?" + query, None, headers)
    elif args.action == "status":
        result = _json_http(base + "/v1/tasks/" + args.request_key, None, headers)
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
        result = _json_http(base + "/v1/usage" + ("?" + query if query else ""), None, headers)
    else:
        limit = 48_000 if args.capability == "ocr" else 32_768
        content = sys.stdin.read(limit + 1)
        if len(content.encode("utf-8")) > limit:
            raise ValueError("input exceeds gateway limit")
        body = {
            "request_key": args.request_key,
            "capability": args.capability,
            "input": content,
            "max_output_tokens": args.max_output_tokens
            if args.max_output_tokens is not None
            else (1 if args.capability == "ocr" else 128),
            "provider": args.provider,
            "model": args.model,
            "source_language": args.source_language,
            "target_language": args.target_language,
            "neuron_bound": args.neuron_bound,
        }
        path = "/v1/tasks" if args.action == "run" else "/v1/routes/explain"
        result = _json_http(base + path, body, headers)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == "catalog":
        for item in result:
            print(
                item["target_id"],
                item["provider"],
                item["model"],
                "available" if item["available"] else "unavailable",
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
    else:
        print(
            result["state"],
            result.get("provider"),
            result.get("model"),
            "input=" + str(result.get("reported_input_tokens")),
            "output=" + str(result.get("reported_output_tokens")),
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
    gateway_serve.add_argument("--config", required=True)
    gateway_serve.add_argument("--db", required=True)
    gateway_serve.add_argument("--port", type=int, default=18084)
    gateway_serve.add_argument("--digest-key-file", required=True)
    gateway_serve.add_argument("--client-token-file", required=True)
    gateway_serve.add_argument("--doppler-token-file", required=True)
    gateway_serve.add_argument("--doppler-project", required=True)
    gateway_serve.add_argument("--doppler-config", required=True)
    gateway_serve.add_argument("--secret-names-file", help="optional expiring names-only inventory")
    gateway = sub.add_parser("gateway")
    gateway.add_argument("--url", default="http://127.0.0.1:18084")
    credential = gateway.add_mutually_exclusive_group(required=True)
    credential.add_argument("--token-file")
    credential.add_argument("--token-stdin", action="store_true")
    gateway.add_argument("--json", action="store_true")
    actions = gateway.add_subparsers(dest="action", required=True)
    actions.add_parser("catalog")
    actions.add_parser("diagnostics")
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
    for name in ("run", "explain"):
        task = actions.add_parser(name)
        task.add_argument("--request-key", required=True)
        task.add_argument(
            "--capability", required=True, choices=("text_generation", "translation", "ocr")
        )
        task.add_argument("--max-output-tokens", type=int)
        task.add_argument("--provider")
        task.add_argument("--model")
        task.add_argument("--source-language")
        task.add_argument("--target-language")
        task.add_argument("--neuron-bound", type=int)
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
        gateway_cli(args)
        return
    if args.command == "gateway-serve":
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
            load_gateway_config(args.config),
            Path(args.digest_key_file).read_bytes(),
            resolver,
            secret_inventory=inventory,
        )
        client_token = Path(args.client_token_file).read_text(encoding="utf-8").strip()
        server = make_gateway_server(gateway_instance, client_token, port=args.port)
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
