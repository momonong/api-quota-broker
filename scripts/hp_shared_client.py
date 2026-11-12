"""Fixed HP shared-Broker publisher; no sudo, shell, provider calls or retries."""

import argparse
import base64
import importlib.util
import json
import os
import resource
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import quota_shared_credentials as credentials
from build_shared_runtime_config import validate_runtime_configuration
from hp_quota_client import (
    need,
    read_file,
    state_lock,
    terminal_result,
    write_once,
)

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/hp-shared-maintenance"
RUNTIME = Path("/run/user") / str(os.getuid())
MAINTENANCE_KEY = "QUOTA_SHARED_MAINTENANCE_SSH_PRIVATE_KEY"
TUNNEL_KEY = "QUOTA_SHARED_TUNNEL_SSH_PRIVATE_KEY"
SPEC = importlib.util.spec_from_file_location(
    "shared_maintenance_contract", ROOT / "deploy/hp_shared_maintenance.py"
)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
PATHS, MAX_FILE, MAX_JSON = CONTRACT.ALLOWED, CONTRACT.MAX_FILE, CONTRACT.MAX_JSON
canonical, parse, digest = CONTRACT.canonical, CONTRACT.parse, CONTRACT.digest


def validate(payload):
    manifest, files = CONTRACT.validate(payload)
    validate_runtime_configuration(
        {Path(path).name: parse(raw) for path, raw in files.items() if path.startswith("config/")}
    )
    return {
        "manifest_sha256": digest(canonical(manifest)),
        "files": len(files),
        "decoded_bytes": sum(map(len, files.values())),
    }


def build(configuration, release_id, expected_current, expected_manifest, operation_id):
    files = []
    for path in PATHS:
        source = (
            Path(configuration) / Path(path).name
            if path.startswith("config/")
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
        "schema": "hp-quota-shared-release-v1",
        "operation_id": operation_id,
        "release_id": release_id,
        "expected_current": expected_current,
        "expected_manifest_sha256": expected_manifest,
        "files": files,
    }
    need(len(canonical(payload)) <= MAX_JSON, "package_bound")
    validate(payload)
    return payload


@contextmanager
def private_identity(name):
    need(name in {MAINTENANCE_KEY, TUNNEL_KEY}, "fixed_identity_required")
    info = RUNTIME.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "private_runtime_required",
    )
    key = credentials.get(name)
    need(
        isinstance(key, str)
        and len(key) <= 16384
        and key.isascii()
        and key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        and key.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----"),
        "openssh_key_required",
    )
    with tempfile.TemporaryDirectory(prefix="quota-shared-", dir=RUNTIME) as directory:
        path = Path(directory) / "identity"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(key.rstrip() + "\n")
        yield path


def ssh_environment():
    return {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"}


def ssh_arguments(path, user, *, forwarding=False):
    need(user in {"quota-broker-deploy", "quota-broker-client"}, "fixed_ssh_identity")
    need(forwarding == (user == "quota-broker-client"), "ssh_purpose_mismatch")
    options = [
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "BatchMode=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "StrictHostKeyChecking=yes",
        "ForwardAgent=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "PermitLocalCommand=no",
        "ControlMaster=no",
        "ControlPath=none",
        "ConnectTimeout=10",
        "ClearAllForwardings=no" if forwarding else "ClearAllForwardings=yes",
    ]
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-T",
        "-p",
        "22",
        "-i",
        str(path),
        *[part for option in options for part in ("-o", option)],
        user + "@192.168.68.67",
    ]


