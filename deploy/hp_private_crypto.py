"""Fixed HP key maintenance; systemd supplies host-encrypted credentials.

No network, provider keys, Doppler, API calls, or GPU. The root credential is
loaded only by delegation renewal; the five-minute publisher receives a signer.
"""

import contextlib
import fcntl
import io
import json
import os
import resource
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

STATE = Path("/var/lib/quota-private-crypto")
RUN = Path("/run/quota-private-crypto")


def invoke(arguments):
    from quota_broker.crypto_admin import main

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if main([str(a) for a in arguments]) != 0:
            raise ValueError("key_maintenance_failed")


def delegate(state, root_key):
    from quota_broker.crypto_admin import _replace

    with tempfile.TemporaryDirectory(prefix="delegation-", dir=state) as temporary:
        candidate = Path(temporary) / "delegation.jws"
        invoke(
            [
                "delegate",
                "--root-key",
                root_key,
                "--root-id",
                "hp-root-1",
                "--signer-public",
                state / "signer.pub",
                "--signer-id",
                "hp-config-1",
                "--profile",
                state / "profile.json",
                "--out",
                candidate,
                "--days",
                "30",
            ]
        )
        _replace(state / "delegation.jws", candidate.read_bytes())


def publish(state, signer_key, *, recipients_directory=None, persist=None):
    from quota_broker.crypto_admin import _b64, _replace
    from quota_broker.crypto_config import private_file
    from quota_broker.encrypted_transport import KeyRing

    database, manifest = state / "publisher.sqlite", state / "manifest.json"
    if manifest.exists() and not database.exists():
        raise ValueError("publisher_state_missing")
    version = 1
    if database.exists():
        private_file(database)
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT version FROM trust WHERE id=1").fetchone()
            if row is None:
                raise ValueError("publisher_state_invalid")
            version = row[0] + 1
    recipients = (recipients_directory or state) / "recipients.json"
    public = state / "recipient-public.json"
    now = int(time.time())
    ring = KeyRing.load(recipients) if recipients.exists() else None
    current = max(ring.keys, key=lambda k: k.not_before) if ring else None
    if current is None or current.encrypt_until - now <= 1200:
        used = {k.key_id for k in ring.keys if k.encrypt_until + 1200 > now} if ring else set()
        next_id = next(i for i in range(256) if i not in used)
        invoke(["recipient", "--out", recipients, "--key-id", next_id, "--public", public])
    else:
        # Rebuild public bytes from the private keyring after a partial publication.
        _replace(
            public,
            json.dumps(
                {
                    "config": _b64(ring.config()),
                    "not_before": current.not_before,
                    "encrypt_until": current.encrypt_until,
                }
            ).encode(),
        )
    if persist is not None:
        persist(recipients.read_bytes())  # durable encrypted copy before exposing the manifest
    invoke(
        [
            "publish",
            "--signer-key",
            signer_key,
            "--delegation",
            state / "delegation.jws",
            "--profile",
            state / "profile.json",
            "--recipient-config",
            public,
            "--out",
            manifest,
            "--state",
            database,
            "--version",
            version,
        ]
    )


def publish_encrypted(state, runtime, signer_key, storage_key):
    from cryptography.fernet import Fernet

    from quota_broker.crypto_admin import _replace
    from quota_broker.crypto_config import private_file

    cipher = Fernet(storage_key)
    stored, clear = state / "recipients.fernet", runtime / "recipients.json"
    if stored.exists():
        private_file(stored)
        _replace(clear, cipher.decrypt(stored.read_bytes()))
    elif (state / "manifest.json").exists() or clear.exists():
        raise ValueError("recipient_storage_missing")
    publish(
        state,
        signer_key,
        recipients_directory=runtime,
        persist=lambda raw: _replace(stored, cipher.encrypt(raw)),
    )


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in {"delegate", "publish"} or os.geteuid() == 0:
        raise ValueError("fixed_unprivileged_operation_required")
    sys.path[:0] = ["/opt/quota-private-crypto/app", "/opt/quota-private-crypto/site"]
    action = sys.argv[1]
    key_name = "root.pem" if action == "delegate" else "signer.pem"
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / key_name
    raw = credential.read_bytes()
    if not 0 < len(raw) <= 4096:
        raise ValueError("credential_size")
    with (STATE / "maintenance.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix="key-", dir=RUN) as temporary:
            path = Path(temporary) / key_name
            path.write_bytes(raw)
            path.chmod(0o600)
            if action == "delegate":
                delegate(STATE, path)
            else:
                storage = (credential.parent / "storage.key").read_bytes()
                if len(storage) != 44:
                    raise ValueError("credential_size")
                publish_encrypted(STATE, RUN, path, storage)
    print(json.dumps({"operation": action, "status": "complete"}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - no secret-bearing errors in systemd journal
        print('{"status":"stopped","reason":"key_maintenance_failed"}', flush=True)
        raise SystemExit(1) from None
