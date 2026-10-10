"""Bounded new once scope on a real Gateway/SQLite; fake provider transport only."""

import hashlib
import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway

ROOT = Path(__file__).parents[1]


def load(name, directory="deploy/asus"):
    spec = importlib.util.spec_from_file_location(name, ROOT / directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan, operator, base_plan = map(
    load, ("seven_pool_plan", "seven_pool_operator", "provider_pool_plan")
)
NOW = datetime(2026, 10, 8, 4, tzinfo=UTC)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAsCAAAAADwNOEjAAAASElEQVR4nGP4TyFgGDVg2BnAgATQxXHxh5MBeBXhYI9EA9DVjDQDsGkeaQZgUzMSDcCqbhgZAJPElmjwGj7MDCAHjBowLAwAAM/oF2guJ3zDAAAAAElFTkSuQmCC"


def evidence():
    return {
        provider: {
            "model": model,
            "key_ref": ref,
            "account_scope": "asus-dev-" + provider + "-key",
            "human_free_no_payment_details": True,
            "account_basis": "existing_human_free_declaration_same_key_slot",
            "account_reviewed_at": NOW.isoformat(),
            "account_valid_until": (NOW + timedelta(days=14)).isoformat(),
            "official_url": plan.OFFICIAL[provider],
            "official_reviewed_at": NOW.isoformat(),
            "official_valid_until": (NOW + timedelta(days=14)).isoformat(),
        }
        for provider, (model, ref, _) in plan.PROVIDERS.items()
    }


class Fixture:
    def __init__(self, root, fault=None):
        self.root, self.fault = root, fault
        self.claimed, self.changed = False, False
        self.budget = operator.Budget()
        self.record = None
        self.calls, self.posts, self.switches, self.ops_checks = [], [], [], 0
        self.db, self.path = root / "ledger.sqlite3", root / "gateway.json"
        self.path.write_text('{"targets":[]}')
        self.gateway = self.make_gateway()
        self.evidence = evidence()
        if fault == "ineligible":
            self.evidence["mistral"]["human_free_no_payment_details"] = False

    def make_gateway(self):
        targets = load_gateway_config(self.path)
        hosts = {
            "nvidia": "integrate.api.nvidia.com",
            "google": "generativelanguage.googleapis.com",
            "mistral": "api.mistral.ai",
            "cloudflare": "api.cloudflare.com",
            "openrouter": "openrouter.ai",
            "ocrspace": "api.ocr.space",
            "groq": "api.groq.com",
        }

        def transport(url, _headers, body, _timeout):
            provider = next(name for name, host in hosts.items() if host in url)
            assert provider != "groq", "this stage cannot call Groq"
            self.calls.append(provider)
            status = 429 if self.fault == provider else 200
            if status != 200:
                return status, {}, b'{"error":{"type":"rate_limit_error"}}'
            if provider == "google":
                value = {
                    "modelVersion": plan.PROVIDERS[provider][0] + "-001",
                    "candidates": [
                        {"content": {"parts": [{"text": "READY"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
                }
            elif provider == "cloudflare":
                value = {
                    "success": True,
                    "result": {
                        "response": "READY",
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                }
            elif provider == "ocrspace":
                value = {
                    "OCRExitCode": 1,
                    "IsErroredOnProcessing": False,
                    "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "OK"}],
                }
            else:
                value = {
                    "model": plan.PROVIDERS[provider][0],
                    "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }
            return 200, {}, json.dumps(value).encode()

        return Gateway(
            self.db,
            targets,
            b"PUBLIC_HMAC_FIXTURE_MORE_THAN_32_BYTES",
            lambda name: "0" * 32 if name == "CLOUDFLARE_ACCOUNT_ID" else "PUBLIC_FIXTURE_KEY",
            transport,
            clock=lambda: NOW,
        )

    def preflight(self):
        pass

    def claim(self, record):
        self.claimed = True
        self.save(record)

    def save(self, record):
        self.record = json.loads(json.dumps(record))

    def stage(self):
        pass

    def eligible(self):
        return {
            provider
            for provider in plan.PROVIDERS
            if plan.evidence_valid(provider, self.evidence[provider], NOW)
        }

    def tasks(self):
        return plan.tasks(PNG)

    def configuration(self, accepted, *, normal=False):
        return plan.config(
            base_plan,
            accepted,
            self.evidence,
            (NOW + timedelta(days=30)).isoformat(),
            normal=normal,
            now=NOW,
        )

    def switch(self, config, *, original=False):
        self.budget.restart(rollback=original)
        self.changed = True
        self.switches.append(config)
        self.path.write_text(json.dumps(config))
        self.gateway = self.make_gateway()

    def preservation(self):
        return {"history_preserved": True, "OrderFlow_unchanged": True, "SSH_unchanged": True}

    def http(self, method, path, body=None):
        assert method == "POST" and path == "/v1/tasks"
        self.posts.append(body["request_key"])
        assert body["max_attempts"] == 1 and body["max_output_tokens"] <= 64
        if self.fault == "unknown" and body.get("provider") == "nvidia":
            raise TimeoutError("PUBLIC_PRIVATE_DO_NOT_PRINT")
        return 200, self.gateway.run(body)

    def observe(self, provider, value):
        if value is None:
            return {
                "provider": provider,
                "full_answer_verified": False,
                "state": "unknown",
                "blocker": "delivery_unknown",
            }
        with sqlite3.connect(self.db) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM gateway_attempts WHERE request_key=?",
                    (value["request_key"],),
                ).fetchone()[0]
                <= 1
            )
        return {
            "provider": provider,
            "full_answer_verified": value["state"] in {"completed", "completed_usage_unknown"},
            "state": value["state"],
            "request_key": value["request_key"],
        }

    def auto_probe(self, record, candidates):
        self.budget.post()
        record["posts_intended"].append("auto_route")
        self.save(record)
        body = plan.auto_task()
        explained = self.gateway.explain(body)
        assert isinstance(explained, dict) and all(
            t.provider != "groq" for t in self.gateway.targets if t.enabled
        )
        value = self.gateway.run(body)
        selected = value.get("provider")
        assert selected in candidates and selected not in {"groq", "ocrspace"}
        with sqlite3.connect(self.db) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM gateway_attempts WHERE request_key=?", (plan.AUTO_KEY,)
                ).fetchone()[0]
                <= 1
            )
        return {
            "provider": selected,
            "state": value["state"],
            "full_answer_verified": value["state"] in {"completed", "completed_usage_unknown"},
            "request_key": plan.AUTO_KEY,
        }

    def verify_all(self, record):
        assert len(self.posts) <= 6 and len(self.calls) <= 7
        if self.fault == "preservation":
            raise operator.Blocked("ledger_changed")

    def verify_ops_pins(self):
        self.ops_checks += 1
        if self.fault == "ops_gate":
            raise operator.Blocked("ops_pin_sync")

    def command(self, _):
        pytest.fail("no host service command in fixture")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "nvidia",
        "google",
        "mistral",
        "cloudflare",
        "openrouter",
        "ocrspace",
        "unknown",
        "ineligible",
    ],
)
def test_real_gateway_scope_probe_once_auto_one_no_groq_or_ocr_text_fallback(tmp_path, fault):
    fixture = Fixture(tmp_path, fault)
    result = operator.transaction(fixture)
    assert result["status"] == "passed" and result["provider_posts"] <= 7
    assert len(set(fixture.posts)) == len(fixture.posts) <= 6
    assert len(fixture.calls) <= 7 and "groq" not in fixture.calls
    assert result["Broker_restarts"] == 3 and fixture.ops_checks == 1
    if fault in plan.PROVIDERS or fault in {"unknown", "ineligible"}:
        provider = "nvidia" if fault == "unknown" else "mistral" if fault == "ineligible" else fault
        assert provider not in result["enabled_providers"]
    assert all(t["max_output_tokens"] <= 64 for cfg in fixture.switches for t in cfg["targets"])
    assert len(fixture.switches[-1]["targets"]) == 7
    assert "PUBLIC_PRIVATE" not in json.dumps(result)


def test_missing_admission_does_not_get_renewed_by_longer_credential(tmp_path):
    ev = evidence()
    ev["google"]["account_valid_until"] = (NOW - timedelta(seconds=1)).isoformat()
    cfg = plan.config(
        base_plan, set(plan.PROVIDERS), ev, (NOW + timedelta(days=365)).isoformat(), now=NOW
    )
    target = next(t for t in cfg["targets"] if t["provider"] == "google")
    assert not target["enabled"] and not target["free_eligible"] and target["expires_at"] is None


def test_explicit_probe_shapes_keys_preserve_scopes_and_metadata_unknown():
    tasks = plan.tasks(PNG)
    assert (
        set(tasks) == set(plan.PROVIDERS) - {"groq"}
        and tasks["openrouter"]["max_output_tokens"] == 64
    )
    assert (
        tasks["ocrspace"]["model"] == "ocr.space/engine2"
        and tasks["ocrspace"]["capability"] == "ocr"
    )
    cfg = plan.config(
        base_plan,
        set(plan.PROVIDERS),
        evidence(),
        (NOW + timedelta(days=30)).isoformat(),
        normal=True,
        now=NOW,
    )
    assert all(
        f["remaining"]["provenance"] == "unknown"
        for t in cfg["targets"]
        for f in t["provider_quota_facts"]
    )
    groq = next(t for t in cfg["targets"] if t["provider"] == "groq")
    assert (
        groq["account_id"] == "asus-dev-groq-key"
        and groq["shared_concurrency_scope"] == "groq:asus-dev-key:shared"
    )


def test_budget_deadline_reserves_full_provider_call_and_never_eighth_post():
    tick = [0]
    budget = operator.Budget(clock=lambda: tick[0])
    for _ in range(7):
        budget.post()
    with pytest.raises(operator.Blocked, match="post_budget"):
        budget.post()
    budget = operator.Budget(clock=lambda: tick[0])
    tick[0] = 900
    with pytest.raises(operator.Blocked, match="deadline_budget"):
        budget.post()


def test_post_restart_ledger_failure_restores_original_only_once(tmp_path):
    fixture = Fixture(tmp_path, "preservation")
    result = operator.transaction(fixture)
    assert result["status"] == "blocked" and result["original_restored"] is True
    assert fixture.budget.restarts == 4 and fixture.switches[-1] == {"targets": []}


def native_pin_fixture(tmp_path, monkeypatch):
    base = load("enable_provider_pool")
    root = tmp_path / "ops"
    root.mkdir()
    monkeypatch.setattr(operator, "OPS_BASE", root)
    monkeypatch.setattr(operator, "OPS_POLICY", root / "policy.json")

    def read(path, *, mode=0o600, **_):
        assert path.stat().st_mode & 0o777 == mode
        return path.read_bytes()

    def write(path, raw, *, mode=0o600, **_):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)

    monkeypatch.setattr(base, "read_regular", read)
    monkeypatch.setattr(base, "write_exclusive", write)
    monkeypatch.setattr(base, "sync_directory", lambda _: None)
    modules = {
        "pool_base.py": base,
        "seven_pool_plan.py": plan,
        "pool_base_plan.py": base_plan,
        "build_asus_release.py": None,
    }
    native = operator.make_native(tmp_path, {"payload_manifest_sha256": "f" * 64}, modules)
    originals = {
        root / "ops_entry.py": ('RELEASE = "' + base.OLD_RELEASE + '"\n').encode(),
        root / "manifest.json": json.dumps(
            {"schema": 2, "files": {"ops_entry.py": "0" * 64}, "units": {}}
        ).encode(),
        root / "policy.json": json.dumps(
            {
                "config_sha256": base.EMPTY_SHA,
                "enabled": False,
                "expires_at": "2026-11-05T04:33:31+00:00",
            }
        ).encode(),
    }
    native.ops_originals = originals
    for path, raw in originals.items():
        path.write_bytes(raw)
        path.chmod(0o644)
        native.ops_expected[path] = operator.digest(raw)
        native.ops_owned_hashes[path] = {operator.digest(raw)}
    return native, base, originals


def test_actual_ops_atomic0644_manifest_last_and_original_expiry_restored(tmp_path, monkeypatch):
    native, _base, originals = native_pin_fixture(tmp_path, monkeypatch)
    cfg = {"targets": []}
    native.sync_ops(cfg, False)
    root = operator.OPS_BASE
    entry = (root / "ops_entry.py").read_bytes()
    assert "release-" + "f" * 64 in entry.decode()
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["files"]["ops_entry.py"] == hashlib.sha256(entry).hexdigest()
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in originals)
    d = json.loads((root / "policy.json").read_bytes())
    assert d["enabled"] is False and d["expires_at"] == "2026-11-05T04:33:31+00:00"
    native.sync_ops(cfg, True)
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_ops_partial_post_replace_fsync_failure_can_restore_owned_hash(tmp_path, monkeypatch):
    native, base, originals = native_pin_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        base, "sync_directory", lambda _: (_ for _ in ()).throw(OSError("PUBLIC_IO_ERROR"))
    )
    with pytest.raises(OSError):
        native.sync_ops({"targets": []}, False)
    monkeypatch.setattr(base, "sync_directory", lambda _: None)
    native.sync_ops({"targets": []}, True)
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_ops_foreign_drift_is_not_overwritten_by_rollback(tmp_path, monkeypatch):
    native, _, _originals = native_pin_fixture(tmp_path, monkeypatch)
    path = operator.OPS_BASE / "ops_entry.py"
    path.write_bytes(b"PUBLIC_FOREIGN_WORK")
    with pytest.raises(operator.Blocked, match="ops_pin_changed"):
        native.sync_ops({"targets": []}, True)
    assert path.read_bytes() == b"PUBLIC_FOREIGN_WORK"


