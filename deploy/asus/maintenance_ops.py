"""Installed, reviewed maintenance code. Uploaded releases are DATA, never root code.

Only PID1's fixed maintenance unit writes deployment paths. The socket helper
creates durable requests and starts that unit; it has no caller-controlled paths.
No provider request, database restoration, pip hook, migration or candidate root
import occurs here. All interruption markers are retained.
"""

import base64
import contextlib
import csv
import fcntl
import hashlib
import importlib.util
import io
import itertools
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/usr/local/lib/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops/maintenance")
APP = Path("/opt/api-quota-broker")
SYSTEM = Path("/etc/systemd/system")
CONFIG = Path("/etc/api-quota-broker/gateway.json")
POLICY = Path("/etc/api-quota-broker-ops/policy.json")
ACTIVE = Path("/etc/api-quota-broker-ops/active-release.json")
DB = Path("/var/lib/api-quota-broker/ledger.sqlite3")
BACKUPS = Path("/var/backups/api-quota-broker")
LEGACY = ("normal-pool-v1-2026-10-08-r1.claim.json", "normal-pool-v1-2026-10-08-r1.json")
OLD = "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb"
OLD_CONFIG = "8bcdc98e9a30aaed5adfd7958b0dbef32e647211ce16c322034facee7dd79875"
SERVICE = "api-quota-broker.service"
CLIENT = "api-quota-broker-client.service"
PROFILES = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": ("nvidia", 4096, 65536),
    "gemini-3.5-flash-lite": ("google", 8192, 131328),
    "ministral-3b-latest": ("mistral", 4096, 65536),
    "@cf/meta/llama-3.2-1b-instruct": ("cloudflare", 2048, 32768),
    "liquid/lfm-2.5-2.6b:free": ("openrouter", 4096, 32768),
    "openai/gpt-oss-20b": ("groq", 4096, 32768),
    "ocr.space/engine2": ("ocrspace", 1, 0),
}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
REPAIR_ID = "5d66c9118e144365b2c0331936ec0af7"
REPAIR_MANIFEST = "20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855"

FD_RECOVERY_ID = "e1028cade84c4fc09fa0cbba85d19be0"
FD_RECOVERY_MANIFEST = "510b514833ce06ee491128a8c12d5ab2acb2666d0f9839ee79cdc82e0ec4dda8"
FD_RECOVERY_SOURCE = "968bb6f6a17733e8f07f3c621b6b04eb4118b140023e3f1c99d35c5a209759af"
FD_RECOVERY_WHEEL = "d240b48e84e2c9f6266556972dfb5b5ead470b3d72b14efe7db618b01e9ac685"
FD_RECOVERY_QUEUE = "asus-normal-v1-2026-10-08-r1-queued-auto-a1"
FD_RECOVERY_EXECUTION = "q-5a09fb653fcd4e27a3819c1c7abd104e"


