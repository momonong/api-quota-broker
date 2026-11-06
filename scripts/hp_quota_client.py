"""Candidate HP-only publisher: offline packages, scoped Doppler, durable intent.

Requires Home's dedicated forced-command helper to be installed first. This is
not a bootstrap tool and never falls back to static deployment or sudo.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import resource
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from build_asus_release import ASSET_FILES, SOURCE_FILES

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/hp-quota-maintenance"
PROJECT, CONFIG = "selfhost-deploy", "prd_quota_hp"
SECRET = "QUOTA_DEPLOY_SSH_PRIVATE_KEY"
META = (
    "accounts.json",
    "gateway.json",
    "provider-observations.json",
    "registry.json",
    "reviewed.json",
)
PATHS = tuple(
    sorted(
        [p.replace("src/", "app/", 1) for p in SOURCE_FILES + ASSET_FILES]
        + ["metadata/" + p for p in META]
    )
)
MAX_FILE, MAX_TOTAL, MAX_JSON = 4 * 1024**2, 12 * 1024**2, 20 * 1024**2
HASH = re.compile("[0-9a-f]{64}")
RELEASE = re.compile("[a-z0-9][a-z0-9_-]{0,95}")


class Refused(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise Refused(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    need(len(raw) <= MAX_JSON, "json_too_large")
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise Refused("invalid_json") from None


def read_file(path, maximum, private=False):
    path = Path(path).absolute()
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), "unsafe_parent")
    if private:
        info = path.parent.lstat()
        need(info.st_uid == os.getuid() and not info.st_mode & 0o077, "private_directory_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
            "unsafe_file",
        )
        if private:
            need(
                before.st_uid == os.getuid() and not before.st_mode & 0o077, "private_file_required"
            )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        need(
            len(raw) <= maximum
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "file_changed",
        )
    return raw


def validate(payload):
    need(
        isinstance(payload, dict)
        and set(payload)
        == {
            "schema",
            "operation_id",
            "release_id",
            "expected_current",
            "expected_manifest_sha256",
            "files",
        },
        "payload_fields",
    )
    need(payload["schema"] == "hp-quota-release-v1", "schema")
    try:
        need(str(uuid.UUID(payload["operation_id"])) == payload["operation_id"], "operation_id")
    except (ValueError, TypeError, AttributeError):
        raise Refused("operation_id") from None
    for name in ("release_id", "expected_current"):
        need(isinstance(payload[name], str) and RELEASE.fullmatch(payload[name]), "release_id")
    need(payload["release_id"] != payload["expected_current"], "same_release")
    need(
        isinstance(payload["expected_manifest_sha256"], str)
        and HASH.fullmatch(payload["expected_manifest_sha256"]),
        "expected_manifest",
    )
    files = payload["files"]
    need(isinstance(files, list) and 0 < len(files) <= 128, "file_count")
    manifest, paths, total = [], [], 0
    for item in files:
        need(
            isinstance(item, dict) and set(item) == {"path", "size", "sha256", "content_base64"},
            "file_fields",
        )
        need(
            item["path"] in PATHS and type(item["size"]) is int and 0 <= item["size"] <= MAX_FILE,
            "file_scope",
        )
        need(
            isinstance(item["content_base64"], str)
            and len(item["content_base64"]) <= ((MAX_FILE + 2) // 3) * 4,
            "file_size",
        )
        try:
            raw = base64.b64decode(item["content_base64"], validate=True)
        except ValueError:
            raise Refused("invalid_base64") from None
        need(len(raw) == item["size"] and digest(raw) == item["sha256"], "file_digest")
        total += len(raw)
        paths.append(item["path"])
        manifest.append({key: item[key] for key in ("path", "size", "sha256")})
    need(tuple(paths) == PATHS and total <= MAX_TOTAL, "exact_file_set")
    need(len(canonical(payload)) <= MAX_JSON, "json_too_large")
    return {
        "manifest_sha256": digest(canonical(manifest)),
        "files": len(files),
        "decoded_bytes": total,
    }


def build(metadata, release_id, expected_current, expected_manifest, operation_id):
    files = []
    for path in PATHS:
        source = (
            (Path(metadata) / path.removeprefix("metadata/"))
            if path.startswith("metadata/")
            else ROOT / path.replace("app/", "src/", 1)
        )
        raw = read_file(source, MAX_FILE)
        files.append(
            {
                "path": path,
                "size": len(raw),
                "sha256": digest(raw),
                "content_base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    payload = {
        "schema": "hp-quota-release-v1",
        "operation_id": operation_id,
        "release_id": release_id,
        "expected_current": expected_current,
        "expected_manifest_sha256": expected_manifest,
        "files": files,
    }
    validate(payload)
    return payload


def write_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def credentials():
    """One config service token, fresh Doppler fetch, no ambient credentials/cache."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    token = read_file(STATE / "doppler-token", 128 * 1024, private=True).decode().strip()
    need(token.startswith("dp.st."), "config_service_token_required")
    with tempfile.TemporaryDirectory(prefix="quota-doppler-") as directory:
        result = subprocess.run(
            [
                str(Path.home() / ".local/bin/doppler"),
                "secrets",
                "download",
                "--project",
                PROJECT,
                "--config",
                CONFIG,
                "--format",
                "json",
                "--no-file",
                "--no-fallback",
                "--no-cache",
                "--no-check-version",
                "--attempts",
                "1",
                "--timeout",
                "15s",
                "--config-dir",
                directory,
            ],
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "DOPPLER_TOKEN": token},
            capture_output=True,
            timeout=25,
            check=False,
        )
    need(result.returncode == 0, "doppler_fetch_failed")
    data = parse(result.stdout)
    need(
        isinstance(data, dict)
        and data.get("DOPPLER_PROJECT") == PROJECT
        and data.get("DOPPLER_CONFIG") == CONFIG,
        "doppler_scope_mismatch",
    )
    need(
        {k for k in data if not k.startswith("DOPPLER_")} == {SECRET}, "doppler_secret_set_mismatch"
    )
    need(
        isinstance(data[SECRET], str)
        and data[SECRET].startswith("-----BEGIN OPENSSH PRIVATE KEY-----"),
        "ssh_key_type",
    )
    return data[SECRET]


