"""Admin-only interactive Doppler CLI candidate; never run from executor service."""

import getpass
import re
import shutil
import subprocess
import sys

SECRET_NAME = "NVIDIA_API_KEY"
NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def set_secret(project: str, config: str, secret: str) -> None:
    if not NAME.fullmatch(project) or not NAME.fullmatch(config):
        raise ValueError("invalid project or config name")
    if not secret or len(secret) > 4096 or "\n" in secret or "\r" in secret:
        raise ValueError("invalid secret")
    if shutil.which("doppler") is None:
        raise RuntimeError("Doppler CLI is unavailable")
    base = ["doppler", "--no-read-env", "--silent", "--project", project, "--config", config]
    # Confirm authentication and exact project/config access without printing secret names.
    probe = subprocess.run(base + ["secrets", "--only-names"], capture_output=True, check=False)
    if probe.returncode != 0:
        raise RuntimeError("Doppler project/config preflight failed")
    result = subprocess.run(
        base + ["secrets", "set", SECRET_NAME],
        input=secret.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Doppler secret update failed")


def main() -> int:
    if not sys.stdin.isatty():
        print("Interactive TTY required", file=sys.stderr)
        return 1
    project = input("Approved Doppler project: ").strip()
    config = input("Approved Doppler config: ").strip()
    if not NAME.fullmatch(project) or not NAME.fullmatch(config):
        print("Invalid project or config", file=sys.stderr)
        return 1
    print("Destination:", project + "/" + config + "/" + SECRET_NAME)
    if input("Type the exact project/config to confirm: ").strip() != project + "/" + config:
        print("Destination confirmation failed", file=sys.stderr)
        return 1
    secret = getpass.getpass("NVIDIA key (hidden): ")
    try:
        set_secret(project, config, secret)
    except (ValueError, RuntimeError, OSError) as exc:
        print("Doppler update failed:", type(exc).__name__, file=sys.stderr)
        return 1
    print("Doppler reported success. No secret value was displayed or saved locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