def fd_recovery_gate(con):
    """One reviewed, untouched preparation; never a general quiescence bypass."""
    con.execute("PRAGMA query_only=ON")
    need(con.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    counts = {
        "gateway_tasks": 9,
        "gateway_attempts": 8,
        "reservations": 8,
        "charges": 26,
        "execution_completion": 8,
        "queue_jobs": 1,
        "queue_attempts": 1,
    }
    for table, count in counts.items():
        need(
            con.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == count,
            "queue_not_quiescent",
        )
    row = con.execute(
        "SELECT request_key,state,attempt_count,max_attempts,execution_key,run_started,"
        "wait_policy,payload IS NOT NULL,result IS NULL,expires_at,deadline,lease_until,execution_until "
        "FROM queue_jobs"
    ).fetchone()
    need(
        tuple(row[:9])
        == (FD_RECOVERY_QUEUE, "running", 1, 1, FD_RECOVERY_EXECUTION, 1, "reject", 1, 1),
        "queue_not_quiescent",
    )
    now = datetime.now(UTC)
    need(datetime.fromisoformat(row[9]) > now + timedelta(minutes=5), "queue_not_quiescent")
    need(
        row[10] is None or datetime.fromisoformat(row[10]) > now + timedelta(minutes=5),
        "queue_not_quiescent",
    )
    need(
        all(value and datetime.fromisoformat(value) < now for value in row[11:]),
        "queue_not_quiescent",
    )
    task = con.execute(
        "SELECT state,reservation_id,target_id,provider,model,dispatched_at,completed_at "
        "FROM gateway_tasks WHERE request_key=?",
        (FD_RECOVERY_EXECUTION,),
    ).fetchone()
    need(task == ("preparing", None, None, None, None, None, None), "queue_not_quiescent")
    need(
        con.execute("SELECT request_key,attempt_no,execution_key FROM queue_attempts").fetchall()
        == [(FD_RECOVERY_QUEUE, 1, FD_RECOVERY_EXECUTION)],
        "queue_not_quiescent",
    )
    need(
        con.execute(
            "SELECT count(*) FROM gateway_attempts WHERE request_key=?", (FD_RECOVERY_EXECUTION,)
        ).fetchone()[0]
        == 0,
        "queue_not_quiescent",
    )
    prefix = "gw:" + FD_RECOVERY_EXECUTION + ":"
    need(
        con.execute(
            "SELECT count(*) FROM reservations WHERE substr(request_key,1,?)=?",
            (len(prefix), prefix),
        ).fetchone()[0]
        == 0,
        "queue_not_quiescent",
    )
    need(
        con.execute("SELECT id,length(verifier)>0 FROM queue_settings").fetchall() == [(1, 1)],
        "queue_not_quiescent",
    )
    return 1


def repair_permit(entry, req):
    """One explicitly reviewed continuation; original terminal evidence survives."""
    path = STATE / (req["request_id"] + ".repair-authorized.json")
    if not os.path.lexists(path):
        return False
    need(
        req["request_id"] == REPAIR_ID and req["operation"] in {"deploy", "operation_status"},
        "scope_invalid",
    )
    protocol = module(entry, "maintenance_protocol.py")
    original_req = {"operation": "deploy", "request_id": REPAIR_ID}
    expected_result = wire(
        protocol.receipt(original_req, "blocked", "internal_error", legacy_clear=True, queue_jobs=0)
    )
    need(
        entry.read_root(STATE / (REPAIR_ID + ".result.json"), mode=0o600) == expected_result,
        "foreign_change",
    )
    expected = {
        "schema": 1,
        "request_id": REPAIR_ID,
        "original_result_sha256": digest(expected_result),
        "manifest_sha256": REPAIR_MANIFEST,
        "archive": "releases/failed-stage-" + REPAIR_ID,
    }
    need(entry.strict_json(entry.read_root(path, mode=0o600)) == expected, "scope_invalid")
    return True


def need(ok, code="package_invalid"):
    if not ok:
        raise ValueError(code)


def runtime_capabilities():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    need(int(status["CapEff"].strip(), 16) & 192 == 192, "runtime_invalid")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def wheel_files(raw, source_rows, version="1.0.0"):
    """A genuine pure-Python 1.0 wheel, matched to the release source bytes."""
    with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
        names = wheel.namelist()
        need(len(names) == len(set(names)), "package_invalid")
        source = {
            row["path"].removeprefix("src/"): row["sha256"]
            for row in source_rows
            if row["path"].startswith("src/quota_broker/")
        }
        need(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version))
        dist = "api_quota_broker-" + version + ".dist-info/"
        need(
            set(names)
            == set(source)
            | {dist + n for n in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")},
            "package_invalid",
        )
        need(sum(i.file_size for i in wheel.infolist()) <= 2097152)
        need(all(i.file_size <= 1048576 for i in wheel.infolist()))
        files = {name: wheel.read(name) for name in names}
        for info in wheel.infolist():
            need(
                not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16), "package_invalid"
            )
        for name, sha in source.items():
            need(hashlib.sha256(files[name]).hexdigest() == sha, "package_invalid")
        need(
            ("Version: " + version + "\n").encode() in files[dist + "METADATA"]
            and b"Tag: py3-none-any\n" in files[dist + "WHEEL"],
            "package_invalid",
        )
        records = list(csv.reader(io.StringIO(files[dist + "RECORD"].decode())))
        need(
            len(records) == len(files) and {r[0] for r in records} == set(files), "package_invalid"
        )
        for name, encoded, size in records:
            need(name in files, "package_invalid")
            if name != dist + "RECORD":
                actual = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(files[name]).digest()
                ).decode().rstrip("=")
                need(encoded == actual and int(size) == len(files[name]), "package_invalid")
        need(
            files[dist + "entry_points.txt"].decode().strip()
            == "[console_scripts]\nquota-broker = quota_broker.cli:main",
            "package_invalid",
        )
        return files


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(entry, path, raw, mode=0o600, gid=0):
    temp = path.parent / (".maintenance-" + os.urandom(16).hex())
    entry.write_exclusive(temp, raw, mode=mode, gid=gid)
    os.replace(temp, path)
    sync(path.parent)


def module(entry, name):
    return entry.load_public_module(name, "aqb_" + name.replace(".", "_"))


def profile(entry):
    p = entry.strict_json(entry.read_root(BASE / "maintenance-profile.json", mode=0o644))
    need(p["schema"] == 1 and p["profile"] == "normal-v1" and p["old_release"] == OLD)
    need(re.fullmatch("[a-f0-9]{64}", p["manifest_sha256"]))
    need(p["incoming"] == "/var/tmp/api-quota-broker-normal-v1-review-2026-10-09-r2/source.tar")
    return p


