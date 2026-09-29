"""Interactive, TPM-bound Doppler token import for a reviewed systemd host.

Run manually on the intended Linux host only after its service owner and path
have been approved. This script does not install or start a service.
"""

import getpass
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

CREDENTIAL_NAME = "doppler_service_token"


def check_host(directory: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run as root on the reviewed target host")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("systemd system manager is unavailable")
    if shutil.which("systemd-creds") is None:
        raise RuntimeError("systemd-creds is unavailable")
    probe = subprocess.run(
        ["systemd-creds", "has-tpm2", "--quiet"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        raise RuntimeError("TPM2 is unavailable; no plaintext or host-key fallback")
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or directory.resolve() != directory
        or not directory.is_dir()
    ):
        raise RuntimeError("Credential directory must already exist as an absolute real directory")
    info = directory.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("Credential directory must be root-owned and mode 0700 or stricter")
    if (directory / (CREDENTIAL_NAME + ".cred")).exists():
        raise RuntimeError(
            "Credential already exists; rotation requires a separate reviewed procedure"
        )


def import_credential(directory: Path, token: str) -> Path:
    if not token or len(token) > 4096 or "\n" in token or "\r" in token:
        raise ValueError("Invalid token input")
    target = directory / (CREDENTIAL_NAME + ".cred")
    temporary = directory / ("." + CREDENTIAL_NAME + "." + uuid.uuid4().hex + ".tmp")
    old_umask = os.umask(0o077)
    try:
        result = subprocess.run(
            [
                "systemd-creds",
                "--with-key=tpm2",
                "--name=" + CREDENTIAL_NAME,
                "encrypt",
                "-",
                str(temporary),
            ],
            input=token.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not temporary.is_file():
            raise RuntimeError("systemd-creds encryption failed; no credential installed")
        os.chmod(temporary, 0o600)
        # link() refuses an existing target; never replace a credential silently.
        os.link(temporary, target)
        return target
    finally:
        os.umask(old_umask)
        temporary.unlink(missing_ok=True)


def main() -> int:
    if not sys.stdin.isatty():
        print("Interactive TTY required", file=sys.stderr)
        return 1
    hostname = socket.gethostname()
    print("Target host:", hostname)
    if input("Type the target hostname to confirm: ").strip() != hostname:
        print("Host confirmation failed", file=sys.stderr)
        return 1
    if input("Type the approved credential owner (root): ").strip() != "root":
        print("Owner confirmation failed", file=sys.stderr)
        return 1
    directory = Path(input("Approved existing absolute credential directory: ").strip())
    try:
        check_host(directory)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    target = directory / (CREDENTIAL_NAME + ".cred")
    print("Encrypted output:", target)
    if input("Type INSTALL to proceed: ").strip() != "INSTALL":
        print("Cancelled", file=sys.stderr)
        return 1
    token = getpass.getpass("Doppler config-scoped read-only Service Token: ")
    try:
        import_credential(directory, token)
    except (ValueError, RuntimeError, OSError) as exc:
        print("Credential import failed:", type(exc).__name__, file=sys.stderr)
        return 1
    print("Encrypted credential installed:", target)
    print("No service was changed. Review LoadCredentialEncrypted before deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
