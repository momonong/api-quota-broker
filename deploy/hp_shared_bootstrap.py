"""Reviewed first-install gate for HP's separate shared Broker.

No application code is imported as root. Payload and launcher pins are supplied
by the reviewed caller. Credentials arrive only on stdin and are encrypted with
HP's systemd host key. The fixed receipt prevents replay after any partial apply.
This gate does not change Caddy, the dashboard, Receipts, sudoers or SSH policy.
"""

import argparse
import base64
import binascii
import fcntl
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import resource
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROVIDER_REFS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "ocrspace": "OCRSPACE_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "huggingface": "HUGGINGFACE_API_TOKEN",
    "cerebras": "CEREBRAS_API_KEY",
}
LEGACY_PROVIDER_NAMES = frozenset({"OPENAI_API_KEY", "OPENROUTER_API_KEY"})
PROVIDER_NAMES = frozenset(PROVIDER_REFS.values()) | {"CLOUDFLARE_ACCOUNT_ID"}

INSTANCE = "hp-shared-20261111"
SERVICE = "quota-shared-broker.service"
IDENTITY = "quota-shared-broker"
BASE = Path("/opt/quota-shared-broker")
STATE = Path("/var/lib/quota-shared-broker")
RECEIPT = Path("/var/lib/quota-shared-bootstrap-20261111.json")
LOCK = Path("/run/quota-shared-bootstrap.lock")
LAUNCHER = Path("/usr/local/libexec/quota-shared-runtime.py")
UNIT = Path("/etc/systemd/system/quota-shared-broker.service")
CREDENTIAL = Path("/etc/credstore.encrypted/quota-shared-broker-runtime")
MAX_FILE, MAX_TOTAL, MAX_INPUT = 4 * 1024**2, 12 * 1024**2, 20 * 1024**2
MODULES = (
    "__init__",
    "access",
    "bounded_curl",
    "catalog",
    "cli",
    "client",
    "client_credentials",
    "config",
    "core",
    "crypto_admin",
    "crypto_config",
    "dashboard",
    "dashboard_accounts",
    "dashboard_auth",
    "dashboard_demo",
    "dashboard_provider_metadata",
    "task_matching",
    "dashboard_reviewed",
    "dashboard_server",
    "discovery",
    "discovery_parsers",
    "discovery_sources",
    "encrypted_transport",
    "execution_policy",
    "families",
    "family_adapters",
    "family_transport",
    "gateway",
    "gateway_providers",
    "gateway_server",
    "key_admin",
    "local_jobs",
    "nvidia",
    "nvidia_server",
    "provider_policy",
    "queue",
    "registry",
    "retry",
    "routing",
    "secure_client",
    "server",
    "token_preflight",
)
CONFIGS = (
    "registry.json",
    "gateway.json",
    "access.json",
    "observations.json",
    "secret-inventory.json",
)
PATHS = tuple(
    sorted(
        ["app/quota_broker/" + name + ".py" for name in MODULES]
        + [
            "app/quota_broker/dashboard_assets/" + name
            for name in ("index.html", "dashboard.css", "dashboard.js", "provider-inventory.json")
        ]
        + ["config/" + name for name in CONFIGS]
    )
)
UNIT_TEXT = """[Unit]
Description=Private shared API Quota Broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=quota-shared-broker
Group=quota-shared-broker
WorkingDirectory=/var/lib/quota-shared-broker
StateDirectory=quota-shared-broker
StateDirectoryMode=0700
RuntimeDirectory=quota-shared-broker
RuntimeDirectoryMode=0700
UMask=0077
ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/quota-shared-runtime.py
LoadCredentialEncrypted=runtime.json:/etc/credstore.encrypted/quota-shared-broker-runtime
Restart=no
TimeoutStopSec=180
KillSignal=SIGTERM
MemoryMax=384M
MemorySwapMax=0
CPUQuota=75%
TasksMax=32
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET
SocketBindDeny=any
SocketBindAllow=ipv4:tcp:18587
SocketBindAllow=ipv4:tcp:18588

[Install]
WantedBy=multi-user.target
"""


class Refused(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise Refused(reason)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, "duplicate_json")
        value[key] = item
    return value


def parse(raw):
    need(0 < len(raw) <= MAX_INPUT, "input_bound")
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise Refused("invalid_json") from None


