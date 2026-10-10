"""Reviewed 1.0 upgrade transaction; r2 data and unknown quota holds survive.

Default is a zero-effect plan. Execution requires the sealed root two-phase
wrapper. The ordinary-user probes share one budget of at most three requests.
"""

import base64
import csv
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKUPS = Path("/var/backups/api-quota-broker")
CLAIM = BACKUPS / "normal-pool-v1-2026-10-08-r1.claim.json"
JOURNAL = BACKUPS / "normal-pool-v1-2026-10-08-r1.json"
UNIT = "api-quota-broker-normal-v1.service"
AUDIT_UNIT = "api-quota-broker-normal-audit.service"
CLIENT_UNIT = "api-quota-broker-client.service"
CLIENT_UNIT_PATH = Path("/etc/systemd/system") / CLIENT_UNIT
BROKER_UNIT_PATH = Path("/etc/systemd/system/api-quota-broker.service")
WRAPPER_PATH = Path("/usr/local/bin/aqb")
OLD_RELEASE = "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb"
OLD_CONFIG_SHA = "8bcdc98e9a30aaed5adfd7958b0dbef32e647211ce16c322034facee7dd79875"
OLD_UNIT_SHA = "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060"
OLD_ENTRY_SHA = "ab54fbba16232c173e7838510f1f7c387dcf2e6d40bb20eea01d377f31bd2a88"
PREFIX = "asus-normal-v1-2026-10-08-r1-"
QUEUE_KEY = PREFIX + "queued-auto-a1"
GOOGLE_KEY = PREFIX + "google-long-a1"
CF_KEY = PREFIX + "cloudflare-long-a1"
PROMPT = (
    "For a public fictional library, write a practical guide of approximately 400 words "
    "with eight numbered sections on organizing a weekly reading club. Include concrete "
    "steps, supplies, and a short checklist. Return the finished guide, not READY."
)
GOOGLE_PROMPT = (
    PROMPT + "\nPublic fictional notes: " + ("The library has chairs, books, and a clock. " * 60)
)

SAFE_STAGES = {
    "entry",
    "argument_gate",
    "package_metadata",
    "seal_verification",
    "module_import",
    "sandbox_gate",
    "readonly_audit",
    "unit_verify",
    "history_projection",
    "queue_gate",
    "apply_transaction",
}
SAFE_CODES = {
    "root_scope",
    "package_untrusted",
    "account_evidence_expired",
    "baseline_changed",
    "claim_collision",
    "client_cleanup_unverified",
    "client_identity",
    "client_path_changed",
    "client_path_exists",
    "client_request_unverified",
    "credential_metadata",
    "existing_queue_work",
    "ledger_changed",
    "native_unit_verify_failed",
    "normal_limits_unverified",
    "ops_pin_changed",
    "ops_pin_sync",
    "post_budget",
    "queue_foreign_work",
    "queue_result_unknown",
    "queue_schema_unverified",
    "restart_budget",
    "result_identity",
    "selector_or_config_changed",
    "unit_changed",
    "wheel_unverified",
    "worker_unit_unverified",
    "audit_sqlite_unavailable",
    "audit_sql_timeout",
    "audit_schema_missing_columns",
    "audit_queue_active",
    "ledger_schema",
    "ledger_integrity",
    "private_metadata_untrusted",
    "history_consistency",
    "history_changed",
    "history_scope_unknown",
    "history_bound",
    "history_ambiguous",
    "groq_evidence_missing",
    "ops_pin_drift",
    "package_unverified",
    "upgrade_receipt_unknown",
    "normal_pool_entry_gate",
}
AUDIT_DIAGNOSTIC = {"stage": "readonly_audit", "returncode": None}


def failure_projection(error, *, stage="entry", returncode=None):
    """Only immutable categories cross the child→wrapper→human boundary."""
    candidate = error.args[0] if isinstance(error, ValueError) and len(error.args) == 1 else None
    code = (
        candidate
        if isinstance(candidate, str) and candidate in SAFE_CODES
        else "normal_pool_entry_gate"
    )
    return {
        "status": "blocked",
        "stage": stage if stage in SAFE_STAGES else "entry",
        "code": code,
        "returncode": returncode if type(returncode) is int and -128 <= returncode <= 255 else None,
        "automatic_retry": False,
        "provider_posts": None if stage == "apply_transaction" else 0,
        "provider_posts_status": "unknown_check_durable_claim_and_ledger"
        if stage == "apply_transaction"
        else "not_authorized_in_this_stage",
    }