def result(entry, req):
    protocol = module(entry, "maintenance_protocol.py")
    claim = STATE / (req["request_id"] + ".request.json")
    if not claim.exists():
        return protocol.receipt(req, "absent", "receipt_absent")
    original = protocol.request(entry.strict_json(entry.read_root(claim, mode=0o600)))
    need(req["operation"] in {original["operation"], "operation_status"}, "request_conflict")
    repaired = repair_permit(entry, req)
    path = STATE / (req["request_id"] + (".repair-result.json" if repaired else ".result.json"))
    if path.exists():
        value = entry.strict_json(entry.read_root(path, mode=0o600))
        protocol.validate(value, original)
        value["operation"] = req["operation"]
        return protocol.validate(value, req)
    active = entry.native(
        (
            "/usr/bin/systemctl",
            "show",
            "api-quota-broker-maintenance@" + req["request_id"] + ".service",
            "--property=ActiveState",
            "--value",
        )
    ).strip()
    running = active in {b"active", b"activating", b"deactivating"}
    return protocol.receipt(
        req, "running" if running else "unknown", "accepted" if running else "interrupted"
    )


def dispatch(entry, req):
    """Request ID is mandatory and caller-owned; a replay never starts a unit."""
    protocol = module(entry, "maintenance_protocol.py")
    protocol.request(req)
    entry.root_dir(STATE, mode=0o700)
    if req["operation"] == "operation_status":
        return result(entry, req)
    path = STATE / (req["request_id"] + ".request.json")
    if path.exists():
        return result(entry, req)
    if req["operation"] in {"deploy", "restart"}:
        for previous in STATE.glob("*.request.json"):
            prior = protocol.request(entry.strict_json(entry.read_root(previous, mode=0o600)))
            suffix = ".repair-result.json" if repair_permit(entry, prior) else ".result.json"
            if prior["operation"] in {"deploy", "restart", "rollback"} and not os.path.lexists(
                STATE / (prior["request_id"] + suffix)
            ):
                return protocol.receipt(req, "blocked", "interrupted")
    need(len(list(STATE.iterdir())) < 2048, "storage_bound")
    try:
        entry.write_exclusive(path, wire(req))
    except FileExistsError:
        return result(entry, req)
    # Claim durable BEFORE manager dispatch. Failure/timeout leaves it unknown.
    entry.native(
        (
            "/usr/bin/systemctl",
            "start",
            "--no-block",
            "api-quota-broker-maintenance@" + req["request_id"] + ".service",
        )
    )
    return protocol.receipt(req, "pending", "accepted")


def normal_config(original, now=None):
    """Fixed profile; preserve identity, billing, admission expiry and quota scopes."""
    now = now or datetime.now(UTC)
    value = json.loads(wire(original))
    need(set(value) == {"targets"} and len(value["targets"]) == 7)
    need({t["model"] for t in value["targets"]} == set(PROFILES))
    for t in value["targets"]:
        provider, output, inputs = PROFILES[t["model"]]
        need(
            t["provider"] == provider
            and t["enabled"] is True
            and t["free_eligible"] is True
            and t["billing_enabled"] is False
        )
        need(
            datetime.fromisoformat(t["verified_at"])
            <= now
            < datetime.fromisoformat(t["expires_at"]),
            "expiry_invalid",
        )
        t["max_output_tokens"] = output
        caps = [c for c in t["local_safety_caps"] if c["metric"] == "input_tokens"]
        need(bool(caps) or provider == "ocrspace")
        for cap in caps:
            cap["limit"] = inputs
        if provider == "cloudflare":
            estimate = t["neuron_estimate"]
            need(estimate["source"] == "cloudflare_llama_3_2_1b_formula")
            need(now < datetime.fromisoformat(estimate["expires_at"]), "expiry_invalid")
            estimate.update(amount=119, max_input_tokens=32768, max_output_tokens=2048)
    return value


