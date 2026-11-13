"""HP-only forced-command publisher. Bootstrap is separate; no root app execution.

Public protocol: status, validate, publish, rollback. Runtime paths, unit,
launcher and HTTP checks are fixed. Never accepts a command or credential.
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
import stat
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from uuid import UUID

OWNER = 0
GROUP = 0  # Set only from root-owned bootstrap policy in main().
BASE = Path("/opt/momonong-quota")
STATE = Path("/var/lib/momonong-quota-maintenance")
LOCK = Path("/run/momonong-quota-update.lock")
POLICY = Path("/etc/momonong-quota-maintenance/policy.json")
ENTRY = Path("/usr/local/libexec/hp-quota-maintenance.py")
LAUNCHER = Path("/usr/local/libexec/momonong-quota-launcher.py")
UNIT = Path("/etc/systemd/system/api-quota-dashboard.service")
SERVICE = "api-quota-dashboard.service"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
MAX_FILE, MAX_TOTAL, MAX_JSON = 4 * 1024**2, 12 * 1024**2, 20 * 1024**2
NAME = re.compile("[a-z0-9][a-z0-9_-]{0,95}")
HASH = re.compile("[a-f0-9]{64}")
TERMINAL = frozenset({"complete", "rolled_back", "rejected"})
ALLOWED = (
    "app/quota_broker/__init__.py",
    "app/quota_broker/access.py",
    "app/quota_broker/bounded_curl.py",
    "app/quota_broker/catalog.py",
    "app/quota_broker/cli.py",
    "app/quota_broker/client.py",
    "app/quota_broker/client_credentials.py",
    "app/quota_broker/config.py",
    "app/quota_broker/core.py",
    "app/quota_broker/crypto_admin.py",
    "app/quota_broker/crypto_config.py",
    "app/quota_broker/dashboard.py",
    "app/quota_broker/dashboard_accounts.py",
    "app/quota_broker/dashboard_assets/dashboard.css",
    "app/quota_broker/dashboard_assets/dashboard.js",
    "app/quota_broker/dashboard_assets/index.html",
    "app/quota_broker/dashboard_assets/provider-inventory.json",
    "app/quota_broker/dashboard_auth.py",
    "app/quota_broker/dashboard_demo.py",
    "app/quota_broker/dashboard_provider_metadata.py",
    "app/quota_broker/dashboard_reviewed.py",
    "app/quota_broker/dashboard_server.py",
    "app/quota_broker/discovery.py",
    "app/quota_broker/discovery_parsers.py",
    "app/quota_broker/discovery_sources.py",
    "app/quota_broker/encrypted_transport.py",
    "app/quota_broker/execution_policy.py",
    "app/quota_broker/families.py",
    "app/quota_broker/family_adapters.py",
    "app/quota_broker/family_transport.py",
    "app/quota_broker/gateway.py",
    "app/quota_broker/gateway_providers.py",
    "app/quota_broker/gateway_server.py",
    "app/quota_broker/key_admin.py",
    "app/quota_broker/local_jobs.py",
    "app/quota_broker/nvidia.py",
    "app/quota_broker/nvidia_server.py",
    "app/quota_broker/provider_policy.py",
    "app/quota_broker/queue.py",
    "app/quota_broker/registry.py",
    "app/quota_broker/retry.py",
    "app/quota_broker/routing.py",
    "app/quota_broker/secure_client.py",
    "app/quota_broker/server.py",
    "app/quota_broker/task_matching.py",
    "app/quota_broker/token_preflight.py",
    "metadata/accounts.json",
    "metadata/gateway.json",
    "metadata/provider-observations.json",
    "metadata/registry.json",
    "metadata/reviewed.json",
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
        },
        "payload_schema",
    )
    need(payload["schema"] == "hp-quota-release-v1", "payload_schema")
    operation_id(payload["operation_id"])
    identifier(payload["release_id"])
    identifier(payload["expected_current"])
    hash_value(payload["expected_manifest_sha256"])
    need(payload["release_id"] != payload["expected_current"], "same_release")
    rows = payload["files"]
    need(isinstance(rows, list) and 0 < len(rows) <= 128, "file_count")
    manifest = []
    files = {}
    total = 0
    for row in rows:
        need(
            isinstance(row, dict) and set(row) == {"path", "size", "sha256", "content_base64"},
            "file_schema",
        )
        path = row["path"]
        need(isinstance(path, str) and path in ALLOWED and path not in files, "file_scope")
        need(type(row["size"]) is int and 0 <= row["size"] <= MAX_FILE, "file_size")
        need(
            isinstance(row["content_base64"], str)
            and len(row["content_base64"]) <= ((MAX_FILE + 2) // 3) * 4,
            "encoded_size",
        )
        try:
            raw = base64.b64decode(row["content_base64"], validate=True)
        except ValueError:
            raise Refused("base64_invalid") from None
        need(len(raw) == row["size"] and digest(raw) == row["sha256"], "content_digest")
        total += len(raw)
        need(total <= MAX_TOTAL, "total_size")
        # Parse only; never import/execute the publisher's Python input as root.
        try:
            if path.endswith(".py"):
                ast.parse(raw, filename=path)
            elif path.endswith(".json"):
                parse(raw)
        except (ValueError, SyntaxError, RecursionError):
            raise Refused("syntax_invalid") from None
        manifest.append({key: row[key] for key in ("path", "size", "sha256")})
        files[path] = raw
    need(tuple(files) == ALLOWED, "exact_files_required")
    return manifest, files


def directory(path):
    info = path.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
        "directory_metadata",
    )


def read(path, bound=MAX_FILE):
    # Caller pins the root; no candidate-controlled parent may be a symlink.
    for parent in path.parents:
        need(stat.S_ISDIR(parent.lstat().st_mode), "parent_symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == OWNER
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= bound,
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


def write_new(path, raw, mode=0o600, group=0):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), OWNER, group)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def command(argv, timeout=30):
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
        timeout=timeout,
        check=False,
    )
    need(result.returncode == 0, "service_command_failed")
    return result.stdout


def restart_service():
    command(["/usr/bin/systemctl", "restart", SERVICE])


def verify_service(manifest):
    assets = {row["path"]: row["sha256"] for row in manifest}

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
        expected = [
            b"/usr/bin/python3",
            b"-I",
            b"-B",
            os.fsencode(LAUNCHER),
            b"dashboard-serve",
            b"--reviewed-config",
            os.fsencode(BASE / "current/metadata/reviewed.json"),
            b"--accounts-file",
            os.fsencode(BASE / "current/metadata/accounts.json"),
            b"--provider-metadata-file",
            os.fsencode(BASE / "current/metadata/provider-observations.json"),
            b"--dashboard-password-file",
            b"/etc/api-quota-dashboard/browser-auth.json",
            b"--public-base-url",
            b"https://momonong.me/quota/",
            b"--port",
            b"18586",
        ]
        need(
            (Path("/proc") / fields["MainPID"] / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            == expected,
            "process_mismatch",
        )
        checks = [
            ("/quota/", "index.html", 200),
            ("/quota/dashboard.js", "dashboard.js", 200),
            ("/quota/dashboard.css", "dashboard.css", 200),
            ("/quota/api/snapshot", None, 401),
            ("/quota/api/usage", None, 401),
            ("/quota/api/maintenance", None, 404),
            ("/quota/api/v1/tasks", None, 404),
            ("/quota/provider-inventory.json", None, 404),
        ]
        for path, asset, status in checks:
            conn = HTTPConnection("127.0.0.1", 18586, timeout=2)
            try:
                conn.request("GET", path, headers={"Host": "momonong.me"})
                response = conn.getresponse()
                body = response.read(MAX_FILE + 1)
                need(len(body) <= MAX_FILE and response.status == status, "http_boundary")
                need("no-store" in response.getheader("Cache-Control", ""), "cache_boundary")
                if asset:
                    need(
                        digest(body) == assets["app/quota_broker/dashboard_assets/" + asset],
                        "http_asset_mismatch",
                    )
            finally:
                conn.close()

    deadline = time.monotonic() + 20
    while True:
        try:
            once()
            return
        except (OSError, Refused, subprocess.SubprocessError, ValueError):
            if time.monotonic() >= deadline:
                raise Refused("readiness_failed") from None
            time.sleep(0.25)


class Operator:
    def __init__(self, root=BASE, state=STATE, lock=LOCK):
        self.root, self.state, self.lock = root, state, lock

    def manifest(self, release):
        location = self.root / "releases" / identifier(release)
        directory(location)
        rows = parse(read(location / "manifest.json", 128 * 1024))
        need(isinstance(rows, list) and 0 < len(rows) <= 128, "manifest_shape")
        paths = []
        total = 0
        for row in rows:
            need(isinstance(row, dict) and set(row) == {"path", "size", "sha256"}, "manifest_shape")
            path = row["path"]
            need(path in ALLOWED and path not in paths, "manifest_path")
            need(type(row["size"]) is int and 0 <= row["size"] <= MAX_FILE, "manifest_size")
            raw = read(location / path)
            need(len(raw) == row["size"] and digest(raw) == row["sha256"], "installed_file_drift")
            total += len(raw)
            paths.append(path)
        legacy = tuple(p for p in ALLOWED if p != "app/quota_broker/task_matching.py")
        need(tuple(paths) in (ALLOWED, legacy) and total <= MAX_TOTAL, "installed_file_set")
        # Reject undeclared extra code/data and any writable or aliased directory.
        actual = []
        for parent, dirs, files in os.walk(location, followlinks=False):
            directory(Path(parent))
            for name in dirs:
                directory(Path(parent) / name)
            for name in files:
                actual.append((Path(parent) / name).relative_to(location).as_posix())
        need(set(actual) == set(paths) | {"manifest.json"}, "installed_extra_file")
        return rows

    def current(self):
        directory(self.root)
        directory(self.root / "releases")
        link = self.root / "current"
        info = link.lstat()
        need(stat.S_ISLNK(info.st_mode) and info.st_uid == OWNER, "current_metadata")
        value = os.readlink(link)
        need(value.startswith("releases/") and value.count("/") == 1, "current_target")
        release = identifier(value.split("/")[1])
        rows = self.manifest(release)
        return release, digest(canonical(rows)), rows

    def stage(self, release, manifest, files):
        location = self.root / "releases" / identifier(release)
        location.mkdir(mode=0o750)
        location.chmod(0o750)
        if os.geteuid() == 0:
            os.chown(location, OWNER, GROUP)
        fsync_dir(location.parent)
        for path, raw in files.items():
            target = location / path
            parents = []
            parent = target.parent
            while parent != location:
                parents.append(parent)
                parent = parent.parent
            for parent in reversed(parents):
                if not parent.exists():
                    parent.mkdir(mode=0o750)
                    parent.chmod(0o750)
                    if os.geteuid() == 0:
                        os.chown(parent, OWNER, GROUP)
                    fsync_dir(parent.parent)
                directory(parent)
            write_new(target, raw, mode=0o640, group=GROUP)
        write_new(location / "manifest.json", canonical(manifest), mode=0o640, group=GROUP)
        self.manifest(release)

    def receipt(self, operation):
        raw = parse(read(self.state / (operation_id(operation) + ".json"), 65536))
        need(isinstance(raw, dict) and raw.get("operation_id") == operation, "receipt_identity")
        for key in ("release_id", "previous"):
            identifier(raw.get(key))
        for key in ("manifest_sha256", "previous_manifest_sha256"):
            hash_value(raw.get(key))
        need(
            raw.get("status")
            in TERMINAL | {"claimed", "switching", "restarting", "rolling_back", "unknown"},
            "receipt_status",
        )
        return raw

    def save(self, receipt, new=False):
        path = self.state / (operation_id(receipt["operation_id"]) + ".json")
        if new:
            write_new(path, canonical(receipt))
            return
        # Retain each prior phase as immutable history before changing the head.
        prior = read(path, 65536)
        history = self.state / "history"
        directory(history)
        backup = history / (receipt["operation_id"] + "-" + digest(prior) + ".json")
        if not backup.exists():
            write_new(backup, prior)
        temporary = path.with_suffix(".pending")
        write_new(temporary, canonical(receipt))
        os.replace(temporary, path)
        fsync_dir(self.state)

    def switch(self, release, operation):
        self.manifest(release)
        temporary = self.root / (".current-" + operation_id(operation))
        target = "releases/" + identifier(release)
        if temporary.is_symlink() or temporary.exists():
            info = temporary.lstat()
            need(
                stat.S_ISLNK(info.st_mode)
                and info.st_uid == OWNER
                and os.readlink(temporary) == target,
                "foreign_temporary_link",
            )
        else:
            os.symlink(target, temporary)
        os.replace(temporary, self.root / "current")
        fsync_dir(self.root)

    def unresolved(self, except_id=None):
        receipts = list(self.state.glob("*.json"))
        need(len(receipts) <= 4096, "receipt_count_bound")
        for path in receipts:
            record = self.receipt(path.stem)
            need(
                record["operation_id"] == except_id or record["status"] in TERMINAL,
                "unresolved_operation",
            )
        need(not list(self.state.glob("*.pending")), "unresolved_receipt_write")

    def recover_pending(self, record):
        pending = self.state / (record["operation_id"] + ".pending")
        if not pending.exists() and not pending.is_symlink():
            return
        raw = read(pending, 65536)
        try:
            parsed = parse(raw)
        except Refused:
            parsed = None  # Interrupted root-owned write; retain bytes, trust only old head.
        if parsed is not None:
            need(
                isinstance(parsed, dict)
                and all(
                    parsed.get(k) == record[k]
                    for k in (
                        "operation_id",
                        "release_id",
                        "manifest_sha256",
                        "previous",
                        "previous_manifest_sha256",
                    )
                ),
                "pending_identity_drift",
            )
        history = (
            self.state
            / "history"
            / (record["operation_id"] + "-interrupted-" + digest(raw) + ".json")
        )
        if not history.exists():
            write_new(history, raw)
        pending.unlink()
        fsync_dir(self.state)

    def restore(self, record):
        operation = record["operation_id"]
        try:
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
            record["status"] = "rolling_back"
            self.save(record)
            if current != record["previous"]:
                self.switch(record["previous"], operation)
            restart_service()
            verify_service(old)
            need(
                self.current()[:2] == (record["previous"], record["previous_manifest_sha256"]),
                "rollback_readback",
            )
            record["status"] = "rolled_back"
            self.save(record)
        except Exception:  # noqa: BLE001 - preserve ambiguous operation; never reflect private error
            record["status"] = "unknown"
            self.save(record)
        return record

    def publish(self, payload):
        manifest, files = validate(payload)
        operation = payload["operation_id"]
        self.unresolved()
        need(not (self.state / (operation + ".json")).exists(), "operation_already_recorded")
        need(
            not (self.root / "releases" / payload["release_id"]).exists(), "release_already_exists"
        )
        current, sha, old = self.current()
        need(
            (current, sha) == (payload["expected_current"], payload["expected_manifest_sha256"]),
            "baseline_drift",
        )
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
            record["status"] = "switching"
            self.save(record)
            self.switch(payload["release_id"], operation)
            record["status"] = "restarting"
            self.save(record)
            restart_service()
            verify_service(manifest)
            need(
                self.current()[:2] == (payload["release_id"], record["manifest_sha256"]),
                "publish_readback",
            )
            record["status"] = "complete"
            self.save(record)
        except Exception:  # noqa: BLE001 - preserve ambiguous operation; never reflect private error
            # A partial staging failure cannot cause a foreign release rollback.
            self.restore(record)
        return self.project(record)

    def project(self, record=None):
        try:
            current, sha, _ = self.current()
        except (OSError, Refused):
            current = sha = None
        result = {
            "current": current,
            "manifest_sha256": sha,
            "status": record["status"]
            if record
            else ("ready" if current is not None else "unverified"),
            "provider_calls": 0,
            "paid_allowed": False,
        }
        if record:
            result.update(
                operation_id=record["operation_id"],
                release_id=record["release_id"],
                release_manifest_sha256=record["manifest_sha256"],
                previous=record["previous"],
                previous_manifest_sha256=record["previous_manifest_sha256"],
            )
        return result

    def handle(self, request):
        need(
            isinstance(request, dict) and request.get("schema") == "hp-quota-maintenance-v1",
            "wire_schema",
        )
        action = request.get("action")
        need(action in {"status", "validate", "publish", "rollback"}, "action")
        allowed = {"schema", "action"} | (
            {"payload"} if action in {"validate", "publish"} else {"operation_id"}
        )
        need(not set(request) - allowed, "wire_fields")
        if action in {"validate", "publish"}:
            need("payload" in request, "payload_required")
        if "operation_id" in request:
            operation_id(request["operation_id"])
        directory(self.root)
        directory(self.state)
        # Bootstrap must create this root-only journal directory and lock.
        directory(self.state / "history")
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
                return self.project(
                    self.receipt(request["operation_id"]) if "operation_id" in request else None
                )
            if action == "validate":
                manifest, _ = validate(request["payload"])
                return {"status": "validated", "manifest_sha256": digest(canonical(manifest))}
            if action == "publish":
                return self.publish(request["payload"])
            need("operation_id" in request, "operation_required")
            record = self.receipt(request["operation_id"])
            need(
                self.current()[:2]
                in {
                    (record["release_id"], record["manifest_sha256"]),
                    (record["previous"], record["previous_manifest_sha256"]),
                },
                "foreign_current",
            )
            self.recover_pending(record)
            self.unresolved(request["operation_id"])
            need(record["status"] != "rolled_back", "operation_already_recorded")
            return self.project(self.restore(record))
        finally:
            os.close(fd)


def main():
    global GROUP
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    signal.alarm(240)
    try:
        need(
            len(sys.argv) == 1
            and os.geteuid() == 0
            and socket.gethostname() == "hp-ubuntu2604-server",
            "fixed_hp_root_entry",
        )
        need(not os.environ.get("SSH_ORIGINAL_COMMAND"), "remote_command_forbidden")
        policy = parse(read(POLICY, 65536))
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
            },
            "bootstrap_policy",
        )
        need(policy["schema"] == "hp-quota-maintenance-policy-v1", "bootstrap_policy")
        need(
            Path(__file__) == ENTRY
            and digest(read(ENTRY, 1024 * 1024)) == hash_value(policy["helper_sha256"]),
            "helper_pin",
        )
        need(
            digest(read(LAUNCHER, 65536)) == hash_value(policy["launcher_sha256"])
            and digest(read(UNIT, 65536)) == hash_value(policy["unit_sha256"]),
            "host_pin",
        )
        user = pwd.getpwnam(policy["service_user"])
        group = grp.getgrnam(policy["service_group"])
        need(user.pw_uid != 0 and group.gr_gid != 0, "service_identity")
        GROUP = group.gr_gid
        values = dict(
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
            values
            == {
                "User": policy["service_user"],
                "Group": policy["service_group"],
                "FragmentPath": str(UNIT),
                "DropInPaths": "",
            },
            "service_identity",
        )
        raw = sys.stdin.buffer.read(MAX_JSON + 1)
        result = Operator().handle(parse(raw))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - never reflect private error
        # No payload contents, private state or system stderr escapes this boundary.
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "reason": str(exc) if isinstance(exc, Refused) else "inspect_original_receipt",
                    "automatic_retry": False,
                    "provider_calls": 0,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