def decode_blob(row):
    need(isinstance(row, dict) and set(row) == {"size", "sha256", "content_base64"}, "blob_fields")
    need(type(row["size"]) is int and 0 <= row["size"] <= MAX_FILE, "file_bound")
    need(
        isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "file_hash"
    )
    need(
        isinstance(row["content_base64"], str)
        and len(row["content_base64"]) <= ((MAX_FILE + 2) // 3) * 4,
        "base64_bound",
    )
    try:
        raw = base64.b64decode(row["content_base64"], validate=True)
    except (ValueError, binascii.Error):
        raise Refused("invalid_base64") from None
    need(len(raw) == row["size"] and digest(raw) == row["sha256"], "file_digest")
    return raw


def validate_payload(value, payload_pin, launcher_pin):
    need(
        isinstance(value, dict)
        and set(value)
        == {"schema", "operation_id", "release_id", "instance_id", "launcher", "files"},
        "payload_fields",
    )
    need(
        value["schema"] == "hp-quota-shared-bootstrap-v1" and value["instance_id"] == INSTANCE,
        "fixed_instance",
    )
    need(
        isinstance(payload_pin, str)
        and re.fullmatch(r"[0-9a-f]{64}", payload_pin)
        and hmac.compare_digest(digest(canonical(value)), payload_pin),
        "payload_pin",
    )
    try:
        need(str(uuid.UUID(value["operation_id"])) == value["operation_id"], "operation_id")
    except (ValueError, TypeError, AttributeError):
        raise Refused("operation_id") from None
    need(
        isinstance(value["release_id"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", value["release_id"]),
        "release_id",
    )
    launcher = decode_blob(value["launcher"])
    need(
        isinstance(launcher_pin, str)
        and re.fullmatch(r"[0-9a-f]{64}", launcher_pin)
        and hmac.compare_digest(digest(launcher), launcher_pin),
        "launcher_pin",
    )
    rows = value["files"]
    need(isinstance(rows, list) and len(rows) == len(PATHS), "exact_file_set")
    files, names = {}, []
    for row in rows:
        need(
            isinstance(row, dict) and set(row) == {"path", "size", "sha256", "content_base64"},
            "file_fields",
        )
        path = row["path"]
        need(isinstance(path, str) and path in PATHS and path not in files, "file_scope")
        names.append(path)
        files[path] = decode_blob({k: row[k] for k in ("size", "sha256", "content_base64")})
    need(tuple(names) == PATHS and sum(map(len, files.values())) <= MAX_TOTAL, "exact_file_set")
    # Validate JSON syntax without importing or evaluating package code.
    for name in CONFIGS:
        parse(files["config/" + name])
    return {"files": files, "launcher": launcher, "payload_sha256": payload_pin}


def validate_credentials(value, read_token, access, *, gateway=None):
    need(
        isinstance(value, dict)
        and set(value)
        == {"schema", "instance_id", "digest_key", "queue_key", "legacy_token", "provider_keys"}
        and type(value["schema"]) is int
        and value["schema"] == 1
        and value["instance_id"] == INSTANCE,
        "credential_instance",
    )
    required_names = set(LEGACY_PROVIDER_NAMES)
    if gateway is not None:
        need(
            isinstance(gateway, dict) and isinstance(gateway.get("targets"), list),
            "gateway_targets",
        )
        required_names = set()
        for target in gateway["targets"]:
            need(isinstance(target, dict) and type(target.get("enabled")) is bool, "target_enabled")
            if not target["enabled"]:
                continue
            provider = target.get("provider")
            need(
                isinstance(provider, str)
                and provider in PROVIDER_REFS
                and target.get("secret_ref") == PROVIDER_REFS[provider]
                and target.get("account_id_ref")
                == ("CLOUDFLARE_ACCOUNT_ID" if provider == "cloudflare" else None),
                "enabled_secret_reference",
            )
            required_names.add(PROVIDER_REFS[provider])
            if provider == "cloudflare":
                required_names.add("CLOUDFLARE_ACCOUNT_ID")
    keys = value["provider_keys"]
    need(
        isinstance(keys, dict)
        and (
            set(keys) == required_names
            or (set(keys) == LEGACY_PROVIDER_NAMES and required_names <= LEGACY_PROVIDER_NAMES)
        ),
        "credential_scope",
    )
    need(
        all(
            isinstance(v, str)
            and 8 <= len(v) <= 1024
            and v.isascii()
            and not any(c.isspace() for c in v)
            for v in keys.values()
        ),
        "credential_format",
    )
    try:
        key = base64.b64decode(value["digest_key"], validate=True)
        queue_key = base64.b64decode(value["queue_key"], validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise Refused("credential_key") from None
    need(32 <= len(key) <= 64 and len(queue_key) == 32, "credential_key")
    need(
        isinstance(value["legacy_token"], str)
        and re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", value["legacy_token"]),
        "credential_token",
    )
    need(
        isinstance(read_token, str) and re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", read_token),
        "read_token",
    )
    need(
        isinstance(access, dict)
        and set(access) == {"version", "principals"}
        and access["version"] == 1
        and isinstance(access["principals"], list),
        "access_policy",
    )
    match = [
        p
        for p in access["principals"]
        if isinstance(p, dict) and p.get("token_sha256") == digest(read_token.encode())
    ]
    need(
        len(match) == 1
        and match[0].get("project_id") is None
        and isinstance(match[0].get("scopes"), list)
        and sorted(match[0]["scopes"]) == ["plan", "read"],
        "read_only_principal",
    )
    material = b"quota-shared-ledger-binding-v1\x00"
    for part in (INSTANCE.encode("ascii"), key, queue_key):
        material += len(part).to_bytes(4, "big") + part
    return digest(material)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_exclusive(path, raw, *, uid=0, gid=0, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, gid)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def update_receipt(value):
    temporary = RECEIPT.with_name(RECEIPT.name + ".next")
    write_exclusive(temporary, canonical(value))
    os.replace(temporary, RECEIPT)
    sync_directory(RECEIPT.parent)


def root_directory(path):
    for item in (path, *path.parents):
        info = item.lstat()
        need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            "root_directory",
        )


def command(args, *, input_data=None, timeout=30):
    result = subprocess.run(
        args,
        input=input_data,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    need(result.returncode == 0, "host_command_failed")
    return result.stdout


def absent(path):
    need(not os.path.lexists(path), "existing_state_requires_reconcile")


def preflight():
    for parent in (BASE.parent, STATE.parent, LAUNCHER.parent, UNIT.parent):
        root_directory(parent)
    for path in (BASE, STATE, LAUNCHER, UNIT, CREDENTIAL, RECEIPT):
        absent(path)
    if CREDENTIAL.parent.exists():
        root_directory(CREDENTIAL.parent)
    else:
        root_directory(CREDENTIAL.parent.parent)
    try:
        pwd.getpwnam(IDENTITY)
    except KeyError:
        pass
    else:
        raise Refused("existing_identity_requires_reconcile")
    try:
        grp.getgrnam(IDENTITY)
    except KeyError:
        pass
    else:
        raise Refused("existing_identity_requires_reconcile")
    for port in (18587, 18588):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    need(Path("/usr/bin/systemd-creds").is_file(), "credential_tool_missing")
    root_directory(Path("/opt/momonong-quota-readonly/site"))


def directory(path, uid, gid, mode):
    path.mkdir(mode=mode)
    os.chown(path, uid, gid)
    os.chmod(path, mode)
    sync_directory(path.parent)


def health(read_token):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, token=None, port=18587):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}" + path,
            headers={"Authorization": "Bearer " + token} if token else {},
        )
        try:
            with opener.open(request, timeout=3) as response:
                body = response.read(1024 * 1024 + 1)
                need(len(body) <= 1024 * 1024, "health_response_bound")
                return response.status, parse(body)
        except urllib.error.HTTPError as error:
            return error.code, None

    last_error = None
    for _attempt in range(40):
        try:
            for endpoint, kind in (
                ("/v1/catalog", list),
                ("/v1/diagnostics", dict),
                ("/v1/usage", list),
            ):
                status, body = get(endpoint, read_token)
                need(status == 200 and isinstance(body, kind), "metadata_health")
            need(get("/v1/catalog")[0] == 401, "anonymous_metadata")
            need(get("/quota/api/v1/jobs", port=18588)[0] == 401, "anonymous_private")
            need(get("/quota/api/v1/jobs", read_token, 18588)[0] == 403, "website_cannot_execute")
            return {
                "catalog": 200,
                "diagnostics": 200,
                "usage": 200,
                "anonymous": 401,
                "website_private_jobs": 403,
            }
        except (OSError, ValueError, TimeoutError):
            last_error = "runtime_not_ready"
            time.sleep(1)
    raise Refused(last_error or "runtime_not_ready")


def apply(value, proof, credential, read_token, binding):
    preflight()
    record = {
        "schema": 1,
        "operation_id": value["operation_id"],
        "instance_id": INSTANCE,
        "release_id": value["release_id"],
        "payload_sha256": proof["payload_sha256"],
        "status": "claimed",
        "phase": "pre_mutation",
        "provider_calls": 0,
        "historical_gap": True,
        "automatic_retry": False,
    }
    write_exclusive(RECEIPT, canonical(record))
    try:
        command(
            [
                "/usr/sbin/useradd",
                "--system",
                "--user-group",
                "--home-dir",
                str(STATE),
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                IDENTITY,
            ]
        )
        account = pwd.getpwnam(IDENTITY)
        uid, gid = account.pw_uid, account.pw_gid
        need(uid != 0 and gid != 0, "service_identity")
        record.update(phase="release")
        update_receipt(record)
        directory(BASE, 0, gid, 0o750)
        directory(BASE / "releases", 0, gid, 0o750)
        release = BASE / "releases" / value["release_id"]
        directory(release, 0, gid, 0o750)
        for name, raw in proof["files"].items():
            path = release / name
            missing = []
            parent = path.parent
            while parent != release and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                directory(parent, 0, gid, 0o750)
            write_exclusive(path, raw, gid=gid, mode=0o640)
        manifest = [
            {"path": name, "size": len(raw), "sha256": digest(raw)}
            for name, raw in proof["files"].items()
        ]
        write_exclusive(release / "manifest.json", canonical(manifest), gid=gid, mode=0o640)
        write_exclusive(LAUNCHER, proof["launcher"], gid=gid, mode=0o640)
        write_exclusive(UNIT, UNIT_TEXT.encode(), mode=0o644)
        directory(STATE, uid, gid, 0o700)
        marker = {
            "schema": 1,
            "instance_id": INSTANCE,
            "historical_gap": True,
            "history_start": datetime.now(UTC).isoformat(),
            "credential_fingerprint": binding,
        }
        write_exclusive(STATE / "instance.json", canonical(marker), uid=uid, gid=gid)
        # Explicit first-install ledger, never a replacement for a missing ledger.
        write_exclusive(STATE / "ledger.sqlite3", b"", uid=uid, gid=gid)
        with sqlite3.connect(STATE / "ledger.sqlite3") as database:
            need(database.execute("PRAGMA quick_check").fetchone()[0] == "ok", "initial_ledger")
        if CREDENTIAL.parent.exists():
            root_directory(CREDENTIAL.parent)
        else:
            directory(CREDENTIAL.parent, 0, 0, 0o700)
        record.update(phase="credential_encrypt")
        update_receipt(record)
        encrypted = command(
            [
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                "--name=runtime.json",
                "-",
                "-",
            ],
            input_data=canonical(credential),
        )
        need(0 < len(encrypted) <= 65536, "encrypted_credential_bound")
        write_exclusive(CREDENTIAL, encrypted)
        os.symlink("releases/" + value["release_id"], BASE / "current")
        sync_directory(BASE)
        record.update(phase="starting")
        update_receipt(record)
        command(["/usr/bin/systemctl", "daemon-reload"])
        command(["/usr/bin/systemctl", "start", SERVICE])
        checks = health(read_token)
        command(["/usr/bin/systemctl", "enable", SERVICE])
        record.update(status="complete", phase="complete", metadata_checks=checks)
        update_receipt(record)
        return record
    except BaseException:  # noqa: BLE001 - fixed recovery boundary, no exception reflection
        # Only the newly named service may be stopped; data and claim survive.
        subprocess.run(
            ["/usr/bin/systemctl", "stop", SERVICE], capture_output=True, timeout=185, check=False
        )
        record.update(status="needs_reconcile", phase=record["phase"])
        update_receipt(record)
        return record


def read_status():
    if not RECEIPT.exists():
        return {"status": "not_started", "instance_id": INSTANCE, "automatic_retry": False}
    fd = os.open(RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "receipt_permissions",
        )
        raw = stream.read(65537)
        need(len(raw) <= 65536, "receipt_bound")
        return parse(raw)


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.alarm(240)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--payload-sha256")
    parser.add_argument("--launcher-sha256")
    args = parser.parse_args()
    if args.status:
        need(os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server", "fixed_hp_root")
        return read_status()
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    envelope = parse(raw)
    need(
        isinstance(envelope, dict)
        and set(envelope) == {"payload", "runtime_credential", "read_token"},
        "envelope_fields",
    )
    value = envelope["payload"]
    proof = validate_payload(value, args.payload_sha256, args.launcher_sha256)
    binding = validate_credentials(
        envelope["runtime_credential"],
        envelope["read_token"],
        parse(proof["files"]["config/access.json"]),
        gateway=parse(proof["files"]["config/gateway.json"]),
    )
    if args.validate:
        host_preflight = os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server"
        if host_preflight:
            preflight()
        return {
            "status": "validated",
            "operation_id": value["operation_id"],
            "release_id": value["release_id"],
            "files": len(PATHS),
            "provider_calls": 0,
            "host_preflight": host_preflight,
        }
    need(os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server", "fixed_hp_root")
    root_directory(LOCK.parent)
    fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as lock:
        info = os.fstat(lock.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "lock_permissions",
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return apply(value, proof, envelope["runtime_credential"], envelope["read_token"], binding)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception:  # noqa: BLE001 - final credential redaction boundary
        # Never reflect supplied secrets, parser values, subprocess stderr or tracebacks.
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "reason": "inspect_original_root_receipt",
                    "instance_id": INSTANCE,
                    "automatic_retry": False,
                    "provider_calls": 0,
                }
            )
        )
        raise SystemExit(1) from None