def queue_gate(con, *, first):
    """Historical terminal jobs are allowed; unknown/runnable jobs are never resumed."""
    con.execute("PRAGMA query_only=ON")
    need(con.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
    columns = {row[1] for row in con.execute("PRAGMA table_info(queue_jobs)")}
    need(
        {
            "state",
            "lease_owner",
            "lease_token",
            "lease_until",
            "execution_until",
            "run_started",
            "execution_key",
            "payload",
        }
        <= columns,
        "queue_not_quiescent",
    )
    count = con.execute("SELECT count(*) FROM queue_jobs").fetchone()[0]
    need(not first or count == 0, "queue_not_quiescent")
    need(
        con.execute(
            "SELECT count(*) FROM queue_jobs WHERE state NOT IN "
            "('completed','cancelled','rejected','expired') OR lease_owner IS NOT NULL "
            "OR lease_token IS NOT NULL OR lease_until IS NOT NULL"
        ).fetchone()[0]
        == 0,
        "queue_not_quiescent",
    )
    need(
        not first or con.execute("SELECT count(*) FROM queue_attempts").fetchone()[0] == 0,
        "queue_not_quiescent",
    )
    settings = con.execute("SELECT id,length(verifier) FROM queue_settings").fetchall()
    need(len(settings) == 1 and settings[0][0] == 1 and settings[0][1] > 0, "queue_not_quiescent")
    return count


class Native:
    def __init__(self, entry, req):
        self.e, self.req, self.p = entry, req, profile(entry)
        self.new = "releases/release-" + self.p["manifest_sha256"]
        self.backup = STATE / ("backup-" + req["request_id"])
        self.changed = False
        self.saved = None
        self.jobs = None
        self.legacy_clear = False
        self.current = os.readlink(APP / "current")
        self.initial = self.current == OLD
        self.manifest_sha = self.p["manifest_sha256"]
        self.version = "1.0.0"
        self.wheel = None
        self.recovery = req["request_id"] == FD_RECOVERY_ID
        if self.recovery:
            need(
                req["operation"] == "deploy"
                and self.current == "releases/release-" + REPAIR_MANIFEST,
                "scope_invalid",
            )

    def command(self, *args):
        return self.e.native(("/usr/bin/systemctl", *args), timeout=60)

    def read(self, path, limit=131072):
        return self.e.read_root(path, limit=limit)

    def db(self):
        # Never instantiate application code or run migrations as root.
        return sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=5)

    def preflight(self, *, pins=True):
        self.e.package()
        policy = self.e.runtime_policy()
        need(policy["expires_at"] == "2026-11-05T04:33:31+00:00", "expiry_invalid")
        if pins:
            self.e.broker_pins(policy["config_sha256"])
        self.e.root_dir(STATE, mode=0o700)
        self.before_services = [
            self.e.service_state(n) for n in ("orderflow.service", "ssh.service")
        ]
        self.legacy_clear = all(not os.path.lexists(BACKUPS / n) for n in LEGACY)
        current = os.readlink(APP / "current")
        need(re.fullmatch("releases/release-[a-f0-9]{64}", current), "baseline_changed")
        previous = STATE / "deployment.json"
        if previous.exists() and self.req["operation"] != "rollback":
            previous_value = self.e.strict_json(self.read(previous))
            need(previous_value.get("outcome") in {"passed", "restored"}, "interrupted")
        if self.req["operation"] in {"deploy", "preflight"}:
            # A previous once claim is preserved and blocks new acceptance budget.
            if self.initial:
                need(self.legacy_clear, "legacy_claim_present")
                need(digest(self.read(CONFIG)) == OLD_CONFIG, "baseline_changed")
            normal_config(self.e.strict_json(self.read(CONFIG)))
            meta = self.e.strict_json(
                self.read(CONFIG.parent / "credentials/doppler-metadata.json")
            )
            need(
                meta["project"] == "api-quota-broker"
                and meta["config"] == "dev"
                and meta["access"] == "read"
                and meta["human_dashboard_attested"] is True
                and datetime.fromisoformat(meta["expires_at"])
                > datetime.now(UTC) + timedelta(minutes=10),
                "expiry_invalid",
            )
            for path in (
                SYSTEM / CLIENT,
                Path("/usr/local/bin/aqb"),
                Path("/run/api-quota-broker-client"),
            ):
                if self.initial:
                    need(not os.path.lexists(path), "foreign_change")
            if not self.initial:
                need(
                    digest(self.read(SYSTEM / CLIENT))
                    == digest(self.read(BASE / "client-v1.service")),
                    "foreign_change",
                )
                need(
                    digest(self.read(Path("/usr/local/bin/aqb")))
                    == digest(self.read(BASE / "aqb")),
                    "foreign_change",
                )
        with contextlib.closing(self.db()) as con:
            self.jobs = self.queue_check(con)
        return policy

    def queue_check(self, con):
        if self.recovery:
            return fd_recovery_gate(con)
        return queue_gate(
            con, first=self.initial and self.req["operation"] in {"deploy", "preflight"}
        )

    def upload(self):
        # First activation retains the reviewed fixed packet. Later authorized
        # code-only updates use a directory derived solely from this operation ID.
        directory = (
            Path(self.p["incoming"]).parent
            if self.initial
            else Path("/var/tmp/aqb-release-" + self.req["request_id"])
        )
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def read(name, bound):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                need(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == 1000
                    and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_size <= bound
                )
                value = stream.read(bound + 1)
                need(len(value) == info.st_size)
                return value

        try:
            meta = os.fstat(parent)
            need(meta.st_uid == 1000 and stat.S_IMODE(meta.st_mode) == 0o700)
            raw = read("source.tar", 16777216)
            if self.initial:
                need(digest(raw) == self.p["archive_sha256"])
                self.wheel = self.read(BASE / "project.whl", 2097152)
                need(digest(self.wheel) == self.p["wheel_sha256"])
            else:
                # This digest is an artifact identity, NOT a trust root. Root
                # validates every allowlisted member and never executes any of it.
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                    members = list(itertools.islice(archive, 65))
                    need(len(members) <= 64)
                    manifests = [m for m in members if m.name == "release-manifest.json"]
                    need(len(manifests) == 1)
                    member = manifests[0]
                    need(member.isfile() and member.size <= 131072 and not member.linkname)
                    manifest = archive.extractfile(member).read(131073)
                self.manifest_sha = digest(manifest)
                self.wheel = read("project.whl", 2097152)
        finally:
            os.close(parent)
        verifier = module(self.e, "release_verifier.py")
        verified = verifier.verify_archive(raw, self.manifest_sha)
        self.version = verified["release"]["package_version"]
        need(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version))
        files = wheel_files(self.wheel, verified["release"]["files"], self.version)
        if not self.initial:
            old_dist = list(
                (APP / self.current / "runtime/lib/python3.14/site-packages").glob(
                    "api_quota_broker-*.dist-info/METADATA"
                )
            )
            need(len(old_dist) == 1, "runtime_invalid")
            old_meta = self.read(old_dist[0])
            new_meta = files["api_quota_broker-" + self.version + ".dist-info/METADATA"]

            def deps(meta):
                return sorted(
                    line
                    for line in meta.splitlines()
                    if line.startswith((b"Requires-Dist:", b"Requires-Python:"))
                )

            need(deps(old_meta) == deps(new_meta), "runtime_invalid")
        if self.recovery:
            need(
                self.manifest_sha == FD_RECOVERY_MANIFEST
                and digest(raw) == FD_RECOVERY_SOURCE
                and digest(self.wheel) == FD_RECOVERY_WHEEL,
                "package_invalid",
            )
        self.new = "releases/release-" + self.manifest_sha
        return raw, verifier

    def stage(self):
        # systemd v259 drops CAP_SETUID for explicit User=root + seccomp unless
        # retained in AmbientCapabilities. Fail before writing candidate files.
        runtime_capabilities()
        raw, verifier = self.upload()
        destination = APP / self.new
        need(not os.path.lexists(destination), "release_exists")
        verifier.extract_archive(raw, self.manifest_sha, destination)
        old = APP / self.current / "runtime"
        # Installed old runtime is checked by broker_pins before copying.
        links = {
            str(p.relative_to(old)): str(p.readlink()) for p in old.rglob("*") if p.is_symlink()
        }
        need(
            links
            == {
                "lib64": "lib",
                "bin/python3": "python",
                "bin/python": "/usr/bin/python3.14",
                "bin/python3.14": "python",
            },
            "runtime_invalid",
        )
        need(
            sum(p.stat().st_size for p in old.rglob("*") if p.is_file() and not p.is_symlink())
            < 134217728,
            "runtime_invalid",
        )
        shutil.copytree(
            old,
            destination / "runtime",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        site = destination / "runtime/lib/python3.14/site-packages"
        package = site / "quota_broker"
        need(package.is_dir() and not package.is_symlink(), "runtime_invalid")
        for source in (destination / "src/quota_broker").iterdir():
            need(source.is_file() and source.suffix == ".py", "runtime_invalid")
            shutil.copyfile(source, package / source.name)
            (package / source.name).chmod(0o644)
        manifest = self.e.strict_json((destination / "release-manifest.json").read_bytes())
        raw_wheel = (
            self.wheel if self.wheel is not None else self.read(BASE / "project.whl", limit=2097152)
        )
        files = wheel_files(raw_wheel, manifest["files"], self.version)
        dist_name = "api_quota_broker-" + self.version + ".dist-info"
        historical = destination / "historical-project-distribution"
        historical.mkdir(mode=0o755)
        old_dist = list(site.glob("api_quota_broker-*.dist-info"))
        need(len(old_dist) == 1 and not old_dist[0].is_symlink(), "runtime_invalid")
        old_dist[0].rename(historical / old_dist[0].name)
        (site / dist_name).mkdir(mode=0o755)
        for name, data in files.items():
            if name.startswith("quota_broker/"):
                need((site / name).read_bytes() == data)
            else:
                self.e.write_exclusive(site / name, data, mode=0o644)
        console = destination / "runtime/bin/quota-broker"
        need(console.is_file() and not console.is_symlink(), "runtime_invalid")
        code = (
            "#!"
            + str(destination / "runtime/bin/python")
            + "\nfrom quota_broker.cli import main\nraise SystemExit(main())\n"
        ).encode()
        console.write_bytes(code)
        console.chmod(0o755)
        record = site / dist_name / "RECORD"
        rows = list(csv.reader(io.StringIO(record.read_text())))
        rows.append(
            [
                "../../../bin/quota-broker",
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(code).digest()).decode().rstrip("="),
                str(len(code)),
            ]
        )
        stream = io.StringIO()
        csv.writer(stream).writerows(rows)
        record.write_text(stream.getvalue())
        self.e.write_exclusive(
            destination / "maintenance-runtime-install.json",
            wire(
                {
                    "schema": 1,
                    "basis": "existing_native_dependencies_plus_verified_1_0_wheel",
                    "package_version": self.version,
                    "historical_distribution_metadata_retained_outside_site": True,
                    "manifest_sha256": self.manifest_sha,
                    "root_candidate_execution": False,
                }
            ),
            mode=0o644,
        )
        for directory in [
            destination,
            *(p for p in destination.rglob("*") if p.is_dir() and not p.is_symlink()),
        ]:
            directory.chmod(0o755)
        # Fixed child runs with app uid/gid and no credentials, under PrivateNetwork.
        import pwd

        account = pwd.getpwnam("api-quota-broker")
        need(account.pw_uid > 0 and account.pw_gid > 0, "runtime_invalid")
        identity_check = (
            "import os; from pathlib import Path; "
            + "assert os.getresuid()=="
            + repr((account.pw_uid,) * 3)
            + " and os.getresgid()=="
            + repr((account.pw_gid,) * 3)
            + " and os.getgroups()==[]; "
            + "s=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines()); "
            + "assert all(int(s[k].strip(),16)==0 for k in ('CapEff','CapPrm','CapAmb')); "
            + "assert int(s['NoNewPrivs'].strip())==1; "
        )
        try:
            check = subprocess.run(
                [
                    str(destination / "runtime/bin/python"),
                    "-I",
                    "-B",
                    "-c",
                    identity_check
                    + "import quota_broker,importlib.metadata; assert importlib.metadata.version('api-quota-broker')=="
                    + repr(self.version)
                    + "; assert quota_broker.__version__ == "
                    + repr(self.version),
                ],
                user=account.pw_uid,
                group=account.pw_gid,
                extra_groups=[],
                env=ENV,
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            raise ValueError("runtime_invalid") from None
        need(check.returncode == 0, "runtime_invalid")

    def save_backup(self):
        self.backup.mkdir(mode=0o700)
        paths = {
            "config": CONFIG,
            "policy": POLICY,
            "broker-unit": SYSTEM / SERVICE,
            "active": ACTIVE,
        }
        files = {name: self.read(path) for name, path in paths.items()}
        for name, raw in files.items():
            self.e.write_exclusive(self.backup / name, raw)
        # The audit and whole DB snapshot contain no plaintext provider secret.
        with contextlib.closing(self.db()) as source:
            self.jobs = self.queue_check(source)
            with contextlib.closing(sqlite3.connect(self.backup / "ledger.sqlite3")) as copied:
                source.backup(copied)
                need(copied.execute("PRAGMA quick_check").fetchall() == [("ok",)], "backup_invalid")
        (self.backup / "ledger.sqlite3").chmod(0o600)
        with open(self.backup / "ledger.sqlite3", "rb") as f:
            os.fsync(f.fileno())
        new_config = (
            wire(normal_config(self.e.strict_json(files["config"])))
            if self.initial
            else files["config"]
        )
        policy = self.e.strict_json(files["policy"])
        policy["config_sha256"] = digest(new_config)
        self.saved = {
            "schema": 1,
            "request_id": self.req["request_id"],
            "old_current": self.current,
            "initial": self.initial,
            "outcome": "running",
            "new_current": self.new,
            "files": {k: digest(v) for k, v in files.items()},
            "new_files": {
                "config": digest(new_config),
                "policy": digest(wire(policy)),
                "broker-unit": digest(self.read(BASE / "broker-v1.service")),
                "active": digest(
                    wire(
                        {
                            "release": self.new,
                            "unit_sha256": digest(self.read(BASE / "broker-v1.service")),
                        }
                    )
                ),
            },
        }
        self.e.write_exclusive(self.backup / "index.json", wire(self.saved))
        # Fixed location tracks the only current deployment; never arbitrary path.
        atomic(self.e, STATE / "deployment.json", wire(self.saved))
        sync(self.backup)
        return new_config, wire(policy)

    def replace_owned(self, path, raw, allowed, mode=0o644, gid=0):
        need(digest(self.read(path)) in allowed, "foreign_change")
        atomic(self.e, path, raw, mode, gid)

    def selector(self, target):
        need(
            target in {self.current, self.new}
            and os.readlink(APP / "current") in {self.current, self.new},
            "foreign_change",
        )
        temp = APP / (".maintenance-current-" + os.urandom(16).hex())
        os.symlink(target, temp)
        os.replace(temp, APP / "current")
        sync(APP)

    def deploy(self):
        self.stage()
        # Stop first, then re-check quiescence and snapshot: no race with worker.
        self.changed = True
        self.command("stop", SERVICE)
        new_config, new_policy = self.save_backup()
        if self.initial:
            self.e.write_exclusive(
                SYSTEM / CLIENT, self.read(BASE / "client-v1.service"), mode=0o644
            )
            self.e.write_exclusive(Path("/usr/local/bin/aqb"), self.read(BASE / "aqb"), mode=0o755)
        self.replace_owned(
            SYSTEM / SERVICE,
            self.read(BASE / "broker-v1.service"),
            {self.saved["files"]["broker-unit"]},
        )
        self.replace_owned(CONFIG, new_config, {self.saved["files"]["config"]}, 0o640, 982)
        self.replace_owned(POLICY, new_policy, {self.saved["files"]["policy"]})
        self.replace_owned(
            ACTIVE,
            wire(
                {"release": self.new, "unit_sha256": digest(self.read(BASE / "broker-v1.service"))}
            ),
            {self.saved["files"]["active"]},
        )
        self.selector(self.new)
        self.command("daemon-reload")
        # Broker Wants=client; no new enable symlink is necessary.
        self.command("start", SERVICE)
        self.command("start", CLIENT)
        self.verify()
        self.saved["outcome"] = "passed"
        atomic(self.e, STATE / "deployment.json", wire(self.saved))

    def preserved_rows(self):
        """Compare old rows by SQLite rowid; later inserts/unknown holds survive."""
        backup = self.backup / "ledger.sqlite3"
        if not backup.exists():
            return
        with (
            contextlib.closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as before,
            contextlib.closing(self.db()) as after,
        ):
            before.execute("PRAGMA query_only=ON")
            after.execute("PRAGMA query_only=ON")
            tables = (
                "gateway_tasks",
                "gateway_attempts",
                "reservations",
                "charges",
                "execution_completion",
                "queue_jobs",
                "queue_attempts",
                "queue_settings",
            )
            for table in tables:
                maximum = before.execute('SELECT max(rowid) FROM "' + table + '"').fetchone()[0]
                if maximum is None:
                    continue
                sql = 'SELECT rowid,* FROM "' + table + '" WHERE rowid<=?'
                params = (maximum,)
                # Exactly these two rows are expected to advance in the worker.
                # Every historical ledger/unknown row and original attempt remains exact.
                if self.recovery and table in {"gateway_tasks", "queue_jobs"}:
                    sql += " AND request_key<>?"
                    params += (
                        FD_RECOVERY_EXECUTION if table == "gateway_tasks" else FD_RECOVERY_QUEUE,
                    )
                sql += " ORDER BY rowid"

                def rows(con, sql=sql, params=params):
                    total = 0
                    hashed = hashlib.sha256()
                    for row in con.execute(sql, params):
                        total += 1
                        need(total <= 1000000, "backup_invalid")
                        raw = wire([{"blob": v.hex()} if isinstance(v, bytes) else v for v in row])
                        hashed.update(len(raw).to_bytes(8, "big"))
                        hashed.update(raw)
                    return total, hashed.digest()

                need(rows(before) == rows(after), "foreign_change")

    def verify(self):
        if self.saved is not None:
            self.preserved_rows()
        self.e.broker_pins(self.e.runtime_policy()["config_sha256"])
        self.e.service_state()
        need(
            self.before_services
            == [self.e.service_state(n) for n in ("orderflow.service", "ssh.service")],
            "foreign_change",
        )

    def restore(self):
        if self.saved is None:
            if self.req["operation"] != "rollback":
                # This attempt has not installed anything. Never restore a
                # PREVIOUS successful deployment because current staging failed.
                with contextlib.closing(self.db()) as con:
                    queue_gate(con, first=False)
                self.command("start", SERVICE)
                if self.current != OLD:
                    self.command("start", CLIENT)
                self.verify()
                return
            need((STATE / "deployment.json").exists(), "backup_invalid")
            self.saved = self.e.strict_json(self.read(STATE / "deployment.json"))
            need(re.fullmatch("[a-f0-9]{32}", self.saved["request_id"]), "backup_invalid")
            self.backup = STATE / ("backup-" + self.saved["request_id"])
        need(
            all(
                re.fullmatch("releases/release-[a-f0-9]{64}", self.saved[k])
                for k in ("old_current", "new_current")
            ),
            "backup_invalid",
        )
        self.current, self.new = self.saved["old_current"], self.saved["new_current"]
        self.initial = self.saved["initial"]
        self.command("stop", SERVICE)
        # Never restore ledger.sqlite3: post-deployment holds and queue rows survive.
        for name, path, mode, gid in [
            ("config", CONFIG, 0o640, 982),
            ("policy", POLICY, 0o644, 0),
            ("broker-unit", SYSTEM / SERVICE, 0o644, 0),
            ("active", ACTIVE, 0o644, 0),
        ]:
            raw = self.read(self.backup / name)
            need(digest(raw) == self.saved["files"][name], "backup_invalid")
            self.replace_owned(
                path, raw, {self.saved["files"][name], self.saved["new_files"][name]}, mode, gid
            )
        if self.initial and os.path.lexists(SYSTEM / CLIENT):
            need(
                digest(self.read(SYSTEM / CLIENT)) == digest(self.read(BASE / "client-v1.service")),
                "foreign_change",
            )
            self.command("stop", CLIENT)
            need(
                not os.path.lexists(Path("/run/api-quota-broker-client/client_token")),
                "rollback_unverified",
            )
            (SYSTEM / CLIENT).unlink()
        wrapper = Path("/usr/local/bin/aqb")
        if self.initial and os.path.lexists(wrapper):
            need(digest(self.read(wrapper)) == digest(self.read(BASE / "aqb")), "foreign_change")
            wrapper.unlink()
        self.selector(self.current)
        self.command("daemon-reload")
        self.command("start", SERVICE)
        if not self.initial:
            self.command("start", CLIENT)
        self.verify()
        self.saved["outcome"] = "restored"
        atomic(self.e, STATE / "deployment.json", wire(self.saved))

    def restart(self):
        self.command("stop", SERVICE)
        self.changed = True
        with contextlib.closing(self.db()) as con:
            self.jobs = queue_gate(con, first=False)
        self.command("start", SERVICE)
        if os.readlink(APP / "current") != OLD:
            self.command("start", CLIENT)
        self.verify()


def execute(entry, req, backend=Native):
    protocol = module(entry, "maintenance_protocol.py")
    protocol.request(req)
    ops = backend(entry, req)
    restored = False
    try:
        ops.preflight(pins=req["operation"] != "rollback")
        if req["operation"] == "preflight":
            ops.upload()
        elif req["operation"] == "deploy":
            ops.deploy()
        elif req["operation"] == "restart":
            ops.restart()
        elif req["operation"] == "rollback":
            ops.restore()
            restored = True
        else:
            need(False, "scope_invalid")
        return protocol.receipt(
            req,
            "passed",
            "ok",
            restored=restored,
            legacy_clear=ops.legacy_clear,
            queue_jobs=ops.jobs,
        )
    except BaseException as error:  # noqa: BLE001 - safe categories only
        code = (
            error.args[0]
            if isinstance(error, ValueError) and len(error.args) == 1
            else "internal_error"
        )
        if code not in protocol.CODES:
            code = "internal_error"
        if ops.changed and req["operation"] == "deploy" and getattr(ops, "recovery", False):
            # New worker may already have dispatched. Do not restart old code or
            # replay deployment on ambiguous failure; leave the live DB intact.
            code = "rollback_unverified"
            with contextlib.suppress(BaseException):
                ops.command("stop", SERVICE)
        elif ops.changed and req["operation"] == "deploy":
            try:
                ops.restore()
                restored = True
            except BaseException:  # noqa: BLE001 - preserve durable unknown evidence
                code = "rollback_unverified"
                with contextlib.suppress(BaseException):
                    ops.command("stop", SERVICE)
        return protocol.receipt(
            req,
            "blocked",
            code,
            restored=restored,
            legacy_clear=ops.legacy_clear,
            queue_jobs=ops.jobs,
        )


def main():
    need(len(sys.argv) == 2 and re.fullmatch("[a-f0-9]{32}", sys.argv[1]), "scope_invalid")
    rid = sys.argv[1]
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server", "scope_invalid")
    name = "api-quota-broker-maintenance@" + rid + ".service"
    need(
        Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + name, "scope_invalid"
    )
    group = Path("/sys/fs/cgroup/system.slice") / name
    need(
        (group / "memory.max").read_text().strip() == "402653184"
        and (group / "memory.swap.max").read_text().strip() == "0",
        "scope_invalid",
    )
    need(
        os.statvfs(DB).f_flag & os.ST_RDONLY and os.statvfs(BACKUPS).f_flag & os.ST_RDONLY,
        "scope_invalid",
    )
    import ctypes

    libc = ctypes.CDLL(None)
    need(libc.prctl(39, 0, 0, 0, 0) == 1, "scope_invalid")
    buf = ctypes.create_string_buffer(512)
    need(
        libc.statfs(b"/tmp", ctypes.byref(buf)) == 0
        and ctypes.c_long.from_buffer(buf).value == 0x01021994,
        "scope_invalid",
    )
    spec = importlib.util.spec_from_file_location("aqb_installed_entry", BASE / "ops_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.prevent_process_dumps()
    entry.package()
    entry.runtime_policy()
    protocol = module(entry, "maintenance_protocol.py")
    req = protocol.request(
        entry.strict_json(entry.read_root(STATE / (rid + ".request.json"), mode=0o600))
    )
    need(req["request_id"] == rid and req["operation"] != "operation_status", "scope_invalid")
    entry.root_dir(STATE, mode=0o700)
    fd = os.open(STATE.parent / "operation.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        need(info.st_uid == 0 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Persistent execution marker prevents manual systemd start from replaying.
        repaired = repair_permit(entry, req)
        suffix = ".repair-started" if repaired else ".started"
        entry.write_exclusive(STATE / (rid + suffix), b"1\n")
        answer = execute(entry, req)
        suffix = ".repair-result.json" if repaired else ".result.json"
        entry.write_exclusive(STATE / (rid + suffix), wire(answer))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - preserve durable unknown evidence
        # No result means unknown; fixed unit status is queryable, no auto replay.
        raise SystemExit(1) from None