def test_final_ops_gate_failure_stays_inside_root_rollback_transaction(tmp_path):
    fixture = Fixture(tmp_path, "ops_gate")
    result = operator.transaction(fixture)
    assert result["status"] == "blocked" and result["code"] == "ops_pin_sync"
    assert result["original_restored"] is True and fixture.budget.restarts == 4


def test_native_restart_whitelist_includes_only_consumed_auto_key(tmp_path, monkeypatch):
    native, _base, _originals = native_pin_fixture(tmp_path, monkeypatch)
    db = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE gateway_tasks(request_key TEXT);CREATE TABLE gateway_attempts(request_key TEXT);CREATE TABLE queue_jobs(id TEXT);CREATE TABLE queue_attempts(id TEXT);"
        )
        con.execute("INSERT INTO gateway_tasks VALUES(?)", (plan.AUTO_KEY,))
        con.execute("INSERT INTO gateway_attempts VALUES(?)", (plan.AUTO_KEY,))
    native.highwater = {"gateway_tasks": 0, "gateway_attempts": 0}
    native.connect = lambda: sqlite3.connect(db)
    native.http = lambda *_args, **_kwargs: (200, [])
    native.verify_all({"results": [], "automatic_route": {}, "posts_intended": ["auto_route"]})
    with pytest.raises(operator.Blocked, match="ledger_changed"):
        native.verify_all({"results": [], "automatic_route": {}, "posts_intended": []})
