"""Fixed HP shared-Broker release publisher. No root application execution.

The forced command accepts status/validate/publish/rollback JSON only. It cannot
change credentials, launcher, unit, SSH, Caddy or another service. A failed or
interrupted operation blocks later publication until that original is reconciled.
The SQLite ledger is opened read-only; rollback only changes the code symlink.
"""

import ast
import base64
import fcntl
import grp
import hashlib
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
from contextlib import closing
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from uuid import UUID

OWNER, GROUP, SERVICE_UID = 0, 0, 0
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
CGROUP_ROOT = Path("/sys/fs/cgroup")
IDENTITY = "quota-shared-broker"
BASE = Path("/opt/quota-shared-broker")
JOURNAL = Path("/var/lib/quota-shared-maintenance")
RUNTIME_STATE = Path("/var/lib/quota-shared-broker")
LOCK = Path("/run/quota-shared-update.lock")
POLICY = Path("/etc/quota-shared-maintenance/policy.json")
ENTRY = Path("/usr/local/libexec/hp-quota-shared-maintenance.py")
LAUNCHER = Path("/usr/local/libexec/quota-shared-runtime.py")
UNIT = Path("/etc/systemd/system/quota-shared-broker.service")
READ_TOKEN = Path("/etc/credstore.encrypted/quota-shared-read-token")
MAX_FILE, MAX_TOTAL, MAX_JSON = 4 * 1024**2, 12 * 1024**2, 20 * 1024**2
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}")
HASH = re.compile(r"[0-9a-f]{64}")
TERMINAL = {"complete", "rolled_back"}
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
    "families",
    "family_adapters",
    "family_transport",
    "gateway",
    "gateway_providers",
    "gateway_server",
    "key_admin",
    "nvidia",
    "nvidia_server",
    "provider_policy",
    "queue",
    "registry",
    "retry",
    "routing",
    "server",
    "token_preflight",
)
ALLOWED = tuple(
    sorted(
        ["app/quota_broker/" + name + ".py" for name in MODULES]
        + [
            "app/quota_broker/dashboard_assets/" + name
            for name in ("index.html", "dashboard.css", "dashboard.js", "provider-inventory.json")
        ]
        + [
            "config/" + name
            for name in (
                "registry.json",
                "gateway.json",
                "access.json",
                "observations.json",
                "secret-inventory.json",
            )
        ]
    )
)


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
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate_json")
        result[key] = value
    return result


def parse(raw):
    need(len(raw) <= MAX_JSON, "json_bound")
    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(Refused("json_constant")),
        )
    except (ValueError, RecursionError, UnicodeError):
        raise Refused("json_invalid") from None


def identifier(value):
    need(isinstance(value, str) and NAME.fullmatch(value), "release_id")
    return value


def operation_id(value):
    try:
        need(str(UUID(value)) == value, "operation_id")
    except (ValueError, TypeError, AttributeError):
        raise Refused("operation_id") from None
    return value


