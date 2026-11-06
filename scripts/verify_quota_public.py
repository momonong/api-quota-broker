"""Fixed anonymous HTTPS acceptance checks. Default is an offline plan.

No credentials, model payloads, arbitrary URL, redirect following or retries.
Even a misconfigured execution route receives only an invalid empty object.
"""

import argparse
import hashlib
import json
import ssl
from http.client import HTTPSConnection
from pathlib import Path

HOST = "momonong.me"
LIMIT = 262144
CHECKS = (
    ("GET", "/quota", 308),
    ("GET", "/quota/", 200),
    ("GET", "/quota/dashboard.css", 200),
    ("GET", "/quota/dashboard.js", 200),
    ("GET", "/quota/api/snapshot", 401),
    ("GET", "/quota/api/usage", 401),
    ("POST", "/quota/api/advice", 401),
    ("POST", "/quota/api/v1/tasks", 404),
    ("POST", "/quota/api/v1/jobs/tick", 404),
    ("POST", "/quota/api/admin/probe", 404),
    ("GET", "/quota/api/maintenance", 404),
)
ASSETS = {
    "/quota/": "index.html",
    "/quota/dashboard.css": "dashboard.css",
    "/quota/dashboard.js": "dashboard.js",
}


def verify(*, apply=False, assets=None, connection=HTTPSConnection):
    result = {
        "mode": "anonymous_public_acceptance" if apply else "offline_plan",
        "host": HOST,
        "requests": 0,
        "provider_calls": 0,
        "paid_allowed": False,
        "automatic_retry": False,
        "checks": [],
        "status": "planned",
    }
    if not apply:
        result["checks"] = [{"method": m, "path": p, "expected": s} for m, p, s in CHECKS]
        return result
    for method, path, expected in CHECKS:
        item = {"method": method, "path": path, "expected": expected, "status": None, "ok": False}
        client = None
        try:
            client = connection(HOST, timeout=8, context=ssl.create_default_context())
            result["requests"] += 1
            client.request(
                method,
                path,
                body=b"{}" if method == "POST" else None,
                headers={
                    "Host": HOST,
                    "Origin": "https://" + HOST,
                    "Content-Type": "application/json",
                    "X-Dashboard-Request": "1",
                },
            )
            response = client.getresponse()
            raw = response.read(LIMIT + 1)
            item["status"] = response.status
            item["ok"] = len(raw) <= LIMIT and response.status == expected
            if path == "/quota":
                item["ok"] &= response.getheader("Location") in {
                    "/quota/",
                    "https://momonong.me/quota/",
                }
            elif path in ASSETS:
                item["ok"] &= "no-store" in (response.getheader("Cache-Control") or "")
                if assets is not None:
                    item["content_matches"] = (
                        hashlib.sha256(raw).digest()
                        == hashlib.sha256((Path(assets) / ASSETS[path]).read_bytes()).digest()
                    )
                    item["ok"] &= item["content_matches"]
        except Exception:  # noqa: BLE001 - no remote body/error reflection
            item["error"] = "verification_unavailable"
        finally:
            if client is not None:
                client.close()
        result["checks"].append(item)
    result["status"] = "passed" if all(row["ok"] for row in result["checks"]) else "failed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--assets", type=Path, help="compare against sealed local public assets")
    args = parser.parse_args()
    result = verify(apply=args.apply, assets=args.assets)
    print(json.dumps(result, separators=(",", ":")))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