def remote(request):
    key = credentials()
    runtime = Path("/run/user") / str(os.getuid())
    info = runtime.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "private_runtime_required",
    )
    with tempfile.TemporaryDirectory(prefix="quota-ssh-", dir=runtime) as directory:
        path = Path(directory) / "identity"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(key)
            # Secret storage may omit the PEM final newline; OpenSSH requires it.
            if not key.endswith("\n"):
                stream.write("\n")
        result = subprocess.run(
            [
                "/usr/bin/ssh",
                "-F",
                "/dev/null",
                "-T",
                "-p",
                "22",
                "-i",
                str(path),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ConnectTimeout=10",
                "quota-deploy@192.168.68.67",
            ],
            input=canonical(request),
            capture_output=True,
            timeout=180,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        )
    need(result.returncode == 0, "transport_unknown")
    need(len(result.stdout) <= 65536, "response_too_large")
    response = parse(result.stdout)
    need(isinstance(response, dict), "invalid_response")
    return response


def envelope(action, **fields):
    return {"schema": "hp-quota-maintenance-v1", "action": action, **fields}


@contextmanager
def state_lock(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "private_directory_required",
    )
    fd = os.open(state / "maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == os.getuid()
            and not info.st_mode & 0o077,
            "unsafe_lock",
        )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def publish(payload, state=STATE, transport=remote):
    validate(payload)
    with state_lock(state):
        return _publish(payload, state, transport)


def _publish(payload, state, transport):
    proof = validate(payload)
    operation = payload["operation_id"]
    # An unknown operation cannot be bypassed by generating another UUID.
    for pending in state.glob("*.intent.json"):
        result_path = pending.with_name(pending.name.replace(".intent.json", ".result.json"))
        reconciled = pending.with_name(pending.name.replace(".intent.json", ".reconciled.json"))
        if reconciled.exists():
            result_path = reconciled
        need(result_path.exists(), "inspect_original_receipt_do_not_replay")
        prior = parse(read_file(result_path, 65536, private=True))
        intent = parse(read_file(pending, 65536, private=True))
        need(
            isinstance(prior, dict)
            and isinstance(intent, dict)
            and prior.get("status") in {"complete", "rolled_back"}
            and all(
                prior.get(k) == intent.get(k) and prior.get(k) is not None
                for k in ("operation_id", "release_id", "manifest_sha256")
            ),
            "inspect_original_receipt_do_not_replay",
        )
    # Any previous intent, including ambiguous transport, requires status only.
    need(
        not (state / (operation + ".intent.json")).exists(),
        "inspect_original_receipt_do_not_replay",
    )
    status = transport(envelope("status"))
    need(
        status.get("current") == payload["expected_current"]
        and status.get("manifest_sha256") == payload["expected_manifest_sha256"],
        "baseline_changed",
    )
    validated = transport(envelope("validate", payload=payload))
    need(
        validated.get("status") == "validated"
        and validated.get("manifest_sha256") == proof["manifest_sha256"],
        "remote_validation_failed",
    )
    write_once(
        state / (operation + ".intent.json"),
        {
            "operation_id": operation,
            "release_id": payload["release_id"],
            "manifest_sha256": proof["manifest_sha256"],
            "status": "outcome_unknown",
            "expected_current": payload["expected_current"],
            "expected_manifest_sha256": payload["expected_manifest_sha256"],
        },
    )
    result = transport(envelope("publish", payload=payload))
    need(result.get("operation_id") == operation, "receipt_mismatch")
    # Only authenticated readback proves completion; never trust exit status.
    receipt = transport(envelope("status", operation_id=operation))
    intent = parse(read_file(state / (operation + ".intent.json"), 65536, private=True))
    outcome = terminal_result(intent, receipt)
    write_once(state / (operation + ".result.json"), outcome)
    need(outcome["status"] == "complete", "deployment_rolled_back")
    return {
        "status": "complete",
        "operation_id": operation,
        "active": receipt.get("current") == payload["release_id"]
        and receipt.get("manifest_sha256") == proof["manifest_sha256"],
        **proof,
    }


