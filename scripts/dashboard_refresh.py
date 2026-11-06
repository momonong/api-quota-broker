"""Validate/stage bounded dashboard evidence; explicit local GET collection only.

Default commands are dry runs. No provider credential, generation route, login,
timer, active Gateway configuration or service management is supported.
"""

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quota_broker.dashboard import Dashboard, UpstreamSource
from quota_broker.dashboard_accounts import CreditAccounts, _unique
from quota_broker.dashboard_provider_metadata import (
    KEY_URL,
    counter_fields,
    model_sources,
    quota_projection,
    validate_report,
)
from quota_broker.dashboard_reviewed import create_reviewed
from quota_broker.discovery_sources import MAX_SNAPSHOT_BYTES, validate_snapshot
from quota_broker.gateway_server import _json_depth

MAX_BYTES = 262144
REVIEWED_FILES = ("reviewed.json", "gateway.json", "registry.json")
BROKER_ROUTES = (
    "/v1/catalog",
    "/v1/diagnostics",
    "/v1/usage",
    "/v1/tasks?limit=6",
    "/v1/model-inventory",
)


class RefreshError(ValueError):
    """Only fixed reason codes may leave the private-input boundary."""


class MetadataSource(UpstreamSource):
    ROUTES = frozenset(("GET", path) for path in BROKER_ROUTES)

    def models(self) -> Any:
        return self._get("GET", "/v1/model-inventory")


def _path(path: str | Path) -> Path:
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise RefreshError("unsafe_path")
    path = path.absolute()
    for parent in path.parents:
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise RefreshError("unsafe_path")
    info = path.parent.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RefreshError("private_directory_required")
    return path


def _read(path: Path, maximum: int = MAX_BYTES) -> bytes:
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise RefreshError("private_file_required")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RefreshError("invalid_private_file")
    return raw