def hash_value(value):
    need(isinstance(value, str) and HASH.fullmatch(value), "digest")
    return value


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
        }
        and payload["schema"] == "hp-quota-shared-release-v1",
        "payload_schema",
    )
    operation_id(payload["operation_id"])
    identifier(payload["release_id"])
    identifier(payload["expected_current"])
    hash_value(payload["expected_manifest_sha256"])
    need(payload["release_id"] != payload["expected_current"], "same_release")
    rows = payload["files"]
    need(isinstance(rows, list) and len(rows) == len(ALLOWED), "exact_files_required")
    manifest, files, total = [], {}, 0
    for row in rows:
        need(
            isinstance(row, dict) and set(row) == {"path", "size", "sha256", "content_base64"},
            "file_schema",
        )
        path = row["path"]
        need(isinstance(path, str) and path in ALLOWED and path not in files, "file_scope")
        need(type(row["size"]) is int and 0 <= row["size"] <= MAX_FILE, "file_bound")
        hash_value(row["sha256"])
        need(
            isinstance(row["content_base64"], str)
            and len(row["content_base64"]) <= ((MAX_FILE + 2) // 3) * 4,
            "encoded_bound",
        )
        try:
            raw = base64.b64decode(row["content_base64"], validate=True)
            if path.endswith(".py"):
                ast.parse(raw, filename=path)
            elif path.endswith(".json"):
                parse(raw)
        except (ValueError, SyntaxError, RecursionError):
            raise Refused("file_invalid") from None
        need(len(raw) == row["size"] and digest(raw) == row["sha256"], "file_digest")
        total += len(raw)
        need(total <= MAX_TOTAL, "total_bound")
        manifest.append({key: row[key] for key in ("path", "size", "sha256")})
        files[path] = raw
    need(tuple(files) == ALLOWED, "exact_files_required")
    validate_configuration(files)
    return manifest, files


def configuration_instant(value):
    try:
        need(isinstance(value, str) and len(value) <= 40, "configuration_timestamp")
        parsed = datetime.fromisoformat(value)
        need(parsed.tzinfo is not None, "configuration_timestamp")
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise Refused("configuration_timestamp") from None


def validate_configuration(files):
    """Cheap stdlib cross-file gates only; never import candidate application code."""
    inventory = parse(files["config/secret-inventory.json"])
    refs = PROVIDER_REFS
    need(
        isinstance(inventory, dict)
        and set(inventory) == {"names", "verified_at", "expires_at", "project", "config"}
        and isinstance(inventory.get("names"), list)
        and len(inventory["names"]) <= len(PROVIDER_NAMES)
        and all(isinstance(name, str) for name in inventory["names"])
        and len(set(inventory["names"])) == len(inventory["names"])
        and set(inventory["names"]) <= PROVIDER_NAMES
        and inventory["project"] == "api-quota-broker"
        and inventory["config"] == "dev",
        "inventory_scope",
    )
    inventory_start = configuration_instant(inventory["verified_at"])
    inventory_end = configuration_instant(inventory["expires_at"])
    need(inventory_start < inventory_end, "inventory_validity")
    gateway = parse(files["config/gateway.json"])
    need(
        isinstance(gateway, dict)
        and set(gateway) == {"targets"}
        and isinstance(gateway["targets"], list),
        "gateway_targets",
    )
    need(len(gateway["targets"]) <= 256, "gateway_targets")
    required_names = set()
    for target in gateway["targets"]:
        need(isinstance(target, dict) and type(target.get("enabled")) is bool, "target_enabled")
        if not target["enabled"]:
            continue
        provider = target.get("provider")
        need(
            isinstance(provider, str)
            and target.get("secret_ref") == refs.get(provider)
            and provider in refs
            and target.get("account_id_ref")
            == ("CLOUDFLARE_ACCOUNT_ID" if provider == "cloudflare" else None),
            "enabled_secret_reference",
        )
        required_names.add(refs[provider])
        if provider == "cloudflare":
            required_names.add("CLOUDFLARE_ACCOUNT_ID")
        required = target.get("required_capacity")
        need(isinstance(required, list) and 0 < len(required) <= 32, "enabled_required_capacity")
        dimensions = set()
        for row in required:
            need(
                isinstance(row, dict)
                and set(row) == {"metric", "window"}
                and isinstance(row.get("metric"), str)
                and isinstance(row.get("window"), str)
                and row.get("metric")
                in {
                    "requests",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "audio_seconds",
                    "images",
                    "pages",
                    "conversions",
                    "neurons",
                    "credits_usd_micros",
                }
                and row.get("window")
                in {"rolling_minute", "rolling_hour", "day", "month", "budget"},
                "enabled_required_capacity",
            )
            dimension = (row["metric"], row["window"])
            need(dimension not in dimensions, "enabled_required_capacity")
            dimensions.add(dimension)
        need(
            ("credits_usd_micros", "budget") in dimensions
            if provider == "openai"
            else ("requests", "day") in dimensions
            if provider == "openrouter"
            else ("neurons", "day") in dimensions
            if provider == "cloudflare"
            else bool(dimensions),
            "enabled_required_capacity",
        )
        start = configuration_instant(target.get("verified_at"))
        end = configuration_instant(target.get("expires_at"))
        need(start < end <= inventory_end, "eligibility_exceeds_inventory_validity")
    names = set(inventory["names"])
    need(
        names == required_names
        or (names == LEGACY_PROVIDER_NAMES and required_names <= LEGACY_PROVIDER_NAMES),
        "inventory_scope",
    )


def directory(path):
    info = path.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
        "directory_metadata",
    )


