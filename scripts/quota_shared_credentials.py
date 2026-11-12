"""Fixed Doppler references for this shared instance; values never go to stdout.

Provision is create-if-absent based on a successful names-only inventory. An
uncertain write stops; reinspection reads the same name, never rotates a key.
All subprocess output is captured and discarded on error.
"""

import argparse
import base64
import hashlib
import json
import os
import resource
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = "hp-shared-20261111"
NAMES = (
    "QUOTA_SHARED_INSTANCE_KEYS",
    "QUOTA_SHARED_OWNER_TOKEN",
    "QUOTA_SHARED_KNOWLEDGE_HUB_TOKEN",
    "QUOTA_SHARED_READ_TOKEN",
    "QUOTA_SHARED_MAINTENANCE_SSH_PRIVATE_KEY",
    "QUOTA_SHARED_TUNNEL_SSH_PRIVATE_KEY",
)


def doppler(arguments, *, input_data=None):
    command = [
        str(Path.home() / ".local/bin/doppler"),
        "--no-read-env",
        "--no-verify-tls=false",
        "--no-check-version",
        "--attempts",
        "1",
        "--timeout",
        "15s",
        "--api-host",
        "https://api.doppler.com",
        "--scope",
        str(ROOT),
        "secrets",
        *arguments,
        "--project",
        "api-quota-broker",
        "--config",
        "dev",
    ]
    result = subprocess.run(command, input=input_data, capture_output=True, timeout=25, check=False)
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError("doppler_operation_unconfirmed_inspect_same_name")
    return result.stdout


def names():
    document = json.loads(doppler(["--only-names", "--json"]))
    if not isinstance(document, dict) or any(v != {} for v in document.values()):
        raise ValueError("names_only_inventory_required")
    return set(document)


def get(name):
    if name not in NAMES:
        raise ValueError("fixed_reference_required")
    raw = doppler(["get", name, "--plain"]).decode().strip()
    if not raw or len(raw) > 16384:
        raise ValueError("invalid_secret")
    return raw


def new_value(name):
    if name == NAMES[0]:
        return json.dumps(
            {
                "schema": 1,
                "instance_id": INSTANCE,
                "digest_key": base64.b64encode(secrets.token_bytes(32)).decode(),
                "queue_key": base64.b64encode(secrets.token_bytes(32)).decode(),
                "legacy_token": secrets.token_urlsafe(48),
            }
        )
    if name in NAMES[1:4]:
        return secrets.token_urlsafe(48)
    if name in NAMES[4:]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
    raise ValueError("fixed_reference_required")


def provision():
    existing = names()
    created = []
    for name in NAMES:
        if name in existing:
            continue
        value = new_value(name)
        doppler(["set", name, "--silent", "--no-interactive"], input_data=value.encode())
        if get(name).strip() != value.strip():
            raise ValueError("secret_readback_mismatch")
        created.append(name)
    return {
        "status": "ready",
        "project": "api-quota-broker",
        "config": "dev",
        "created_names": created,
        "values_output": False,
    }


def access_policy():
    principals = []
    for name, project in ((NAMES[1], "owner"), (NAMES[2], "knowledge-hub"), (NAMES[3], None)):
        token = get(name)
        principals.append(
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "project_id": project,
                "scopes": ["read", "plan", "execute"] if project else ["read", "plan"],
                "limits": {
                    "max_input_bytes": 4096,
                    "max_output_tokens": 512,
                    "max_inflight": 1,
                    "max_deadline_seconds": 3600,
                },
            }
        )
    return {"version": 1, "principals": principals}


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(provision()))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - secret-bearing subprocess errors must not escape
        print(
            '{"status":"stopped","reason":"credential_operation_unconfirmed","automatic_retry":false}'
        )
        raise SystemExit(1) from None