def _new_file(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(path: Path, raw: bytes, previous: bytes | None) -> bool:
    """Keep old bytes before atomic replacement; fail if the target drifted."""
    path = _path(path)
    if previous == raw:
        return False
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(4)
    temporary = path.with_name("." + path.name + ".pending." + suffix)
    try:
        _new_file(temporary, raw)
        if previous is not None:
            if _read(path, maximum=2 * 1024 * 1024) != previous:
                raise RefreshError("destination_changed")
            _new_file(path.with_name(path.name + ".previous." + suffix), previous)
            _sync_directory(path.parent)
        elif path.exists() or path.is_symlink():
            raise RefreshError("destination_changed")
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _accounts(path: Path, now: datetime) -> tuple[bytes, dict[str, Any], list[dict[str, Any]]]:
    raw = _read(path, 131072)
    # The production reader validates a stable private copy, never a raced path.
    with tempfile.TemporaryDirectory(prefix="aqb-refresh-") as directory:
        private_copy = Path(directory) / "accounts.json"
        _new_file(private_copy, raw)
        accounts = CreditAccounts(private_copy)
    rows = accounts.snapshot(now)
    summary = []
    for row in rows:
        evidence = row["evidence"]
        if not evidence["sources"] or _instant(evidence["as_of"]) > now:
            raise RefreshError("invalid_evidence")
        summary.append(
            {
                "provider": row["provider"],
                "state": "stale"
                if not evidence["current"]
                else "current"
                if row["current_balance"] is not None
                else "unknown",
                "as_of": evidence["as_of"],
                "valid_until": evidence["valid_until"],
                "sources": evidence["sources"],
                "balance_known": row["current_balance"] is not None,
                "execution_allowed": False,
            }
        )
    return raw, json.loads(raw), summary


def _reviewed(path: Path, now: datetime) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    path = _path(path)
    if path.name != "reviewed.json":
        raise RefreshError("reviewed_filename_required")
    files = {name: _read(path.parent / name) for name in REVIEWED_FILES}
    with tempfile.TemporaryDirectory(prefix="aqb-refresh-") as directory:
        root = Path(directory)
        for name, raw in files.items():
            _new_file(root / name, raw)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        source = create_reviewed(runtime, root / "reviewed.json", clock=lambda: now)
        profile = source.profile
        anchor = _instant(profile["audit_session_at"])
        due = _instant(profile["review_due_at"])
        if anchor > now:
            raise RefreshError("invalid_evidence")
        targets = json.loads(files["gateway.json"])["targets"]
        summary = []
        for target in targets:
            row = source.rows[target["id"]]
            estimates = [
                fact["remaining"]
                for fact in target.get("provider_quota_facts", [])
                if fact["remaining"]["value"] is not None
            ]
            remaining_state = "unknown"
            if estimates:
                remaining_state = (
                    "current_estimate"
                    if all(
                        _instant(item["as_of"]) <= now < _instant(item["valid_until"])
                        for item in estimates
                    )
                    else "stale_estimate"
                )
            summary.append(
                {
                    "provider": target["provider"],
                    "model": target["model"],
                    "state": "unknown"
                    if not row["sources"]
                    else "stale"
                    if now >= due
                    else "current_metadata",
                    "qualification_state": "unverified"
                    if row["qualification_basis"] == "unverified"
                    else "stale"
                    if now >= due
                    else "current_metadata",
                    "billing_state": row["billing_state"],
                    "as_of": profile["audit_session_at"] if row["sources"] else None,
                    "valid_until": profile["review_due_at"] if row["sources"] else None,
                    "sources": row["sources"],
                    "remaining_state": remaining_state,
                    "execution_allowed": False,
                }
            )
    return files, summary


def _newer(previous: dict[str, Any], incoming: dict[str, Any]) -> None:
    old = {row["provider"]: row for row in previous["accounts"]}
    for row in incoming["accounts"]:
        before = old.get(row["provider"])
        if (
            before is not None
            and row != before
            and _instant(row["evidence"]["as_of"]) <= (_instant(before["evidence"]["as_of"]))
        ):
            raise RefreshError("evidence_not_newer")


def _import_accounts(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    raw, document, rows = _accounts(Path(args.input), now)
    destination = _path(args.destination)
    previous = None
    if destination.exists() or destination.is_symlink():
        previous, previous_document, _ = _accounts(destination, now)
        _newer(previous_document, document)
    changed = previous != raw
    if args.apply:
        changed = _replace(destination, raw, previous)
    return {"accounts": rows, "changed": changed, "active_configuration_changed": False}


def _import_reviewed(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    files, rows = _reviewed(Path(args.input), now)
    destination = _path(args.destination)
    if destination.exists() or destination.is_symlink():
        raise RefreshError("new_evidence_directory_required")
    if args.apply:
        temporary = Path(tempfile.mkdtemp(prefix=".reviewed-pending-", dir=destination.parent))
        try:
            for name, raw in files.items():
                _new_file(temporary / name, raw)
            _sync_directory(temporary)
            if destination.exists() or destination.is_symlink():
                raise RefreshError("destination_changed")
            os.rename(temporary, destination)
            _sync_directory(destination.parent)
        finally:
            if temporary.exists():
                for file in temporary.iterdir():
                    file.unlink()
                temporary.rmdir()
    return {"targets": rows, "changed": True, "active_configuration_changed": False}


def _collect(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    # URL validation performs no I/O; dry run does not even open the client token.
    MetadataSource(args.upstream, "offline-url-validation-placeholder-000000")
    result: dict[str, Any] = {
        "routes": list(BROKER_ROUTES),
        "network_performed": False,
        "provider_balance_state": "manual_required",
        "active_configuration_changed": False,
    }
    if not args.apply:
        return result
    output = _path(args.output)
    previous = _read(output, 2 * 1024 * 1024) if output.exists() or output.is_symlink() else None
    if previous is not None:
        try:
            prior = json.loads(previous)
            if (
                not isinstance(prior, dict)
                or prior.get("kind") != "broker_readonly_metadata"
                or type(prior.get("schema_version")) is not int
                or prior["schema_version"] != 1
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise RefreshError("collection_destination_required") from None
    token = _read(Path(args.token_file), 1025).decode("ascii").strip()
    source = MetadataSource(args.upstream, token)
    snapshot = Dashboard(source).snapshot()
    document = {
        "schema_version": 1,
        "kind": "broker_readonly_metadata",
        "collected_at": datetime.now(UTC).isoformat(),
        "basis": "local_broker_metadata_not_official_account_balance",
        "provider_balance_state": "manual_required",
        "snapshot": snapshot,
    }
    raw = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"
    if len(raw) > 2 * 1024 * 1024 or token.encode() in raw:
        raise RefreshError("invalid_collection")
    _replace(output, raw, previous)
    result.update(
        network_performed=True,
        collected_at=document["collected_at"],
        target_count=len(snapshot["targets"]),
        activity_state=snapshot["activity_source"]["state"],
    )
    return result


def _latest(previous: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if (
        previous is not None
        and incoming != previous
        and _instant(incoming["checked_at"]) <= _instant(previous["checked_at"])
    ):
        raise RefreshError("provider_observation_not_newer")
    return incoming


def _provider_report(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    files, _ = _reviewed(Path(args.reviewed_config), now)
    targets = {
        (item["provider"], item["model"]) for item in json.loads(files["gateway.json"])["targets"]
    }
    output = _path(args.destination)
    previous = _read(output) if output.exists() or output.is_symlink() else None
    report: dict[str, Any] = (
        validate_report(previous, now)
        if previous is not None
        else {
            "schema_version": 1,
            "kind": "provider_metadata_observations",
            "models": [],
            "openrouter": None,
        }
    )
    models = {
        (item["provider"], item["model"]): item
        for item in report["models"]
        if (item["provider"], item["model"]) in targets
    }
    if len(args.input) > 8 or args.input.count("-") > 1:
        raise RefreshError("too_many_provider_inputs")
    for path in args.input:
        maximum = 8 * MAX_SNAPSHOT_BYTES
        raw = sys.stdin.buffer.read(maximum + 1) if path == "-" else _read(Path(path), maximum)
        if len(raw) > maximum:
            raise RefreshError("invalid_provider_input")
        _json_depth(raw, maximum=12)
        document = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(document, dict):
            raise RefreshError("invalid_provider_input")
        if document.get("state") in {"completed", "stopped"}:
            # Each completed component retains its own evidence even if a later GET failed.
            snapshots, accounts = document["snapshots"], document["account_metadata"]
        elif document.get("source") == KEY_URL:
            snapshots, accounts = [], [document]
        else:
            snapshots, accounts = [document], []
        if (
            not isinstance(snapshots, list)
            or len(snapshots) > 7
            or not isinstance(accounts, list)
            or len(accounts) > 1
        ):
            raise RefreshError("invalid_provider_input")
        for snapshot in snapshots:
            snapshot = validate_snapshot(snapshot, now)
            provider = snapshot["provider"]
            if snapshot["source"] not in model_sources(provider):
                raise RefreshError("unexpected_provider_source")
            matches: dict[str, set[str]] = {}
            for entry in snapshot["models"]:
                status = (
                    "inactive"
                    if entry["status"] == "retired" or "inactive" in entry["features"]
                    else "listed"
                )
                matches.setdefault(entry["model"], set()).add(status)
            for identity in targets:
                if identity[0] != provider:
                    continue
                states = matches.get(identity[1], set())
                if provider == "ocrspace" and not states:
                    continue
                if provider == "ocrspace":
                    status = "documented"
                elif len(states) == 1:
                    status = next(iter(states))
                else:
                    status = "not_listed" if not states and snapshot["complete"] else "unknown"
                item = {
                    "provider": provider,
                    "model": identity[1],
                    "source": snapshot["source"],
                    "checked_at": snapshot["checked_at"],
                    "status": status,
                }
                models[identity] = _latest(models.get(identity), item)
        for account in accounts:
            if (
                type(account["schema_version"]) is not int
                or account["schema_version"] != 1
                or account["provider"] != "openrouter"
                or account["source"] != KEY_URL
                or account["daily_counter_semantics"] != "reported_tier_policy_not_admission"
            ):
                raise RefreshError("invalid_openrouter_counter")
            row = {
                "source": KEY_URL,
                "checked_at": account["checked_at"],
                "daily": counter_fields(account["free_model_daily_requests"]),
            }
            report["openrouter"] = _latest(report["openrouter"], row)
    report["models"] = [models[key] for key in sorted(models)]
    encoded = json.dumps(report, indent=2, allow_nan=False).encode() + b"\n"
    validate_report(encoded, now)
    changed = _replace(output, encoded, previous) if args.apply else False
    quota = quota_projection(report, now)
    return {
        "changed": changed,
        "observed_models": report["models"],
        "unobserved_models": [list(key) for key in sorted(targets - models.keys())],
        "openrouter_counter_state": quota["state"] if quota else "unobserved",
        "active_configuration_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", aliases=["validate"])
    inspect.add_argument("--accounts-file")
    inspect.add_argument("--reviewed-config")
    for name in ("import-accounts", "import-reviewed"):
        command = commands.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--destination", required=True)
        command.add_argument("--apply", action="store_true")
    collect = commands.add_parser("collect")
    collect.add_argument("--upstream", required=True)
    collect.add_argument("--token-file", required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--apply", action="store_true")
    providers = commands.add_parser("import-provider-metadata")
    providers.add_argument("--input", action="append", required=True)
    providers.add_argument("--reviewed-config", required=True)
    providers.add_argument("--destination", required=True)
    providers.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    result: dict[str, Any]
    try:
        if args.command in {"inspect", "validate"}:
            if not args.accounts_file and not args.reviewed_config:
                raise RefreshError("input_required")
            result = {}
            if args.accounts_file:
                result["accounts"] = _accounts(Path(args.accounts_file), now)[2]
            if args.reviewed_config:
                result["targets"] = _reviewed(Path(args.reviewed_config), now)[1]
        elif args.command == "import-accounts":
            result = _import_accounts(args, now)
        elif args.command == "import-reviewed":
            result = _import_reviewed(args, now)
        elif args.command == "import-provider-metadata":
            result = _provider_report(args, now)
        else:
            result = _collect(args, now)
        result.update(
            mode="applied" if getattr(args, "apply", False) else "dry_run",
            checked_at=now.isoformat(),
            provider_generation_performed=False,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - never echo private input, paths or credentials
        reason = str(exc) if isinstance(exc, RefreshError) else "invalid_or_unavailable_metadata"
        print(json.dumps({"error": reason}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