def sanitize_response(value):
    allowed = {
        "status",
        "operation_id",
        "current",
        "manifest_sha256",
        "release_id",
        "release_manifest_sha256",
        "previous",
        "previous_manifest_sha256",
        "provider_calls",
        "paid_allowed",
        "automatic_retry",
        "pending_operations",
        "interrupted_receipt_write",
        "failure_phase",
        "error_code",
        "service_cleanup",
        "reason",
        "code",
    }
    need(isinstance(value, dict) and not set(value) - allowed, "invalid_response")
    need(
        value.get("status")
        in {
            "ready",
            "validated",
            "complete",
            "rolled_back",
            "reconcile_required",
            "claimed",
            "stopping",
            "switched",
            "starting",
            "rolling_back",
            "unknown",
            "stopped",
            "not_started",
        },
        "invalid_response",
    )
    if value["status"] == "not_started":
        need(
            set(value)
            == {
                "status",
                "operation_id",
                "current",
                "manifest_sha256",
                "provider_calls",
                "paid_allowed",
                "automatic_retry",
            },
            "incomplete_absence_proof",
        )
    for key, item in value.items():
        if key == "operation_id":
            CONTRACT.operation_id(item)
        elif key in {"current", "release_id", "previous"}:
            CONTRACT.identifier(item)
        elif key.endswith("sha256"):
            CONTRACT.hash_value(item)
        elif key == "pending_operations":
            need(isinstance(item, list) and len(item) <= 4096, "invalid_response")
            for operation in item:
                CONTRACT.operation_id(operation)
        elif key in {"paid_allowed", "automatic_retry"}:
            need(item is False, "invalid_response")
        elif key == "provider_calls":
            need(type(item) is int and item == 0, "invalid_response")
        elif key == "interrupted_receipt_write":
            need(type(item) is bool, "invalid_response")
        elif key != "status":
            need(isinstance(item, str) and CONTRACT.NAME.fullmatch(item), "invalid_response")
    return value


def envelope(action, **fields):
    need(action in {"status", "validate", "publish", "rollback"}, "action")
    return {"schema": "hp-quota-shared-maintenance-v1", "action": action, **fields}


def remote(request):
    if request.get("action") in {"validate", "publish"}:
        validate(request["payload"])
    with private_identity(MAINTENANCE_KEY) as path:
        result = subprocess.run(
            ssh_arguments(path, "quota-broker-deploy"),
            input=canonical(request),
            capture_output=True,
            # The fixed root helper has a 360-second deadline, including drain
            # and cleanup. Keep transport alive to receive its durable outcome.
            timeout=390,
            check=False,
            env=ssh_environment(),
        )
    need(result.returncode == 0, "transport_unknown_query_original_operation")
    need(len(result.stdout) <= 65536, "response_bound")
    return sanitize_response(parse(result.stdout))


def _intent(state, operation):
    return parse(read_file(state / (operation + ".intent.json"), 65536, private=True))


def _clear(state):
    for path in state.glob("*.intent.json"):
        stem = path.name.removesuffix(".intent.json")
        outcomes = [state / (stem + suffix) for suffix in (".reconciled.json", ".result.json")]
        existing = next((p for p in outcomes if p.exists()), None)
        need(existing is not None, "inspect_original_receipt_do_not_replay")
        intent = parse(read_file(path, 65536, private=True))
        result = parse(read_file(existing, 65536, private=True))
        need(
            result.get("status") in {"complete", "rolled_back", "not_started"}
            and all(
                result.get(k) == intent.get(k) and intent.get(k) is not None
                for k in ("operation_id", "release_id", "manifest_sha256")
            ),
            "inspect_original_receipt_do_not_replay",
        )
        if result["status"] == "not_started":
            need(
                result.get("current") == intent.get("expected_current")
                and result.get("current_manifest_sha256") == intent.get("expected_manifest_sha256")
                and intent.get("expected_current") is not None
                and intent.get("expected_manifest_sha256") is not None,
                "inspect_original_receipt_do_not_replay",
            )


def publish(payload, state=STATE, transport=remote):
    proof = validate(payload)
    operation = payload["operation_id"]
    with state_lock(state):
        _clear(state)
        need(
            not (state / (operation + ".intent.json")).exists(),
            "inspect_original_receipt_do_not_replay",
        )
        status = sanitize_response(transport(envelope("status")))
        need(
            status.get("status") == "ready"
            and status.get("current") == payload["expected_current"]
            and status.get("manifest_sha256") == payload["expected_manifest_sha256"],
            "baseline_or_pending_changed",
        )
        checked = transport(envelope("validate", payload=payload))
        need(
            checked.get("status") == "validated"
            and checked.get("manifest_sha256") == proof["manifest_sha256"],
            "remote_validation_failed",
        )
        intent = {
            k: payload[k]
            for k in ("operation_id", "release_id", "expected_current", "expected_manifest_sha256")
        }
        intent.update(status="outcome_unknown", manifest_sha256=proof["manifest_sha256"])
        write_once(state / (operation + ".intent.json"), intent)
        response = transport(envelope("publish", payload=payload))
        need(response.get("operation_id") == operation, "receipt_mismatch")
        receipt = sanitize_response(transport(envelope("status", operation_id=operation)))
        result = terminal_result(intent, receipt)
        write_once(state / (operation + ".result.json"), result)
        need(
            result["status"] == "complete"
            and receipt.get("current") == payload["release_id"]
            and receipt.get("manifest_sha256") == proof["manifest_sha256"],
            "active_readback_required",
        )
        return {**result, "active": True, **proof}


