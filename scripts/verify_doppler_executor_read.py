"""One-shot, approved, read-only Doppler executor adapter check.

Requires a human CLI login and explicit approval to create a 5-minute service
access. Holds both the service token and secret value only in process memory.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

from quota_broker.nvidia import doppler_resolver_from_token

PROJECT = "api-quota-broker"
CONFIG = "dev"
SECRET_NAME = "NVIDIA_API_KEY"
MAX_AGE = "5m"
SCOPE = str(Path(__file__).resolve().parents[1])


def cli() -> str:
    local = Path.home() / ".local" / "bin" / "doppler"
    if not local.is_file():
        raise RuntimeError("Validated user-local Doppler CLI unavailable")
    return str(local)


def checked_metadata(binary: str) -> bool:
    command = [
        binary,
        "--no-read-env",
        "--no-check-version",
        "--attempts",
        "1",
        "--scope",
        SCOPE,
        "secrets",
        "--only-names",
        "--json",
        "--project",
        PROJECT,
        "--config",
        CONFIG,
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError("Doppler metadata lookup failed")
    try:
        names = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Doppler metadata response invalid") from exc
    return SECRET_NAME in names if isinstance(names, (list, dict)) else False


def create_service_token(binary: str) -> str:
    token_name = "broker-one-shot-" + uuid.uuid4().hex[:12]
    command = [
        binary,
        "--no-read-env",
        "--no-check-version",
        "--attempts",
        "1",
        "--scope",
        SCOPE,
        "configs",
        "tokens",
        "create",
        token_name,
        "--project",
        PROJECT,
        "--config",
        CONFIG,
        "--access",
        "read",
        "--max-age",
        MAX_AGE,
        "--plain",
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError("Service Token creation failed or is uncertain; inspect Access metadata")
    return result.stdout.decode("utf-8").strip()


def create_and_read(binary: str) -> bool:
    token = create_service_token(binary)
    resolver = doppler_resolver_from_token(token, PROJECT, CONFIG)
    value = resolver(SECRET_NAME)
    return bool(value)


def main() -> int:
    if not sys.stdin.isatty():
        print("interactive TTY required")
        return 1
    print(f"Target: {PROJECT}/{CONFIG}/{SECRET_NAME}")
    print("Create: read-only, config-scoped Service Token, automatic expiry 5m")
    print("Destination: this process memory only; no secret value or token displayed")
    print("No NVIDIA request or service change; token/secret not written to local files")
    if input("Type CREATE-READ-5M after approval: ").strip() != "CREATE-READ-5M":
        print("cancelled")
        return 1
    try:
        binary = cli()
        if not checked_metadata(binary):
            print("metadata_name_present: no")
            return 1
        print("metadata_name_present: yes")
        success = create_and_read(binary)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("executor_secret_read: failed", type(exc).__name__)
        print("temporary_access_may_exist: check Doppler Access metadata; max age 5m")
        return 1
    print("executor_secret_read:", "success" if success else "failed")
    print("temporary_access_expiry: 5m from creation")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
