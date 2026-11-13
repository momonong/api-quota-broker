"""Plain HTTP example: standard library only, exactly one request per command."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("catalog", "explain", "submit", "status", "result", "cancel")
    )
    parser.add_argument("--key")
    parser.add_argument("--text", default="只回答 OK。")
    args = parser.parse_args()
    token = os.environ.get("QUOTA_PROJECT_TOKEN", "")
    root = os.environ.get("QUOTA_BASE_URL", "https://momonong.me/quota/api/v1").rstrip("/")
    try:
        url = urllib.parse.urlsplit(root)
        valid = (url.scheme == "https" and bool(url.hostname)) or (
            url.scheme == "http" and url.hostname == "127.0.0.1" and url.port is not None
        )
        if (
            not valid
            or url.path != "/quota/api/v1"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(ord(c) <= 32 for c in root)
        ):
            raise ValueError
    except ValueError:
        parser.error("QUOTA_BASE_URL must be an HTTPS API root (or explicit local loopback port)")
    if not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
        parser.error("Set QUOTA_PROJECT_TOKEN using your secret manager or private environment")
    if args.action != "catalog" and (
        not args.key or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", args.key)
    ):
        parser.error("--key must be a persisted business operation ID")
    body = None
    if args.action in {"explain", "submit"}:
        body = {
            "request_key": args.key,
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": args.text}]},
            "max_output_tokens": 32,
        }
        path = "/jobs" if args.action == "submit" else "/routes/explain"
    elif args.action == "catalog":
        path = "/catalog"
    else:
        path = "/jobs/" + urllib.parse.quote(args.key, safe="")
        if args.action != "status":
            path += "/" + args.action
        if args.action == "cancel":
            body = {}
    request = urllib.request.Request(
        root + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "quota-http-example/1.0",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc)
            if not isinstance(error, dict):
                error = {"error": "http_error"}
        except (ValueError, OSError, HTTPException):
            error = {"error": "http_error"}
        print(
            json.dumps({**error, "http_status": exc.code, "request_key": args.key}), file=sys.stderr
        )
    except (OSError, ValueError, HTTPException):
        print(
            json.dumps(
                {
                    "error": "transport_unknown",
                    "request_key": args.key,
                    "action": "query_original_job"
                    if args.action != "catalog"
                    else "check_connection",
                }
            ),
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