def private_audit_tmp_gate():
    """Writable ephemeral /tmp only; the host /var/tmp payload stays visible."""
    info = Path("/tmp").lstat()
    buf = ctypes.create_string_buffer(512)
    libc = ctypes.CDLL(None)
    need(
        libc.statfs(b"/tmp", ctypes.byref(buf)) == 0
        and ctypes.c_long.from_buffer(buf).value == 0x01021994
        and info.st_uid == info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == 0o700
        and not os.statvfs("/tmp").f_flag & os.ST_RDONLY,
        "root_scope",
    )


class Blocked(ValueError):
    pass


def need(value, code):
    if not value:
        raise Blocked(code)


def probes():
    return [
        {
            "request_key": QUEUE_KEY,
            "capability": "text_generation",
            "input": PROMPT,
            "max_output_tokens": 2048,
            "max_attempts": 1,
            "wait_policy": "reject",
        },
        {
            "request_key": GOOGLE_KEY,
            "capability": "text_generation",
            "input": GOOGLE_PROMPT,
            "provider": "google",
            "model": "gemini-3.5-flash-lite",
            "max_output_tokens": 2048,
            "max_attempts": 1,
            "wait_policy": "reject",
        },
        {
            "request_key": CF_KEY,
            "capability": "text_generation",
            "input": PROMPT,
            "provider": "cloudflare",
            "model": "@cf/meta/llama-3.2-1b-instruct",
            "max_output_tokens": 2048,
            "max_attempts": 1,
            "wait_policy": "reject",
        },
    ]


def empty_queue_gate(con):
    """Inspect authoritative schema/states/leases; no constructor or maintenance."""
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    need(
        {n for n in names if n.startswith("queue_")}
        == {"queue_jobs", "queue_attempts", "queue_settings"},
        "queue_schema_unverified",
    )
    columns = {row[1] for row in con.execute("PRAGMA table_info(queue_jobs)")}
    need(
        {
            "request_key",
            "state",
            "next_retry_at",
            "deadline",
            "lease_owner",
            "lease_token",
            "lease_until",
            "execution_until",
            "execution_key",
            "run_started",
            "payload",
        }
        <= columns,
        "queue_schema_unverified",
    )
    rows = con.execute(
        "SELECT state,next_retry_at,deadline,lease_owner,lease_token,lease_until,"
        "execution_until,execution_key,run_started,payload FROM queue_jobs LIMIT 1"
    ).fetchall()
    # Any row, even a terminal/scheduled/foreign row, requires a separate reviewed
    # provenance decision. Never clear, expire, cancel, or resume it here.
    need(
        not rows and con.execute("SELECT count(*) FROM queue_attempts").fetchone()[0] == 0,
        "existing_queue_work",
    )
    settings = con.execute("SELECT id,length(verifier) FROM queue_settings").fetchall()
    need(
        len(settings) == 1 and settings[0][0] == 1 and settings[0][1] > 0, "queue_schema_unverified"
    )
    return {
        "jobs": 0,
        "attempts": 0,
        "unknown_scheduled_resumable": 0,
        "schema_verified": True,
        "existing_key_verifier_preserved": True,
    }


def wheel_files(raw, source_rows):
    """A genuine pure-Python 1.0 wheel, matched to the release source bytes."""
    with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
        names = wheel.namelist()
        need(len(names) == len(set(names)), "wheel_unverified")
        source = {
            row["path"].removeprefix("src/"): row["sha256"]
            for row in source_rows
            if row["path"].startswith("src/quota_broker/")
        }
        dist = "api_quota_broker-1.0.0.dist-info/"
        need(
            set(names)
            == set(source)
            | {dist + n for n in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")},
            "wheel_unverified",
        )
        files = {name: wheel.read(name) for name in names}
        for info in wheel.infolist():
            need(
                not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16), "wheel_unverified"
            )
        for name, sha in source.items():
            need(hashlib.sha256(files[name]).hexdigest() == sha, "wheel_unverified")
        need(
            b"Version: 1.0.0\n" in files[dist + "METADATA"]
            and b"Tag: py3-none-any\n" in files[dist + "WHEEL"],
            "wheel_unverified",
        )
        records = list(csv.reader(io.StringIO(files[dist + "RECORD"].decode())))
        need(
            len(records) == len(files) and {r[0] for r in records} == set(files), "wheel_unverified"
        )
        for name, encoded, size in records:
            need(name in files, "wheel_unverified")
            if name != dist + "RECORD":
                actual = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(files[name]).digest()
                ).decode().rstrip("=")
                need(encoded == actual and int(size) == len(files[name]), "wheel_unverified")
        need(
            files[dist + "entry_points.txt"].decode().strip()
            == "[console_scripts]\nquota-broker = quota_broker.cli:main",
            "wheel_unverified",
        )
        return files


