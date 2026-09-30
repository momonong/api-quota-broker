"""Local server and an entirely offline direct-call demonstration."""

import argparse
import json
import os
import tempfile
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from .client import DirectClient
from .config import Quota, Target, load_config
from .core import Broker, utcnow
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
    catalog = sub.add_parser("catalog")
    catalog.add_argument("--config", required=True)
    catalog.add_argument("--db", required=True)
    sub.add_parser("demo")
    args = parser.parse_args()
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