def terminal_result(intent, receipt):
    need(
        isinstance(intent, dict)
        and isinstance(receipt, dict)
        and receipt.get("status") in {"complete", "rolled_back"}
        and receipt.get("operation_id") == intent.get("operation_id")
        and receipt.get("release_id") == intent.get("release_id")
        and receipt.get("release_manifest_sha256") == intent.get("manifest_sha256"),
        "reconcile_required",
    )
    if receipt["status"] == "rolled_back":
        need(
            intent.get("expected_current") is not None
            and receipt.get("previous") == intent["expected_current"]
            and receipt.get("previous_manifest_sha256") == intent.get("expected_manifest_sha256"),
            "reconcile_required",
        )
    return {
        "operation_id": intent["operation_id"],
        "status": receipt["status"],
        "manifest_sha256": intent["manifest_sha256"],
        "release_id": intent["release_id"],
    }


def reconcile(operation, state=STATE, transport=remote):
    need(str(uuid.UUID(operation)) == operation, "operation_id")
    with state_lock(state):
        intent = parse(read_file(state / (operation + ".intent.json"), 65536, private=True))
        receipt = transport(envelope("status", operation_id=operation))
        need(intent.get("operation_id") == operation, "intent_mismatch")
        result = terminal_result(intent, receipt)
        path = state / (operation + ".reconciled.json")
        if path.exists():
            need(parse(read_file(path, 65536, private=True)) == result, "reconciliation_drift")
        else:
            write_once(path, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify", "status", "publish", "reconcile"))
    parser.add_argument("--package", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--expected-current")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    operation = None
    try:
        if args.action == "build":
            need(args.package is not None and args.metadata is not None, "paths_required")
            payload = build(
                args.metadata,
                args.release_id,
                args.expected_current,
                args.expected_manifest_sha256,
                args.operation_id or str(uuid.uuid4()),
            )
            write_once(args.package, payload)
            result = {
                "status": "offline_package_built",
                "operation_id": payload["operation_id"],
                **validate(payload),
            }
        elif args.action == "reconcile":
            result = reconcile(args.operation_id)
        elif args.action == "status":
            if args.operation_id:
                need(str(uuid.UUID(args.operation_id)) == args.operation_id, "operation_id")
            result = remote(
                envelope(
                    "status", **({"operation_id": args.operation_id} if args.operation_id else {})
                )
            )
            result = {
                k: result[k]
                for k in (
                    "status",
                    "operation_id",
                    "current",
                    "manifest_sha256",
                    "release_id",
                    "release_manifest_sha256",
                    "previous",
                    "previous_manifest_sha256",
                )
                if k in result
                and isinstance(result[k], str)
                and re.fullmatch("[a-zA-Z0-9_-]{1,128}", result[k])
            }
        else:
            need(args.package is not None, "package_required")
            payload = parse(read_file(args.package, MAX_JSON))
            validate(payload)
            operation = payload["operation_id"]
            result = (
                {"status": "offline_validated", **validate(payload)}
                if args.action == "verify"
                else publish(payload)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "automatic_retry": False,
                    "reason": str(exc)
                    if isinstance(exc, Refused)
                    else "inspect_configuration_or_original_receipt",
                    "operation_id": operation,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