def read(path, bound=MAX_FILE, *, owner=None, mode=None):
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), "parent_symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == (OWNER if owner is None else owner)
            and not before.st_mode & 0o022
            and before.st_size <= bound
            and (mode is None or stat.S_IMODE(before.st_mode) == mode),
            "file_metadata",
        )
        raw = stream.read(bound + 1)
        after = os.fstat(stream.fileno())
        need(
            len(raw) <= bound
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "file_changed",
        )
        return raw


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, *, mode=0o600, group=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), OWNER, group)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def command(args, timeout=30):
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    need(result.returncode == 0, "host_command_failed")
    return result.stdout


def _stopped_fields():
    fields = dict(
        line.split("=", 1)
        for line in command(
            [
                "/usr/bin/systemctl",
                "show",
                SERVICE,
                "--property=ActiveState,SubState,MainPID,ControlPID,Job,ControlGroup",
            ]
        )
        .decode()
        .splitlines()
    )
    need(
        set(fields) == {"ActiveState", "SubState", "MainPID", "ControlPID", "Job", "ControlGroup"},
        "service_stop_uncertain",
    )
    need(
        fields["MainPID"] == "0" and fields["ControlPID"] == "0" and fields["Job"] == "",
        "service_stop_uncertain",
    )
    return fields


def _empty_service_cgroup(group):
    expected = "/system.slice/" + SERVICE
    need(group in {"", expected}, "service_cgroup_uncertain")
    path = CGROUP_ROOT / expected.lstrip("/")
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), "service_cgroup_uncertain")
    if not os.path.lexists(path):
        return
    directory(path)
    # A top-level empty procs file does not prove descendant groups are empty.
    # This service has no delegated child groups: refuse any rather than walking
    # an unbounded tree or accidentally resetting a unit with surviving workers.
    with os.scandir(path) as entries:
        for entry in entries:
            need(
                not entry.is_symlink() and not entry.is_dir(follow_symlinks=False),
                "service_cgroup_has_children",
            )
    try:
        processes = read(path / "cgroup.procs", 65536)
    except FileNotFoundError:
        need(not os.path.lexists(path), "service_cgroup_uncertain")
        return
    need(not processes.strip(), "service_cgroup_not_empty")


def stop_service():
    command(["/usr/bin/systemctl", "stop", SERVICE], timeout=185)
    fields = _stopped_fields()
    if (fields["ActiveState"], fields["SubState"]) == ("failed", "failed"):
        _empty_service_cgroup(fields["ControlGroup"])
        # Reset only this proven-quiescent unit; no start, kill or other service.
        command(["/usr/bin/systemctl", "reset-failed", SERVICE])
        fields = _stopped_fields()
    need(
        (fields["ActiveState"], fields["SubState"]) == ("inactive", "dead"),
        "service_stop_uncertain",
    )
    _empty_service_cgroup(fields["ControlGroup"])


def start_service():
    command(["/usr/bin/systemctl", "start", SERVICE])


