"""Python SDK example. Credentials come only from the caller's environment."""

import argparse
import json
import os
import ssl
import sys

from quota_broker.client import ClientError, LegacyProjectClient, ProjectClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("catalog", "explain", "run", "submit", "wait", "status", "result", "cancel"),
    )
    parser.add_argument("--key", help="Persisted business operation ID; reuse it when recovering")
    parser.add_argument("--text", default="只回答 OK。")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--execution-policy", choices=("local_only", "cloud_only"))
    parser.add_argument(
        "--legacy", action="store_true", help="Explicit non-sensitive legacy JSON client"
    )
    parser.add_argument("--max-output-tokens", type=int, default=32)
    parser.add_argument("--wait-timeout", type=float, default=120)
    args = parser.parse_args()
    token = os.environ.get("QUOTA_PROJECT_TOKEN")
    if not token:
        parser.error("Set QUOTA_PROJECT_TOKEN using your secret manager or private environment")
    if args.action != "catalog" and not args.key:
        parser.error("--key is required; save it before submitting")
    try:
        base = os.environ.get("QUOTA_BASE_URL", "https://momonong.me/quota/api/v1")
        if args.legacy:
            if args.execution_policy == "local_only":
                parser.error("local_only requires the encrypted client")
            client = LegacyProjectClient(base, token)
        else:
            client = ProjectClient(
                base,
                token,
                trust_profile=os.environ.get("QUOTA_TRUST_PROFILE"),
                trust_state=os.environ.get("QUOTA_TRUST_STATE"),
                ssl_context=ssl.create_default_context(cafile=os.environ.get("QUOTA_CA_FILE")),
            )
        task = {
            "request_key": args.key,
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": args.text}]},
            "max_output_tokens": args.max_output_tokens,
        }
        for field in ("provider", "model"):
            if getattr(args, field):
                task[field] = getattr(args, field)
        if args.execution_policy:
            task["execution_policy"] = args.execution_policy
        if args.action == "catalog":
            result = client.catalog()
        elif args.action in {"submit", "explain"}:
            result = getattr(client, args.action)(task)
        elif args.action == "run":
            result = client.run(task, wait_timeout=args.wait_timeout)
        elif args.action == "wait":
            result = client.wait(args.key, wait_timeout=args.wait_timeout)
        else:
            result = getattr(client, args.action)(args.key)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ClientError as exc:
        print(
            json.dumps(
                exc.detail or {"error": "client_error", "message": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
