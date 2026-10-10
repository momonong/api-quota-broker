"""New once-only ASUS deployment/probe transaction. Default is a zero-effect plan.

The first audit unit has no credentials. Only its passed safe receipt enables
the second fixed credential unit. No old entry, Groq POST, retry, or paid route.
"""

import ctypes
import hashlib
import http.client
import importlib.util
import json
import os
import re
import resource
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/opt/api-quota-broker")
CONFIG = Path("/etc/api-quota-broker/gateway.json")
BACKUPS = Path("/var/backups/api-quota-broker")
CLAIM = BACKUPS / "seven-pool-2026-10-08-r2.claim.json"
JOURNAL = BACKUPS / "seven-pool-2026-10-08-r2.json"
OPS_BASE = Path("/usr/local/lib/api-quota-broker-ops")
OPS_POLICY = Path("/etc/api-quota-broker-ops/policy.json")
UNIT = "api-quota-broker-seven-pool.service"
AUDIT_UNIT = "api-quota-broker-seven-audit.service"
TOTAL_SECONDS = 1200
CLEANUP_SECONDS = 240
SAFE_CODES = {
    "root_scope",
    "package_untrusted",
    "audit_gate",
    "claim_collision",
    "ledger_changed",
    "old_budget_used",
    "account_evidence_missing",
    "credential_metadata",
    "post_budget",
    "restart_budget",
    "deadline_budget",
    "ops_pin_changed",
    "ops_pin_sync",
    "service_changed",
    "response_unclassified",
    "receipt_unknown",
}


class Blocked(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Blocked(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, *, uid=0, gid=0, mode=0o600, bound=131072):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        a = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(a.st_mode)
            and a.st_uid == uid
            and a.st_gid == gid
            and a.st_nlink == 1
            and stat.S_IMODE(a.st_mode) == mode
            and a.st_size <= bound,
            "package_untrusted",
        )
        raw = stream.read(bound + 1)
        b = os.fstat(stream.fileno())
        need(
            len(raw) == a.st_size
            and (a.st_ino, a.st_mtime_ns, a.st_ctime_ns)
            == (b.st_ino, b.st_mtime_ns, b.st_ctime_ns),
            "package_untrusted",
        )
        return raw


def loader(root, name, expected):
    need(digest(read(root / name)) == expected, "package_untrusted")
    spec = importlib.util.spec_from_file_location("sealed_" + name.replace(".", "_"), root / name)
    need(spec is not None and spec.loader is not None, "package_untrusted")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package():
    root = Path(__file__).absolute().parent
    meta = root.lstat()
    need(
        root.parent == Path("/var/tmp")
        and re.fullmatch(r"aqb-seven-pool-[a-f0-9]{32}", root.name)
        and stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == 0
        and stat.S_IMODE(meta.st_mode) == 0o700,
        "package_untrusted",
    )
    seal = json.loads(read(root / "seal.json"))
    need(set(seal) == {"schema", "files"} and seal["schema"] == 1, "package_untrusted")
    names = {
        "seven_pool_operator.py",
        "seven_pool_plan.py",
        "pool_base.py",
        "pool_base_plan.py",
        "ops_history_projection.py",
        "build_asus_release.py",
        "source.tar",
        "policy.json",
    }
    need(set(seal["files"]) == names, "package_untrusted")
    for name, sha in seal["files"].items():
        need(
            digest(read(root / name, bound=134217728 if name == "source.tar" else 131072)) == sha,
            "package_untrusted",
        )
    policy = json.loads(read(root / "policy.json"))
    modules = {
        name: loader(root, name, seal["files"][name])
        for name in (
            "seven_pool_plan.py",
            "pool_base.py",
            "pool_base_plan.py",
            "ops_history_projection.py",
            "build_asus_release.py",
        )
    }
    return root, policy, modules


def scope(unit, *, readonly=False):
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server", "root_scope")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    need(
        resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
        and libc.prctl(4, 0, 0, 0, 0) == 0
        and libc.prctl(3, 0, 0, 0, 0) == 0
        and libc.prctl(39, 0, 0, 0, 0) == 1,
        "root_scope",
    )
    need(Path("/proc/self/cgroup").read_text().strip() == "0::/system.slice/" + unit, "root_scope")
    group = Path("/sys/fs/cgroup/system.slice") / unit
    need(
        (group / "memory.max").read_text().strip() == "402653184"
        and (group / "memory.swap.max").read_text().strip() == "0",
        "root_scope",
    )
    if readonly:
        need(
            all(
                os.statvfs(p).f_flag & os.ST_RDONLY
                for p in (CONFIG, BACKUPS, Path("/var/lib/api-quota-broker"))
            ),
            "root_scope",
        )