def verify_service(_manifest):
    read(READ_TOKEN, 65536, mode=0o600)
    token = command(
        ["/usr/bin/systemd-creds", "decrypt", "--name=read-token", str(READ_TOKEN), "-"]
    )
    need(len(token) <= 1024, "credential_bound")
    try:
        token = token.decode("ascii").strip()
    except UnicodeError:
        raise Refused("credential_format") from None
    need(re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", token), "credential_format")

    def once():
        fields = dict(
            line.split("=", 1)
            for line in command(
                ["/usr/bin/systemctl", "show", SERVICE, "--property=ActiveState,SubState,MainPID"],
                timeout=5,
            )
            .decode()
            .splitlines()
        )
        need(
            fields.get("ActiveState") == "active"
            and fields.get("SubState") == "running"
            and fields.get("MainPID", "0").isdigit()
            and int(fields["MainPID"]) > 0,
            "service_not_ready",
        )
        need(
            (Path("/proc") / fields["MainPID"] / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            == [b"/usr/bin/python3", b"-I", b"-B", os.fsencode(LAUNCHER)],
            "process_mismatch",
        )
        checks = [
            (18587, "/v1/catalog", token, 200, list),
            (18587, "/v1/diagnostics", token, 200, dict),
            (18587, "/v1/usage", token, 200, list),
            (18587, "/v1/catalog", None, 401, None),
            (18588, "/quota/api/v1/jobs", None, 401, None),
            (18588, "/quota/api/v1/jobs", token, 403, None),
        ]
        for port, path, credential, status, kind in checks:
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                headers = {"Authorization": "Bearer " + credential} if credential else {}
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                raw = response.read(MAX_FILE + 1)
                need(len(raw) <= MAX_FILE and response.status == status, "http_boundary")
                if kind:
                    need(isinstance(parse(raw), kind), "http_shape")
            finally:
                connection.close()

    deadline = time.monotonic() + 45
    while True:
        try:
            once()
            return
        except (OSError, Refused, subprocess.SubprocessError, ValueError):
            if time.monotonic() >= deadline:
                raise Refused("readiness_failed") from None
            time.sleep(0.5)


class Operator:
    def __init__(self, root=BASE, journal=JOURNAL, lock=LOCK, runtime_state=RUNTIME_STATE):
        self.root, self.journal, self.lock, self.runtime_state = root, journal, lock, runtime_state

    def manifest(self, release):
        location = self.root / "releases" / identifier(release)
        directory(location)
        rows = parse(read(location / "manifest.json", 128 * 1024))
        need(isinstance(rows, list) and len(rows) == len(ALLOWED), "manifest_shape")
        names, total = [], 0
        for row in rows:
            need(isinstance(row, dict) and set(row) == {"path", "size", "sha256"}, "manifest_shape")
            path = row["path"]
            need(
                path in ALLOWED
                and path not in names
                and type(row["size"]) is int
                and 0 <= row["size"] <= MAX_FILE,
                "manifest_scope",
            )
            raw = read(location / path)
            need(len(raw) == row["size"] and digest(raw) == row["sha256"], "installed_drift")
            names.append(path)
            total += len(raw)
        need(tuple(names) == ALLOWED and total <= MAX_TOTAL, "installed_file_set")
        actual = set()
        for parent, dirs, files in os.walk(location, followlinks=False):
            directory(Path(parent))
            for name in dirs:
                directory(Path(parent) / name)
            for name in files:
                actual.add((Path(parent) / name).relative_to(location).as_posix())
        need(actual == set(ALLOWED) | {"manifest.json"}, "installed_extra_file")
        return rows

    def current(self):
        directory(self.root)
        directory(self.root / "releases")
        link = self.root / "current"
        info = link.lstat()
        need(stat.S_ISLNK(info.st_mode) and info.st_uid == OWNER, "current_owner")
        target = os.readlink(link)
        need(target.startswith("releases/") and target.count("/") == 1, "current_target")
        release = identifier(target.split("/")[1])
        manifest = self.manifest(release)
        return release, digest(canonical(manifest)), manifest

    def stage(self, release, manifest, files):
        location = self.root / "releases" / identifier(release)
        location.mkdir(mode=0o750)
        location.chmod(0o750)
        if os.geteuid() == 0:
            os.chown(location, OWNER, GROUP)
        fsync_dir(location.parent)
        for name, raw in files.items():
            path = location / name
            missing, parent = [], path.parent
            while parent != location and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o750)
                parent.chmod(0o750)
                if os.geteuid() == 0:
                    os.chown(parent, OWNER, GROUP)
                fsync_dir(parent.parent)
            write_new(path, raw, mode=0o640, group=GROUP)
        write_new(location / "manifest.json", canonical(manifest), mode=0o640, group=GROUP)
        self.manifest(release)

    def idle(self):
        info = self.runtime_state.lstat()
        need(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == SERVICE_UID
            and stat.S_IMODE(info.st_mode) == 0o700,
            "runtime_state",
        )
        marker = read(self.runtime_state / "instance.json", 65536, owner=SERVICE_UID, mode=0o600)
        value = parse(marker)
        need(
            isinstance(value, dict)
            and set(value)
            == {
                "schema",
                "instance_id",
                "historical_gap",
                "history_start",
                "credential_fingerprint",
            }
            and type(value.get("schema")) is int
            and value["schema"] == 1
            and value.get("instance_id") == INSTANCE
            and value.get("historical_gap") is True
            and isinstance(value.get("history_start"), str)
            and isinstance(value.get("credential_fingerprint"), str)
            and HASH.fullmatch(value["credential_fingerprint"]),
            "instance_binding",
        )
        path = self.runtime_state / "ledger.sqlite3"
        info = path.lstat()
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == SERVICE_UID
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "ledger_permissions",
        )
        with closing(
            sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as con:
            need(con.execute("PRAGMA quick_check").fetchone()[0] == "ok", "ledger_integrity")
            need(
                con.execute(
                    "SELECT count(*) FROM queue_jobs WHERE state IN ('queued','waiting','running')"
                ).fetchone()[0]
                == 0,
                "pending_jobs_require_drain",
            )
            need(
                con.execute(
                    "SELECT count(*) FROM reservations r WHERE state IN ('reserved','dispatched') "
                    "AND NOT EXISTS (SELECT 1 FROM execution_completion e WHERE e.reservation_id=r.id)"
                ).fetchone()[0]
                == 0,
                "inflight_requires_reconcile",
            )
        after = path.lstat()
        need((info.st_dev, info.st_ino) == (after.st_dev, after.st_ino), "ledger_replaced")
        return marker, (info.st_dev, info.st_ino)

    def receipt(self, operation):
        result = parse(read(self.journal / (operation_id(operation) + ".json"), 65536, mode=0o600))
        need(
            isinstance(result, dict)
            and result.get("operation_id") == operation
            and result.get("status")
            in TERMINAL
            | {"claimed", "stopping", "switched", "starting", "unknown", "rolling_back"},
            "receipt_identity",
        )
        identifier(result.get("release_id"))
        identifier(result.get("previous"))
        hash_value(result.get("manifest_sha256"))
        hash_value(result.get("previous_manifest_sha256"))
        return result

    def save(self, record, *, new=False):
        path = self.journal / (operation_id(record["operation_id"]) + ".json")
        if new:
            write_new(path, canonical(record))
            return
        prior = read(path, 65536, mode=0o600)
        history = (
            self.journal / "history" / (record["operation_id"] + "-" + digest(prior) + ".json")
        )
        if not history.exists():
            write_new(history, prior)
        pending = path.with_suffix(".pending")
        write_new(pending, canonical(record))
        os.replace(pending, path)
        fsync_dir(self.journal)

    def unresolved(self, except_id=None):
        paths = list(self.journal.glob("*.json"))
        need(
            len(paths) <= 4096 and not list(self.journal.glob("*.pending")),
            "journal_requires_reconcile",
        )
        for path in paths:
            record = self.receipt(path.stem)
            need(
                record["status"] in TERMINAL or record["operation_id"] == except_id,
                "unresolved_operation",
            )

    def switch(self, release, operation):
        self.manifest(release)
        temporary = self.root / (".current-" + operation_id(operation))
        need(not os.path.lexists(temporary), "temporary_link_requires_reconcile")
        os.symlink("releases/" + identifier(release), temporary)
        os.replace(temporary, self.root / "current")
        fsync_dir(self.root)

    def protected_access(self, files, baseline):
        existing = read(self.root / "releases" / baseline / "config/access.json")
        need(files["config/access.json"] == existing, "credential_policy_change_forbidden")

    def publish(self, payload):
        manifest, files = validate(payload)
        self.unresolved()
        operation = payload["operation_id"]
        need(
            not os.path.lexists(self.journal / (operation + ".json")), "operation_already_recorded"
        )
        need(
            not os.path.lexists(self.root / "releases" / payload["release_id"]),
            "release_already_exists",
        )
        current, sha, old = self.current()
        need(
            (current, sha) == (payload["expected_current"], payload["expected_manifest_sha256"]),
            "baseline_drift",
        )
        self.protected_access(files, current)
        binding = self.idle()
        verify_service(old)
        record = {
            "operation_id": operation,
            "release_id": payload["release_id"],
            "manifest_sha256": digest(canonical(manifest)),
            "previous": current,
            "previous_manifest_sha256": sha,
            "status": "claimed",
        }
        self.save(record, new=True)
        try:
            self.stage(payload["release_id"], manifest, files)
            need(self.current()[:2] == (current, sha), "baseline_drift")
            record["status"] = "stopping"
            self.save(record)
            stop_service()
            need(self.idle() == binding, "state_binding_changed")
            self.switch(payload["release_id"], operation)
            record["status"] = "switched"
            self.save(record)
            record["status"] = "starting"
            self.save(record)
            start_service()
            verify_service(manifest)
            need(self.idle() == binding, "state_binding_changed")
            need(
                self.current()[:2] == (payload["release_id"], record["manifest_sha256"]),
                "publish_readback",
            )
            record["status"] = "complete"
            self.save(record)
        except Exception as exc:  # noqa: BLE001 - preserve original operation; redact exceptions
            record["failure_phase"] = record["status"]
            record["error_code"] = str(exc) if isinstance(exc, Refused) else "host_exception"
            if record["status"] == "starting":
                try:
                    stop_service()
                    record["service_cleanup"] = "stopped"
                except Exception:  # noqa: BLE001 - quarantine outcome may itself be uncertain
                    record["service_cleanup"] = "uncertain"
            record["status"] = "unknown"
            self.save(record)
        return self.project(record)

    def rollback(self, operation):
        record = self.receipt(operation)
        need(record["status"] != "rolled_back", "operation_already_recorded")
        self.unresolved(operation)
        current, sha, _ = self.current()
        need(
            (current, sha)
            in {
                (record["release_id"], record["manifest_sha256"]),
                (record["previous"], record["previous_manifest_sha256"]),
            },
            "foreign_current",
        )
        old = self.manifest(record["previous"])
        need(digest(canonical(old)) == record["previous_manifest_sha256"], "previous_drift")
        binding = self.idle()
        started = False
        record["status"] = "rolling_back"
        self.save(record)
        try:
            stop_service()
            need(self.idle() == binding, "state_binding_changed")
            if current != record["previous"]:
                self.switch(record["previous"], operation)
            started = True
            start_service()
            verify_service(old)
            need(self.idle() == binding, "state_binding_changed")
            record["status"] = "rolled_back"
            self.save(record)
        except Exception as exc:  # noqa: BLE001 - explicit rollback may also be outcome-uncertain
            record["failure_phase"] = "rollback"
            record["error_code"] = str(exc) if isinstance(exc, Refused) else "host_exception"
            if started:
                try:
                    stop_service()
                    record["service_cleanup"] = "stopped"
                except Exception:  # noqa: BLE001 - retain uncertain cleanup, never retry it
                    record["service_cleanup"] = "uncertain"
            record["status"] = "unknown"
            self.save(record)
        return self.project(record)

    def project(self, record=None):
        current, sha, _ = self.current()
        result = {
            "current": current,
            "manifest_sha256": sha,
            "status": record["status"] if record else "ready",
            "provider_calls": 0,
            "paid_allowed": False,
            "automatic_retry": False,
        }
        if record:
            result.update(
                operation_id=record["operation_id"],
                release_id=record["release_id"],
                release_manifest_sha256=record["manifest_sha256"],
                previous=record["previous"],
                previous_manifest_sha256=record["previous_manifest_sha256"],
            )
            for name in ("failure_phase", "error_code", "service_cleanup"):
                if name in record:
                    result[name] = record[name]
        else:
            paths = list(self.journal.glob("*.json"))
            need(len(paths) <= 4096, "receipt_count_bound")
            pending = []
            for path in paths:
                receipt = self.receipt(path.stem)
                if receipt["status"] not in TERMINAL:
                    pending.append(receipt["operation_id"])
            interrupted = list(self.journal.glob("*.pending"))
            if pending or interrupted:
                result["status"] = "reconcile_required"
                result["pending_operations"] = pending
                result["interrupted_receipt_write"] = bool(interrupted)
        return result

    def handle(self, request):
        need(
            isinstance(request, dict) and request.get("schema") == "hp-quota-shared-maintenance-v1",
            "wire_schema",
        )
        action = request.get("action")
        need(action in {"status", "validate", "publish", "rollback"}, "action")
        fields = {"schema", "action"} | (
            {"payload"} if action in {"validate", "publish"} else {"operation_id"}
        )
        need(not set(request) - fields, "wire_fields")
        if action in {"validate", "publish"}:
            need("payload" in request, "payload_required")
        for path in (self.root, self.journal, self.journal / "history"):
            directory(path)
        need(
            stat.S_IMODE(self.journal.stat().st_mode) == 0o700
            and stat.S_IMODE((self.journal / "history").stat().st_mode) == 0o700,
            "journal_private",
        )
        fd = os.open(self.lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == OWNER
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600,
                "lock_metadata",
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if action == "status":
                if "operation_id" not in request:
                    return self.project()
                operation = operation_id(request["operation_id"])
                if os.path.lexists(self.journal / (operation + ".json")):
                    return self.project(self.receipt(operation))
                # Absence is authoritative only under this same exclusive lock.
                # Any pending/unknown operation or historical artifact prevents
                # a missing receipt from being mistaken for an unstarted action.
                self.unresolved()
                need(
                    not os.path.lexists(self.journal / (operation + ".pending"))
                    and not list((self.journal / "history").glob(operation + "-*")),
                    "operation_history_requires_reconcile",
                )
                result = self.project()
                need(result["status"] == "ready", "journal_requires_reconcile")
                return {**result, "status": "not_started", "operation_id": operation}
            if action == "validate":
                manifest, files = validate(request["payload"])
                current, sha, _ = self.current()
                need(
                    (current, sha)
                    == (
                        request["payload"]["expected_current"],
                        request["payload"]["expected_manifest_sha256"],
                    ),
                    "baseline_drift",
                )
                self.protected_access(files, current)
                self.unresolved()
                return {"status": "validated", "manifest_sha256": digest(canonical(manifest))}
            if action == "publish":
                return self.publish(request["payload"])
            need("operation_id" in request, "operation_required")
            return self.rollback(operation_id(request["operation_id"]))
        finally:
            os.close(fd)


def main():
    global GROUP, SERVICE_UID
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    signal.alarm(360)
    need(
        sys.argv[1:] == []
        and os.geteuid() == 0
        and socket.gethostname() == "hp-ubuntu2604-server"
        and not os.environ.get("SSH_ORIGINAL_COMMAND"),
        "fixed_hp_root_entry",
    )
    policy = parse(read(POLICY, 65536, mode=0o600))
    need(
        isinstance(policy, dict)
        and set(policy)
        == {
            "schema",
            "helper_sha256",
            "launcher_sha256",
            "unit_sha256",
            "service_user",
            "service_group",
        }
        and policy["schema"] == "hp-quota-shared-maintenance-policy-v1"
        and policy["service_user"] == IDENTITY
        and policy["service_group"] == IDENTITY,
        "policy",
    )
    need(
        Path(__file__) == ENTRY
        and digest(read(ENTRY, MAX_FILE)) == hash_value(policy["helper_sha256"])
        and digest(read(LAUNCHER, MAX_FILE)) == hash_value(policy["launcher_sha256"])
        and digest(read(UNIT, 65536)) == hash_value(policy["unit_sha256"]),
        "host_pin",
    )
    account, group = pwd.getpwnam(IDENTITY), grp.getgrnam(IDENTITY)
    SERVICE_UID, GROUP = account.pw_uid, group.gr_gid
    need(SERVICE_UID != 0 and GROUP != 0, "service_identity")
    fields = dict(
        line.split("=", 1)
        for line in command(
            [
                "/usr/bin/systemctl",
                "show",
                SERVICE,
                "--property=User,Group,FragmentPath,DropInPaths",
            ]
        )
        .decode()
        .splitlines()
    )
    need(
        fields
        == {"User": IDENTITY, "Group": IDENTITY, "FragmentPath": str(UNIT), "DropInPaths": ""},
        "service_identity",
    )
    return Operator().handle(parse(sys.stdin.buffer.read(MAX_JSON + 1)))


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - forced-command output must redact private inputs
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "reason": "inspect_original_operation",
                    "code": str(exc) if isinstance(exc, Refused) else "host_exception",
                    "automatic_retry": False,
                    "provider_calls": 0,
                }
            )
        )
        raise SystemExit(1) from None