def _resolve_original(state, operation, result):
    # A confirmed rollback resolves an interrupted publish under the same ID.
    # Existing terminal proofs remain immutable; append only when none exists.
    if not any(
        (state / (operation + suffix)).exists() for suffix in (".result.json", ".reconciled.json")
    ):
        write_once(state / (operation + ".reconciled.json"), result)


def reconcile(operation, state=STATE, transport=remote):
    CONTRACT.operation_id(operation)
    with state_lock(state):
        suffix = ".rollback" if (state / (operation + ".rollback.intent.json")).exists() else ""
        intent = parse(
            read_file(state / (operation + suffix + ".intent.json"), 65536, private=True)
        )
        need(intent.get("operation_id") == operation, "intent_mismatch")
        receipt = sanitize_response(transport(envelope("status", operation_id=operation)))
        if receipt["status"] == "not_started":
            need(
                not suffix
                and receipt["operation_id"] == operation
                and receipt["current"] == intent.get("expected_current")
                and receipt["manifest_sha256"] == intent.get("expected_manifest_sha256"),
                "preclaim_baseline_or_identity_changed",
            )
            result = {
                "status": "not_started",
                "operation_id": operation,
                "release_id": intent["release_id"],
                "manifest_sha256": intent["manifest_sha256"],
                "current": receipt["current"],
                "current_manifest_sha256": receipt["manifest_sha256"],
            }
        else:
            result = terminal_result(intent, receipt)
        need(not suffix or result["status"] == "rolled_back", "rollback_reconcile_required")
        path = state / (operation + suffix + ".reconciled.json")
        if path.exists():
            need(parse(read_file(path, 65536, private=True)) == result, "reconciliation_drift")
        else:
            write_once(path, result)
        if suffix:
            _resolve_original(state, operation, result)
        return result


def rollback(operation, state=STATE, transport=remote):
    CONTRACT.operation_id(operation)
    with state_lock(state):
        intent = _intent(state, operation)
        need(intent.get("operation_id") == operation, "intent_mismatch")
        path = state / (operation + ".rollback.intent.json")
        need(not path.exists(), "inspect_original_receipt_do_not_replay")
        receipt = sanitize_response(transport(envelope("status", operation_id=operation)))
        need(
            receipt.get("operation_id") == operation
            and receipt.get("release_id") == intent["release_id"]
            and receipt.get("release_manifest_sha256") == intent["manifest_sha256"]
            and receipt.get("status") != "rolled_back",
            "receipt_mismatch",
        )
        write_once(path, {**intent, "status": "outcome_unknown"})
        transport(envelope("rollback", operation_id=operation))
        result = terminal_result(
            intent, sanitize_response(transport(envelope("status", operation_id=operation)))
        )
        need(result["status"] == "rolled_back", "rollback_reconcile_required")
        write_once(state / (operation + ".rollback.result.json"), result)
        _resolve_original(state, operation, result)
        return result


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("build", "verify", "status", "publish", "reconcile", "rollback")
    )
    parser.add_argument("--package", type=Path)
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--expected-current")
    parser.add_argument(
        "--expected-manifest", "--expected-manifest-sha256", dest="expected_manifest"
    )
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    try:
        if args.action == "build":
            need(args.package is not None and args.config_directory is not None, "paths_required")
            payload = build(
                args.config_directory,
                args.release_id,
                args.expected_current,
                args.expected_manifest,
                args.operation_id or str(uuid.uuid4()),
            )
            write_once(args.package, payload)
            result = {
                "status": "offline_package_built",
                "operation_id": payload["operation_id"],
                **validate(payload),
            }
        elif args.action == "status":
            if args.operation_id:
                CONTRACT.operation_id(args.operation_id)
            result = remote(
                envelope(
                    "status", **({"operation_id": args.operation_id} if args.operation_id else {})
                )
            )
        elif args.action in {"reconcile", "rollback"}:
            result = (reconcile if args.action == "reconcile" else rollback)(args.operation_id)
        else:
            need(args.package is not None, "package_required")
            payload = parse(read_file(args.package, MAX_JSON))
            result = (
                {"status": "offline_validated", **validate(payload)}
                if args.action == "verify"
                else publish(payload)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - never reflect credential or subprocess errors
        print(
            '{"status":"stopped","reason":"inspect_original_operation_or_configuration","automatic_retry":false}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