def audit(modules):
    """Never reads a credential or constructs Gateway; preserved ledger first."""
    report, _ = modules["ops_history_projection.py"].private_preflight()
    need(
        report["config_targets"] == 0
        and report["config_sha256"] == modules["pool_base.py"].EMPTY_SHA,
        "audit_gate",
    )
    need(
        report["classification"]
        == {
            "known_groq_settled": 1,
            "not_dispatched": 0,
            "dispatched_known_result": 0,
            "dispatch_or_settlement_unknown": 0,
        }
        and not report["blocked_providers"]
        and not report["old_files"]["claim"]["present"]
        and not report["old_files"]["journal"]["present"],
        "old_budget_used",
    )
    need(
        report["old_bootstrap"]["historical_bundle_provenance_verified"]
        and not CLAIM.exists()
        and not CLAIM.is_symlink()
        and not JOURNAL.exists()
        and not JOURNAL.is_symlink(),
        "claim_collision",
    )
    return {
        "status": "passed",
        "mode": "seven_pool_private_readonly_gate",
        "history": report,
        "credential_reads": 0,
        "provider_posts": 0,
        "db_write": 0,
        "scope_granted": False,
    }


class Budget:
    def __init__(self, clock=time.monotonic):
        self.clock, self.end = clock, clock() + TOTAL_SECONDS
        self.posts, self.restarts = 0, 0

    def require(self, seconds):
        need(self.end - self.clock() >= seconds + CLEANUP_SECONDS, "deadline_budget")

    def post(self):
        need(self.posts < 7, "post_budget")
        self.require(180)
        self.posts += 1

    def restart(self, *, rollback=False):
        need(self.restarts < 4, "restart_budget")
        need(
            self.end - self.clock() >= (60 if rollback else CLEANUP_SECONDS + 60), "deadline_budget"
        )
        self.restarts += 1