def audit(modules, *, root=None):
    base = modules["pool_base.py"]
    unit_verify = "component_fixture_not_executed"
    if root is not None:
        AUDIT_DIAGNOSTIC.update(stage="unit_verify", returncode=None)
        result = subprocess.run(
            [
                "/usr/bin/systemd-analyze",
                "verify",
                str(root / "api-quota-broker.service"),
                str(root / "api-quota-broker-client.service"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            timeout=20,
            check=False,
        )
        AUDIT_DIAGNOSTIC["returncode"] = result.returncode
        need(result.returncode == 0, "native_unit_verify_failed")
        unit_verify = "passed"
    AUDIT_DIAGNOSTIC.update(stage="history_projection", returncode=None)
    report, original = modules["ops_history_projection.py"].private_preflight()
    AUDIT_DIAGNOSTIC["stage"] = "readonly_audit"
    now = datetime.now(UTC)
    for target in base.strict_json(original)["targets"]:
        need(
            target["enabled"] is True
            and target["free_eligible"] is True
            and target["billing_enabled"] is False
            and datetime.fromisoformat(target["verified_at"])
            <= now
            < datetime.fromisoformat(target["expires_at"]),
            "account_evidence_expired",
        )
    need(
        report["config_sha256"] == OLD_CONFIG_SHA and report["config_targets"] == 7,
        "baseline_changed",
    )
    need(
        report["broker_pins"]["release"] == OLD_RELEASE
        and report["broker_pins"]["unit_sha256"] == OLD_UNIT_SHA
        and report["r14_pins"]["entry_sha256"] == OLD_ENTRY_SHA
        and report["r14_pins"]["config_pin_matches"]
        and report["r14_pins"]["entry_matches_installed_manifest"],
        "baseline_changed",
    )
    need(
        report["classification"]
        == {
            "known_groq_settled": 1,
            "not_dispatched": 0,
            "dispatched_known_result": 7,
            "dispatch_or_settlement_unknown": 0,
        },
        "baseline_changed",
    )
    need(report["blocked_providers"] == ["cloudflare"], "baseline_changed")
    need(
        not CLAIM.exists()
        and not CLAIM.is_symlink()
        and not JOURNAL.exists()
        and not JOURNAL.is_symlink(),
        "claim_collision",
    )
    for path in (CLIENT_UNIT_PATH, WRAPPER_PATH, Path("/run/api-quota-broker-client")):
        need(not path.exists() and not path.is_symlink(), "client_path_exists")
    # Ops construction is pure. Use its genuine fixed-path readonly connection.
    reader = base.Ops(Path("/"), {}, None, None)
    AUDIT_DIAGNOSTIC["stage"] = "queue_gate"
    with closing(reader.connect()) as con:
        con.execute("PRAGMA query_only=ON")
        queue = empty_queue_gate(con)
    return {
        "status": "passed",
        "mode": "normal_pool_private_readonly_gate",
        "history": report,
        "queue": queue,
        "credential_reads": 0,
        "provider_posts": 0,
        "db_write": 0,
        "scope_granted": False,
        "native_systemd_unit_verify": unit_verify,
    }


def transaction(ops):
    record = {
        "mode": "asus_normal_pool_v1",
        "phase": "preflight",
        "status": "running",
        "posts_intended": [],
        "results": [],
        "provider_posts_max": 3,
        "automatic_retry": False,
        "new_token_creation": 0,
        "provider_metadata_GET": 0,
    }
    try:
        ops.preflight()
        ops.claim(record)
        record["phase"] = "stage"
        ops.save(record)
        ops.stage()
        ops.install_client_and_worker()
        record["phase"] = "normal_configuration"
        ops.save(record)
        ops.switch(ops.configuration())
        ops.verify_client()
        for body in probes():
            ops.preservation()
            ops.allocate_post()
            record["posts_intended"].append(body["request_key"])
            record["phase"] = "probe_" + body["request_key"]
            ops.save(record)
            result = ops.probe(body)
            record["results"].append(result)
            ops.save(record)
        auto = record["results"][0]
        long_auto = (
            auto.get("queued_auto") is True
            and auto.get("execution_verified") is True
            and type(auto.get("reported_output_tokens")) is int
            and auto["reported_output_tokens"] > 64
        )
        record["phase"] = "restart_persistence"
        ops.save(record)
        ops.restart_and_verify(record)
        ops.verify_ops_pins()
        record.update(ops.preservation())
        record.update(
            status="passed" if long_auto else "partial",
            phase="complete",
            provider_posts_intended=len(record["posts_intended"]),
            Broker_restarts=ops.budget.restarts,
            default_output_tokens=1024,
            normal_pool_limits_verified=True,
            morris_client_without_sudo=True,
            queue_worker_enabled=True,
            live_auto_long_answer_verified=long_auto,
            live_acceptance="passed" if long_auto else "not_demonstrated_no_retry",
            completed_at=datetime.now(UTC).isoformat(),
        )
        ops.save(record)
    except BaseException as error:  # noqa: BLE001 - immutable safe categories only
        record.update(
            status="blocked",
            failure_phase=record["phase"],
            code=error.args[0] if type(error) is Blocked else "upgrade_receipt_unknown",
        )
        try:
            if ops.changed:
                # Cleanup helper must still resolve in the new current release.
                ops.remove_client_and_restore_units()
                if ops.activation_changed():
                    ops.switch(ops.original_config(), original=True)
                record["original_restored"] = True
                record.update(ops.preservation())
        except BaseException:  # noqa: BLE001 - preserve all rows/claims; never replay
            record["original_restored"] = False
            try:
                ops.stop_broker()
                record["Broker_stopped"] = True
            except BaseException:  # noqa: BLE001
                record["Broker_stop_unverified"] = True
        if ops.claimed:
            try:
                ops.save(record)
            except BaseException:  # noqa: BLE001
                record["receipt_unverified"] = True
    return record


def make_native(root, policy, modules):
    seven, base, planner = (
        modules[n] for n in ("seven_pool_operator.py", "pool_base.py", "seven_pool_plan.py")
    )
    seven.CLAIM, seven.JOURNAL, seven.UNIT = CLAIM, JOURNAL, UNIT
    base.OLD_RELEASE = OLD_RELEASE
    base.HISTORY = (
        *base.HISTORY,
        BACKUPS / "seven-pool-2026-10-08-r2.claim.json",
        BACKUPS / "seven-pool-2026-10-08-r2.json",
    )
    planner.task_key = lambda p: PREFIX + p + "-long-a1"
    parent = type(seven.make_native(root, policy, modules))
    base.TABLES = (*base.TABLES, "queue_settings")

    class Native(parent):
        def stage(self):
            super().stage()
            raw = base.read_regular(root / "project.whl", bound=1048576)
            need(base.digest(raw) == policy["project_wheel_sha256"], "wheel_unverified")
            archive = base.read_regular(root / "source.tar", bound=134217728)
            verified = self.verifier.verify_archive(archive, policy["payload_manifest_sha256"])
            files = wheel_files(raw, verified["release"]["files"])
            site = self.new_release / "runtime/lib/python3.14/site-packages"
            history = self.new_release / "historical-project-distribution"
            history.mkdir(mode=0o755)
            old = list(site.glob("api_quota_broker-*.dist-info"))
            need(len(old) == 1 and old[0].is_dir() and not old[0].is_symlink(), "wheel_unverified")
            shutil.move(str(old[0]), str(history / old[0].name))
            dist = site / "api_quota_broker-1.0.0.dist-info"
            dist.mkdir(mode=0o755)
            for name, value in files.items():
                path = site / name
                if name.startswith("quota_broker/"):
                    need(path.is_file() and not path.is_symlink(), "wheel_unverified")
                    need(path.read_bytes() == value, "wheel_unverified")
                else:
                    base.write_exclusive(path, value, mode=0o644)
            executable = self.new_release / "runtime/bin/quota-broker"
            code = (
                "#!"
                + str(self.new_release / "runtime/bin/python")
                + "\nfrom quota_broker.cli import main\nraise SystemExit(main())\n"
            ).encode()
            need(executable.is_file() and not executable.is_symlink(), "wheel_unverified")
            executable.write_bytes(code)
            executable.chmod(0o755)
            # Installed RECORD includes the generated console script, as a wheel
            # installer would. Keep the original archive separately for proof.
            rows = list(
                csv.reader(io.StringIO(files["api_quota_broker-1.0.0.dist-info/RECORD"].decode()))
            )
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
            (dist / "RECORD").write_text(stream.getvalue())
            (dist / "RECORD").chmod(0o644)
            check = "import importlib.metadata,quota_broker; assert quota_broker.__version__=='1.0.0'; assert importlib.metadata.version('api-quota-broker')=='1.0.0'; print('normal_runtime_version_passed')"
            need(
                self.command(
                    [str(self.new_release / "runtime/bin/python"), "-I", "-B", "-c", check]
                ).strip()
                == b"normal_runtime_version_passed",
                "wheel_unverified",
            )
            # Keep the basis receipt as history, and publish an accurate receipt
            # for the completed install. Never rewrite the old release.
            overlay = self.new_release / "pool-runtime-overlay.json"
            overlay.rename(self.new_release / "historical-source-overlay-receipt.json")
            base.write_exclusive(
                self.new_release / "normal-runtime-install.json",
                base.canonical(
                    {
                        "basis": "existing_native_dependencies_plus_verified_1_0_project_wheel",
                        "original_release": OLD_RELEASE,
                        "payload_manifest_sha256": policy["payload_manifest_sha256"],
                        "project_wheel_sha256": policy["project_wheel_sha256"],
                        "package_version": "1.0.0",
                        "source_files_match_wheel": True,
                        "old_project_dist_info_retained_outside_site_packages": True,
                        "installed_record_includes_generated_console_script": True,
                    }
                ),
                mode=0o644,
            )

        def preflight(self):
            need(
                audit(modules, root=root) == json.loads(seven.read(root / "audit.json")),
                "baseline_changed",
            )
            self.original = base.read_regular(seven.CONFIG, gid=982, mode=0o640)
            self.expected_config_sha = base.digest(self.original)
            self.owned_config_hashes = {self.expected_config_sha}
            self.owned_current_targets = {OLD_RELEASE}
            self.expected_current = OLD_RELEASE
            self.history_files, self.history = self.files(), self.history_rows(initial=True)
            self.prior_orderflow, self.prior_ssh = self.orderflow(), self.service("ssh.service")
            self.old_runtime_snapshot = self.runtime_snapshot(base.BASE / OLD_RELEASE / "runtime")
            self.check_service()
            meta = base.strict_json(
                base.read_regular(seven.CONFIG.parent / "credentials/doppler-metadata.json")
            )
            need(
                all(
                    meta.get(k) == v
                    for k, v in {
                        "project": "api-quota-broker",
                        "config": "dev",
                        "access": "read",
                        "credential_policy": "host",
                        "human_dashboard_attested": True,
                    }.items()
                ),
                "credential_metadata",
            )
            end = datetime.fromisoformat(meta["expires_at"])
            need(
                end.tzinfo is not None and end > datetime.now(UTC) + timedelta(seconds=1200),
                "credential_metadata",
            )
            for path in (
                seven.OPS_BASE / "ops_entry.py",
                seven.OPS_BASE / "manifest.json",
                seven.OPS_POLICY,
            ):
                raw = base.read_regular(path, mode=0o644)
                self.ops_originals[path] = raw
                self.ops_expected[path] = base.digest(raw)
                self.ops_owned_hashes[path] = {base.digest(raw)}
            self.broker_unit_original = base.read_regular(BROKER_UNIT_PATH, mode=0o644)
            need(base.digest(self.broker_unit_original) == OLD_UNIT_SHA, "baseline_changed")
            self.unit_expected_sha = OLD_UNIT_SHA
            self.installed_client = False
            self.installed_paths = {}
            self.client_loaded = False
            # Actual installed ops gates run before credential value access.
            spec = importlib.util.spec_from_file_location(
                "normal_current_ops", seven.OPS_BASE / "ops_entry.py"
            )
            installed = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installed)
            installed.package()
            installed.runtime_policy()
            installed.broker_pins(self.expected_config_sha)
            need(
                os.environ.get("CREDENTIALS_DIRECTORY") == "/run/credentials/" + UNIT,
                "credential_metadata",
            )
            self.client = (
                base.read_regular(
                    Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token",
                    mode=0o400,
                    bound=257,
                )
                .decode()
                .strip()
            )
            need(
                re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", self.client) is not None,
                "credential_metadata",
            )

        def original_config(self):
            return base.strict_json(self.original)

        def configuration(self):
            mod = seven.loader(root, "normal_pool_plan.py", policy["normal_plan_sha256"])
            return mod.config(self.original_config())

        def switch(self, config, *, original=False):
            raw = self.original if original else base.canonical(config)
            self.owned_config_hashes.add(base.digest(raw))
            if not original:
                self.owned_current_targets.add(str(self.new_release.relative_to(base.BASE)))
            return super().switch(config, original=original)

        def activation_changed(self):
            sha = base.digest(base.read_regular(seven.CONFIG, gid=982, mode=0o640))
            current = str(base.CURRENT.readlink())
            need(
                sha in self.owned_config_hashes and current in self.owned_current_targets,
                "selector_or_config_changed",
            )
            self.expected_config_sha, self.expected_current = sha, current
            return sha != OLD_CONFIG_SHA or current != OLD_RELEASE

        def allocate_post(self):
            need(self.budget.posts < 3, "post_budget")
            self.budget.require(210)
            self.budget.posts += 1

        def claim(self, record):
            super().claim(record)
            base.write_exclusive(self.backup / "broker-unit.original", self.broker_unit_original)

        def command(self, argv):
            if "restart" in argv and "api-quota-broker.service" in argv:
                need(self.budget.restarts < 3, "restart_budget")
                self.budget.restart(rollback=getattr(self, "restoring", False))
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=True,
                env=base.CLEAN_ENV
                if hasattr(base, "CLEAN_ENV")
                else {"PATH": "/usr/bin:/bin", "LANG": "C"},
            ).stdout

        def install_client_and_worker(self):
            self.changed = True  # Includes files changed before selector activation.
            self.installed_client = True
            for destination, source, mode in (
                (CLIENT_UNIT_PATH, "api-quota-broker-client.service", 0o644),
                (WRAPPER_PATH, "aqb", 0o755),
            ):
                raw = (self.new_release / "deploy/asus" / source).read_bytes()
                fd = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
                )
                info = os.fstat(fd)
                self.installed_paths[destination] = (info.st_dev, info.st_ino, mode)
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            new_unit = (self.new_release / "deploy/asus/api-quota-broker-v1.service").read_bytes()
            need(
                b"--no-queue-worker" not in new_unit
                and b"Wants=api-quota-broker-client.service" in new_unit,
                "worker_unit_unverified",
            )
            # Change the original ordinary file only after verifying its exact bytes.
            self.replace_unit(new_unit)
            self.command(["/usr/bin/systemctl", "daemon-reload"])
            self.client_loaded = True
            self.command(["/usr/bin/systemctl", "enable", CLIENT_UNIT])

        def replace_unit(self, raw):
            need(
                base.digest(base.read_regular(BROKER_UNIT_PATH, mode=0o644))
                == self.unit_expected_sha,
                "unit_changed",
            )
            temp = BROKER_UNIT_PATH.parent / (".normal-unit-" + os.urandom(16).hex())
            base.write_exclusive(temp, raw, mode=0o644)
            os.replace(temp, BROKER_UNIT_PATH)
            self.unit_expected_sha = base.digest(raw)
            base.sync_directory(BROKER_UNIT_PATH.parent)

        def sync_ops(self, config, original):
            originals = self.ops_originals
            if original:
                replacements = originals
            else:
                entry = originals[seven.OPS_BASE / "ops_entry.py"]
                for old, new in (
                    (
                        'RELEASE = "' + OLD_RELEASE + '"',
                        'RELEASE = "releases/release-' + policy["payload_manifest_sha256"] + '"',
                    ),
                    (
                        'UNIT_SHA = "' + OLD_UNIT_SHA + '"',
                        'UNIT_SHA = "' + self.unit_expected_sha + '"',
                    ),
                ):
                    need(entry.count(old.encode()) == 1, "ops_pin_sync")
                    entry = entry.replace(old.encode(), new.encode())
                manifest = base.strict_json(originals[seven.OPS_BASE / "manifest.json"])
                manifest["files"]["ops_entry.py"] = base.digest(entry)
                runtime_policy = base.strict_json(originals[seven.OPS_POLICY])
                runtime_policy["config_sha256"] = base.digest(base.canonical(config))
                replacements = {
                    seven.OPS_BASE / "ops_entry.py": entry,
                    seven.OPS_POLICY: base.canonical(runtime_policy),
                    seven.OPS_BASE / "manifest.json": base.canonical(manifest),
                }
            for path in (
                seven.OPS_BASE / "ops_entry.py",
                seven.OPS_POLICY,
                seven.OPS_BASE / "manifest.json",
            ):
                need(
                    base.digest(base.read_regular(path, mode=0o644)) in self.ops_owned_hashes[path],
                    "ops_pin_changed",
                )
                raw = replacements[path]
                self.ops_owned_hashes[path].add(base.digest(raw))
                temp = path.parent / (".normal-ops-" + os.urandom(16).hex())
                base.write_exclusive(temp, raw, mode=0o644)
                os.replace(temp, path)
                self.ops_expected[path] = base.digest(raw)
                base.sync_directory(path.parent)

        def morris(self, action, *, body=None, key=None, timeout=190):
            need(
                pwd.getpwnam("morris").pw_uid == pwd.getpwnam("morris").pw_gid == 1000,
                "client_identity",
            )
            argv = [
                "/usr/sbin/runuser",
                "-u",
                "morris",
                "--",
                str(WRAPPER_PATH),
                "--json",
                "--http-timeout",
                "185",
                action,
            ]
            if body is not None:
                argv += [
                    "--request-key",
                    body["request_key"],
                    "--capability",
                    "text_generation",
                    "--task-stdin",
                ]
            elif key is not None:
                argv.append(key)
            result = subprocess.run(
                argv,
                input=base.canonical(body) if body is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            need(
                result.returncode == 0 and len(result.stdout) <= 262144, "client_request_unverified"
            )
            return base.strict_json(result.stdout)

        def verify_client(self):
            self.command(["/usr/bin/systemctl", "start", CLIENT_UNIT])
            catalog = self.morris("catalog", timeout=15)
            need(
                len(catalog) == 7
                and all(
                    t["request_limits"]["max_output_tokens"] >= 1024
                    for t in catalog
                    if t["provider"] != "ocrspace"
                ),
                "normal_limits_unverified",
            )
            diagnostics = self.morris("diagnostics", timeout=15)
            need(diagnostics["queue_worker"]["enabled"] is True, "worker_unit_unverified")
            self.morris("usage", timeout=15)

        def probe(self, body):
            queued = body["request_key"] == QUEUE_KEY
            outcome = self.morris("submit" if queued else "run", body=body)
            if queued:
                deadline = time.monotonic() + 180
                while outcome["state"] not in {
                    "completed",
                    "failed",
                    "unknown",
                    "cancelled",
                    "expired",
                }:
                    need(time.monotonic() < deadline, "queue_result_unknown")
                    time.sleep(1)
                    outcome = self.morris("queue-status", key=QUEUE_KEY, timeout=15)
                need(outcome["state"] == "completed", "queue_result_unknown")
                outcome = self.morris("result", key=QUEUE_KEY, timeout=15)
            execution_key = outcome["request_key"]
            need(
                (queued and re.fullmatch(r"q-[a-f0-9]{32}", execution_key))
                or execution_key == body["request_key"],
                "result_identity",
            )
            answer = outcome.get("answer")
            stop = outcome.get("response_truncated") is not True
            if outcome["provider"] in {"nvidia", "groq", "mistral", "openrouter"}:
                stop &= outcome.get("finish_reason") == "stop"
            if outcome["provider"] == "google":
                stop &= outcome.get("diagnostics", {}).get("provider_finish_reason") == "STOP"
            length = len(answer) if isinstance(answer, str) else 0
            safe = base.project_result(outcome["provider"], outcome["model"], outcome)
            safe["reported_output_tokens"] = (
                outcome.get("reported_output_tokens")
                if type(outcome.get("reported_output_tokens")) is int
                and 0 <= outcome["reported_output_tokens"] <= 2048
                else None
            )
            safe.update(
                request_key=body["request_key"],
                execution_key=execution_key,
                answer_characters=length,
                long_answer_verified=bool(
                    stop and length >= 1200 and outcome.get("http_status") == 200
                ),
                queued_auto=queued,
                output_finish_known=outcome.get("response_truncated") is not None,
                execution_verified=bool(
                    outcome.get("http_status") == 200
                    and length > 0
                    and outcome.get("state") in {"completed", "completed_usage_unknown"}
                    and outcome.get("response_truncated") is not True
                ),
            )
            if queued:
                with self.connect() as con:
                    keys = con.execute(
                        "SELECT execution_key FROM queue_attempts WHERE request_key=?", (QUEUE_KEY,)
                    ).fetchall()
                    need(keys == [(execution_key,)], "result_identity")
            old_key = base.task_key
            base.task_key = lambda _: execution_key
            try:
                safe["ledger_projection"] = self.ledger_probe(outcome["provider"], safe)
            finally:
                base.task_key = old_key
            return safe

        def restart_and_verify(self, record):
            # Queue already terminal; restart must not trigger another provider execution.
            with self.connect() as con:
                before = {
                    table: con.execute("SELECT count(*) FROM " + table).fetchone()[0]
                    for table in (
                        "gateway_tasks",
                        "gateway_attempts",
                        "reservations",
                        "execution_completion",
                        "queue_jobs",
                        "queue_attempts",
                    )
                }
            self.command(["/usr/bin/systemctl", "restart", "api-quota-broker.service"])
            self.check_service()
            self.verify_client()
            with self.connect() as con:
                need(
                    before
                    == {
                        table: con.execute("SELECT count(*) FROM " + table).fetchone()[0]
                        for table in before
                    },
                    "ledger_changed",
                )
                parents = {row[0] for row in con.execute("SELECT request_key FROM queue_jobs")}
                need(parents == {QUEUE_KEY}, "queue_foreign_work")
                for result in record["results"]:
                    old_key = base.task_key
                    base.task_key = lambda _, result=result: result["execution_key"]
                    try:
                        need(
                            self.ledger_probe(result["provider"], result)
                            == result["ledger_projection"],
                            "ledger_changed",
                        )
                    finally:
                        base.task_key = old_key

        def remove_client_and_restore_units(self):
            self.restoring = True
            if self.installed_client:
                if self.client_loaded:
                    self.command(["/usr/bin/systemctl", "stop", CLIENT_UNIT])
                    self.command(["/usr/bin/systemctl", "disable", CLIENT_UNIT])
                need(
                    not Path("/run/api-quota-broker-client/client_token").exists(),
                    "client_cleanup_unverified",
                )
                directory = Path("/run/api-quota-broker-client")
                if directory.exists():
                    info = directory.lstat()
                    need(
                        not directory.is_symlink()
                        and info.st_uid == info.st_gid == 0
                        and info.st_mode & 0o777 == 0o711,
                        "client_cleanup_unverified",
                    )
                    directory.rmdir()  # Empty only; foreign contents are never removed.
                for path, (device, inode, mode) in self.installed_paths.items():
                    info = path.lstat()
                    need(
                        info.st_dev == device
                        and info.st_ino == inode
                        and info.st_uid == info.st_gid == 0
                        and info.st_nlink == 1
                        and info.st_mode & 0o777 == mode,
                        "client_path_changed",
                    )
                    path.unlink()
            self.replace_unit(self.broker_unit_original)
            self.command(["/usr/bin/systemctl", "daemon-reload"])

        def stop_broker(self):
            self.command(["/usr/bin/systemctl", "stop", "api-quota-broker.service"])

    return Native()


def main():
    if not sys.argv[1:]:
        print('{"mode":"normal_pool_v1_review_plan","provider_posts_max":3,"host_changes":0}')
        return 0
    stage = "argument_gate"
    AUDIT_DIAGNOSTIC.update(stage="readonly_audit", returncode=None)
    try:
        need(sys.argv[1:] in (["--audit"], ["--apply"]), "root_scope")
        stage = "package_metadata"
        root = Path(__file__).absolute().parent
        info = root.lstat()
        need(
            root.parent == Path("/var/tmp")
            and re.fullmatch(r"aqb-normal-pool-[a-f0-9]{32}", root.name)
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == info.st_gid == 0
            and info.st_mode & 0o777 == 0o700,
            "package_untrusted",
        )

        def read(name):
            fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                need(
                    stat.S_ISREG(meta.st_mode)
                    and meta.st_uid == meta.st_gid == 0
                    and meta.st_nlink == 1
                    and meta.st_mode & 0o777 == 0o600
                    and meta.st_size <= 134217728,
                    "package_untrusted",
                )
                return stream.read(134217729)

        stage = "seal_verification"
        seal = json.loads(read("seal.json"))
        names = {
            "source.tar",
            "policy.json",
            "pool_base.py",
            "pool_base_plan.py",
            "seven_pool_operator.py",
            "seven_pool_plan.py",
            "build_asus_release.py",
            "ops_history_projection.py",
            "normal_pool_operator.py",
            "normal_pool_plan.py",
            "project.whl",
            "api-quota-broker.service",
            "api-quota-broker-client.service",
        }
        need(seal["schema"] == 1 and set(seal["files"]) == names, "package_untrusted")
        for name, sha in seal["files"].items():
            need(hashlib.sha256(read(name)).hexdigest() == sha, "package_untrusted")

        def load(name):
            spec = importlib.util.spec_from_file_location(
                "normal_sealed_" + name.replace(".", "_"), root / name
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module

        stage = "module_import"
        seven = load("seven_pool_operator.py")
        stage = "sandbox_gate"
        seven.scope(
            AUDIT_UNIT if sys.argv[1] == "--audit" else UNIT, readonly=sys.argv[1] == "--audit"
        )
        if sys.argv[1] == "--audit":
            private_audit_tmp_gate()
        stage = "module_import"
        modules = {
            name: load(name)
            for name in (
                "pool_base.py",
                "pool_base_plan.py",
                "seven_pool_plan.py",
                "build_asus_release.py",
                "ops_history_projection.py",
            )
        }
        modules["seven_pool_operator.py"] = seven
        policy = json.loads(read("policy.json"))
        stage = "readonly_audit" if sys.argv[1] == "--audit" else "apply_transaction"
        result = (
            audit(modules, root=root)
            if sys.argv[1] == "--audit"
            else transaction(make_native(root, policy, modules))
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] in {"passed", "partial"} else 1
    except BaseException as error:  # noqa: BLE001 - no raw diagnostic or secret output
        context = (
            AUDIT_DIAGNOSTIC if stage == "readonly_audit" else {"stage": stage, "returncode": None}
        )
        print(json.dumps(failure_projection(error, **context), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