def transaction(ops):
    record = {
        "mode": "asus_seven_pool_r2",
        "phase": "preflight",
        "status": "running",
        "posts_intended": [],
        "results": [],
        "provider_posts_max": 7,
        "Groq_new_posts": 0,
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
        eligible = ops.eligible()
        accepted = {
            "groq"
        } & eligible  # Original settled execution only; never another Groq request.
        record["phase"] = "probe_configuration"
        ops.save(record)
        ops.switch(ops.configuration(eligible - {"groq"}))
        for provider, body in ops.tasks().items():
            if provider not in eligible:
                record["results"].append(
                    {
                        "provider": provider,
                        "state": "not_dispatched",
                        "blocker": "account_or_model_admission_missing",
                        "full_answer_verified": False,
                    }
                )
                ops.save(record)
                continue
            record["phase"] = "probe_" + provider
            ops.preservation()
            ops.budget.post()
            record["posts_intended"].append(provider)
            ops.save(record)
            try:
                status, value = ops.http("POST", "/v1/tasks", body)
            except (OSError, http.client.HTTPException, ValueError):
                status, value = None, None
            result = ops.observe(provider, value if status == 200 else None)
            record["results"].append(result)
            if result["full_answer_verified"]:
                accepted.add(provider)
            ops.save(record)
        auto_candidates = accepted - {"groq", "ocrspace"}
        record["phase"] = "automatic_route_configuration"
        ops.save(record)
        ops.switch(ops.configuration(auto_candidates | (accepted & {"ocrspace"}), normal=True))
        if auto_candidates:
            result = ops.auto_probe(record, auto_candidates)
            record["automatic_route"] = result
            if result.get("provider") and not result["full_answer_verified"]:
                accepted.discard(result["provider"])
            elif result.get("provider") is None and result.get("state") == "unknown":
                accepted -= auto_candidates
        else:
            record["automatic_route"] = {
                "state": "not_dispatched",
                "blocker": "no_qualified_text_candidates",
            }
        record["phase"] = "persistent_configuration"
        ops.save(record)
        ops.switch(ops.configuration(accepted, normal=True))
        ops.verify_all(record)
        ops.verify_ops_pins()
        record.update(ops.preservation())
        record.update(
            status="passed",
            phase="complete",
            enabled_providers=sorted(accepted),
            provider_posts=ops.budget.posts,
            Broker_restarts=ops.budget.restarts,
            broker_credential_GET_bound=9,
            native_ops_auth_GET_bound=2,
            completed_at=datetime.now(UTC).isoformat(),
        )
        ops.save(record)
    except BaseException as error:  # noqa: BLE001 - fixed errors; never response/secret prose.
        code = (
            error.args[0] if type(error) is Blocked and len(error.args) == 1 else "receipt_unknown"
        )
        record.update(
            status="blocked",
            failure_phase=record["phase"],
            code=code if code in SAFE_CODES else "receipt_unknown",
        )
        if ops.changed:
            try:
                ops.switch({"targets": []}, original=True)
                record["original_restored"] = True
                record.update(ops.preservation())
            except BaseException:  # noqa: BLE001 - preserve unknown; stop only Broker, never other services.
                record["original_restored"] = False
                try:
                    ops.command(("/usr/bin/systemctl", "stop", "api-quota-broker.service"))
                    record["Broker_stopped"] = True
                except BaseException:  # noqa: BLE001
                    record["Broker_stop_unverified"] = True
        if ops.claimed:
            try:
                ops.save(record)
            except BaseException:  # noqa: BLE001 - preserve uncertain receipt.
                record["receipt_unverified"] = True
    return record


def make_native(root, policy, modules):
    base, planner = modules["pool_base.py"], modules["seven_pool_plan.py"]
    base.CLAIM, base.JOURNAL, base.UNIT = CLAIM, JOURNAL, UNIT
    base.TABLES = (*base.TABLES, "execution_completion")
    base.task_key = planner.task_key

    class Native(base.Ops):
        def __init__(self):
            super().__init__(root, policy, planner, modules["build_asus_release.py"])
            self.budget = Budget()
            self.ops_originals, self.ops_expected, self.ops_owned_hashes = {}, {}, {}
            self.runtime_metadata_reads = 0

        def preflight(self):
            gate = json.loads(read(root / "audit.json"))
            need(
                gate["status"] == "passed" and gate["mode"] == "seven_pool_private_readonly_gate",
                "audit_gate",
            )
            actual = audit(modules)
            need(actual == gate, "ledger_changed")
            self.original = base.read_regular(CONFIG, gid=982, mode=0o640)
            self.history_files = self.files()
            self.history = self.history_rows(initial=True)
            self.prior_orderflow = self.orderflow()
            self.prior_ssh = self.service("ssh.service")
            self.old_runtime_snapshot = self.runtime_snapshot(BASE / base.OLD_RELEASE / "runtime")
            self.check_service()
            meta = base.strict_json(
                base.read_regular(CONFIG.parent / "credentials/doppler-metadata.json")
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
            expiry = datetime.fromisoformat(meta["expires_at"])
            need(
                expiry.tzinfo is not None
                and expiry > datetime.now(UTC) + timedelta(seconds=TOTAL_SECONDS),
                "credential_metadata",
            )
            self.expiry = expiry.isoformat()
            for path in (OPS_BASE / "ops_entry.py", OPS_BASE / "manifest.json", OPS_POLICY):
                raw = base.read_regular(path, mode=0o644)
                self.ops_originals[path] = raw
                self.ops_expected[path] = digest(raw)
                self.ops_owned_hashes[path] = {digest(raw)}
            need(
                digest(self.ops_originals[OPS_BASE / "ops_entry.py"]) == policy["r15_entry_sha256"],
                "ops_pin_changed",
            )
            d = base.strict_json(self.ops_originals[OPS_POLICY])
            need(
                d["enabled"] is False and d["expires_at"] == "2026-11-05T04:33:31+00:00",
                "ops_pin_changed",
            )
            spec = importlib.util.spec_from_file_location(
                "pinned_current_ops", OPS_BASE / "ops_entry.py"
            )
            need(spec is not None and spec.loader is not None, "ops_pin_changed")
            current_ops = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(current_ops)
            current_ops.package()
            current_ops.runtime_policy()
            current_ops.broker_pins(d["config_sha256"])
            need(
                os.environ.get("CREDENTIALS_DIRECTORY") == "/run/credentials/" + UNIT,
                "credential_metadata",
            )
            raw = base.read_regular(
                Path(os.environ["CREDENTIALS_DIRECTORY"]) / "client_token", mode=0o400, bound=257
            )
            self.client = raw.decode().strip()
            self.runtime_metadata_reads += 1
            need(bool(re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", self.client)), "credential_metadata")
            need(self.http("GET", "/v1/diagnostics")[1]["targets"] == [], "audit_gate")

        def tasks(self):
            return planner.tasks(policy["synthetic_ocr_png"])

        def eligible(self):
            return {
                p
                for p in planner.PROVIDERS
                if planner.evidence_valid(p, policy["admission"].get(p, {}), datetime.now(UTC))
            }

        def configuration(self, admitted, *, normal=False):
            return planner.config(
                modules["pool_base_plan.py"],
                admitted,
                policy["admission"],
                self.expiry,
                normal=normal,
            )

        def command(self, argv):
            if "restart" in argv:
                self.budget.restart(rollback=getattr(self, "restoring", False))
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60 if "restart" in argv else 20,
                check=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            ).stdout

        def http(self, method, path, body=None):
            allowed = (
                method == "GET"
                and body is None
                and path
                in {
                    "/v1/diagnostics",
                    "/v1/usage",
                    *["/v1/tasks/" + planner.task_key(p) for p in planner.PROVIDERS if p != "groq"],
                    "/v1/tasks/" + planner.AUTO_KEY,
                }
            )
            allowed |= (
                method == "POST"
                and path == "/v1/tasks"
                and (body in self.tasks().values() or body == planner.auto_task())
            )
            need(allowed, "post_budget")
            timeout = (
                (150 if body == planner.auto_task() or body.get("provider") == "nvidia" else 60)
                if method == "POST" and path == "/v1/tasks"
                else 10
            )
            deadline = time.monotonic() + timeout
            connection = http.client.HTTPConnection("127.0.0.1", 18084, timeout=timeout)
            try:
                connection.request(
                    method,
                    path,
                    body=base.canonical(body) if body else None,
                    headers={
                        "Authorization": "Bearer " + self.client,
                        "Content-Type": "application/json",
                    },
                )
                wire = connection.sock
                response = connection.getresponse()
                raw = bytearray()
                while not response.isclosed():
                    remaining = deadline - time.monotonic()
                    need(remaining > 0, "response_unclassified")
                    if wire is not None:
                        wire.settimeout(remaining)
                    part = response.read1(min(16384, 262145 - len(raw)))
                    if not part:
                        break
                    raw.extend(part)
                    need(len(raw) <= 262144, "response_unclassified")
                need(response.length in (None, 0), "response_unclassified")
                return response.status, base.strict_json(raw)
            finally:
                connection.close()

        def claim(self, record):
            super().claim(record)
            for path, raw in self.ops_originals.items():
                base.write_exclusive(self.backup / ("ops-original-" + path.name), raw)

        def sync_ops(self, config, original):
            for path, expected in self.ops_expected.items():
                current = digest(base.read_regular(path, mode=0o644))
                need(
                    current in self.ops_owned_hashes[path] if original else current == expected,
                    "ops_pin_changed",
                )
            if original:
                replacements = self.ops_originals
            else:
                release = "releases/release-" + policy["payload_manifest_sha256"]
                old_line = 'RELEASE = "' + base.OLD_RELEASE + '"'
                raw = self.ops_originals[OPS_BASE / "ops_entry.py"]
                need(raw.count(old_line.encode()) == 1, "ops_pin_sync")
                entry = raw.replace(old_line.encode(), ('RELEASE = "' + release + '"').encode())
                manifest = base.strict_json(self.ops_originals[OPS_BASE / "manifest.json"])
                manifest["files"]["ops_entry.py"] = digest(entry)
                d = base.strict_json(self.ops_originals[OPS_POLICY])
                d["config_sha256"] = digest(base.canonical(config))
                replacements = {
                    OPS_BASE / "ops_entry.py": entry,
                    OPS_POLICY: base.canonical(d),
                    OPS_BASE / "manifest.json": base.canonical(manifest),
                }
            # Manifest last: no partial files can pass the installed package gate.
            for path in (OPS_BASE / "ops_entry.py", OPS_POLICY, OPS_BASE / "manifest.json"):
                raw = replacements[path]
                self.ops_owned_hashes[path].add(digest(raw))
                temporary = path.parent / (".seven-ops-" + os.urandom(16).hex())
                base.write_exclusive(temporary, raw, mode=0o644)
                base.require(digest(base.read_regular(temporary, mode=0o644)) == digest(raw))
                os.replace(temporary, path)
                self.ops_expected[path] = digest(raw)
                base.sync_directory(path.parent)

        def switch(self, config, *, original=False):
            self.restoring = original
            super().switch(config, original=original)
            self.sync_ops(config, original)

        def preservation(self):
            result = super().preservation()
            need(self.service("ssh.service") == self.prior_ssh, "service_changed")
            return {**result, "SSH_unchanged": True}

        @contextmanager
        def auto_key(self):
            old = base.task_key
            base.task_key = lambda _: planner.AUTO_KEY
            try:
                yield
            finally:
                base.task_key = old

        def auto_probe(self, record, candidates):
            self.budget.post()
            record["posts_intended"].append("auto_route")
            self.save(record)
            try:
                status, value = self.http("POST", "/v1/tasks", planner.auto_task())
            except (OSError, http.client.HTTPException, ValueError):
                status, value = None, None
            if value is None:
                status, value = self.http("GET", "/v1/tasks/" + planner.AUTO_KEY)
            selected = value.get("provider") if isinstance(value, dict) else None
            if selected not in candidates:
                known_not_sent = (
                    isinstance(value, dict)
                    and value.get("state")
                    in {"rejected", "quota_exhausted", "quota_rejected", "pre_send_failed"}
                    and not value.get("attempts")
                )
                return {
                    "state": "not_dispatched" if known_not_sent else "unknown",
                    "provider": None,
                    "full_answer_verified": False,
                    "blocker": "no_eligible_text_route"
                    if known_not_sent
                    else "auto_delivery_or_selection_unknown",
                    "request_key": planner.AUTO_KEY,
                }
            with self.auto_key():
                result = self.observe(selected, value if status == 200 else None)
            result["route_verified_from_result_and_ledger"] = True
            return result

        def verify_all(self, record):
            results = [r for r in record["results"] if r.get("ledger_projection")]
            for result in results:
                need(
                    self.ledger_probe(result["provider"], result) == result["ledger_projection"],
                    "ledger_changed",
                )
                status, saved = self.http("GET", "/v1/tasks/" + result["request_key"])
                need(status == 200, "ledger_changed")
                safe = base.project_result(result["provider"], result["requested_model"], saved)
                need(
                    all(
                        safe[k] == v
                        for k, v in result.items()
                        if k in safe and k != "full_answer_verified"
                    ),
                    "ledger_changed",
                )
            auto = record["automatic_route"]
            if auto.get("ledger_projection"):
                with self.auto_key():
                    need(
                        self.ledger_probe(auto["provider"], auto) == auto["ledger_projection"],
                        "ledger_changed",
                    )
            with self.connect() as con:
                allowed = {
                    planner.task_key(p) for p in record["posts_intended"] if p != "auto_route"
                }
                if "auto_route" in record["posts_intended"]:
                    allowed.add(planner.AUTO_KEY)
                for table in ("gateway_tasks", "gateway_attempts"):
                    keys = {
                        row[0]
                        for row in con.execute(
                            "SELECT request_key FROM " + table + " WHERE rowid>?",
                            (self.highwater[table],),
                        )
                    }
                    need(keys <= allowed, "ledger_changed")
                need(
                    con.execute("SELECT count(*) FROM queue_jobs").fetchone()[0] == 0
                    and con.execute("SELECT count(*) FROM queue_attempts").fetchone()[0] == 0,
                    "ledger_changed",
                )
            need(self.http("GET", "/v1/usage")[0] == 200, "ledger_changed")

        def verify_ops_pins(self):
            # Nonsecret contract check only; native inspect/history after human
            # result is a distinct accepted phase with at most two auth GETs.
            source = base.read_regular(OPS_BASE / "ops_entry.py", mode=0o644)
            need(digest(source) == self.ops_expected[OPS_BASE / "ops_entry.py"], "ops_pin_changed")
            d = base.strict_json(base.read_regular(OPS_POLICY, mode=0o644))
            need(
                d["config_sha256"] == self.expected_config_sha and d["enabled"] is False,
                "ops_pin_changed",
            )
            spec = importlib.util.spec_from_file_location(
                "pinned_final_ops", OPS_BASE / "ops_entry.py"
            )
            need(spec is not None and spec.loader is not None, "ops_pin_sync")
            installed = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installed)
            installed.package()
            installed.runtime_policy()
            installed.broker_pins(d["config_sha256"])

    return Native()


def main():
    if not sys.argv[1:]:
        print(
            '{"mode":"seven_pool_review_plan","provider_posts_max":7,"Groq_new_posts":0,"host_changes":0}'
        )
        return 0
    try:
        need(sys.argv[1:] in (["--audit"], ["--apply"]), "root_scope")
        readonly = sys.argv[1:] == ["--audit"]
        scope(AUDIT_UNIT if readonly else UNIT, readonly=readonly)
        root, policy, modules = package()
        if readonly:
            result = audit(modules)
        else:
            result = transaction(make_native(root, policy, modules))
    except BaseException as error:  # noqa: BLE001 - no secret/raw response diagnostics.
        code = (
            error.args[0] if type(error) is Blocked and len(error.args) == 1 else "receipt_unknown"
        )
        result = {
            "status": "blocked",
            "code": code if code in SAFE_CODES else "receipt_unknown",
            "automatic_retry": False,
            "provider_posts": 0,
            "secret_values_exposed": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
