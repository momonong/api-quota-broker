"""Operator failures use a real Gateway/SQLite fixture; no credentials or HTTP."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/asus"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operator, planner = load("enable_provider_pool"), load("provider_pool_plan")


def body(provider, status=200):
    if status != 200:
        return {"error": {"type": "rate_limit_error" if status == 429 else "authentication_error"}}
    if provider == "google":
        return {
            "modelVersion": "gemini-3.5-flash-lite-001",
            "candidates": [{"content": {"parts": [{"text": "READY"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
        }
    if provider == "cloudflare":
        return {
            "success": True,
            "result": {"response": "READY", "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        }
    if provider == "ocrspace":
        return {
            "OCRExitCode": 1,
            "IsErroredOnProcessing": False,
            "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "OK"}],
        }
    return {
        "model": planner.PROVIDERS[provider][0],
        "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


class FixtureOps(operator.Ops):
    def __init__(self, root, faults=None):
        super().__init__(
            root,
            {"free_attested_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
            planner,
            None,
        )
        self.db = root / "ledger.sqlite3"
        self.config = root / "gateway.json"
        self.expiry = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        self.faults = faults or {}
        self.provider_calls = []
        self.posts, self.switches, self.receipts = [], [], []
        self.original = b'{"targets":[]}\n'
        self.config.write_bytes(self.original)
        self.gateway = self.make_gateway()
        with self.connect() as con:
            con.execute("CREATE TABLE queue_jobs(request_key TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE queue_attempts(request_key TEXT PRIMARY KEY)")

    def make_gateway(self):
        targets = load_gateway_config(self.config)

        def transport(url, *_):
            provider = next(
                name
                for name, host in {
                    "nvidia": "integrate.api.nvidia.com",
                    "google": "generativelanguage.googleapis.com",
                    "mistral": "api.mistral.ai",
                    "cloudflare": "api.cloudflare.com",
                    "openrouter": "openrouter.ai",
                    "ocrspace": "api.ocr.space",
                    "groq": "api.groq.com",
                }.items()
                if host in url
            )
            self.provider_calls.append(provider)
            status = self.faults.get(provider, 200)
            if status == "provider_timeout":
                raise TimeoutError
            return status, {}, json.dumps(body(provider, status)).encode()

        return Gateway(
            self.db,
            targets,
            b"fixture-digest-more-than-32-bytes",
            lambda _: "fixture-credential",
            transport,
        )

    def connect(self):
        return sqlite3.connect(self.db)

    def preflight(self):
        self.history = self.history_rows(initial=True)

    def claim(self, record):
        self.claimed = True
        self.save(record)
        if self.faults.get("phase") == "backup":
            raise RuntimeError("private-exception-must-never-leak")

    def save(self, record):
        self.receipts.append(json.loads(json.dumps(record)))

    def stage(self):
        if self.faults.get("phase") == "stage":
            raise RuntimeError("private-exception-must-never-leak")

    def tasks(self):
        return {
            provider: {
                "request_key": operator.task_key(provider),
                "provider": provider,
                "model": model,
                "capability": "ocr" if provider == "ocrspace" else "text_generation",
                "input": "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAsCAAAAADwNOEjAAAASElEQVR4nGP4TyFgGDVg2BnAgATQxXHxh5MBeBXhYI9EA9DVjDQDsGkeaQZgUzMSDcCqbhgZAJPElmjwGj7MDCAHjBowLAwAAM/oF2guJ3zDAAAAAElFTkSuQmCC"
                if provider == "ocrspace"
                else "fixture input",
                "max_output_tokens": output,
                "max_attempts": 1,
            }
            for provider, (model, _, output) in planner.PROVIDERS.items()
            if provider != "groq"
        }

    def switch(self, config, *, original=False):
        self.changed = True
        if original and self.faults.get("restore"):
            raise RuntimeError("private-exception")
        self.switches.append(config)
        self.config.write_text(json.dumps(config))
        self.gateway = self.make_gateway()

    def preservation(self):
        assert self.history_rows() == self.history
        return {
            "history_files_preserved": True,
            "historical_rows_preserved": True,
            "orderflow_unchanged": True,
        }

    def http(self, method, path, data=None):
        if method == "POST":
            provider = data["provider"]
            assert provider not in self.posts
            assert self.receipts[-1]["posts_intended"][-1] == provider
            self.posts.append(provider)
            if self.faults.get(provider) == "loopback_timeout":
                raise TimeoutError
            try:
                result = self.gateway.run(data)
            except GatewayError:
                return 503, {"error": "unavailable"}
            return 200, result
        if path == "/v1/usage":
            return 200, self.gateway.usage()
        try:
            return 200, self.gateway.status(path.removeprefix("/v1/tasks/"))
        except GatewayError:
            return 404, {"error": "not_found"}

    def command(self, args):
        self.stopped = args[1] == "stop"


def test_six_once_real_gateway_usage_and_restart(tmp_path):
    ops = FixtureOps(tmp_path)
    result = operator.run(ops)
    assert result["status"] == "passed", result
    assert set(result["enabled_providers"]) == set(planner.PROVIDERS)
    assert len(ops.posts) == len(ops.provider_calls) == 6
    assert "groq" not in ops.provider_calls
    cf = next(r for r in result["results"] if r["provider"] == "cloudflare")
    assert cf["state"] == "completed_usage_unknown" and cf["ledger_basis"] == "held_estimate"
    assert result["restart_verified"]
    for private in ("fixture input", "fixture-credential", '"answer"', '"raw"'):
        assert private not in json.dumps(result)


@pytest.mark.parametrize("fault", [429, 401, 403, 402, 404, "provider_timeout", "loopback_timeout"])
def test_one_blocked_provider_does_not_stop_others_or_replay(tmp_path, fault):
    ops = FixtureOps(tmp_path, {"nvidia": fault})
    result = operator.run(ops)
    assert result["status"] == "passed", result
    assert len(ops.posts) == 6
    assert "nvidia" not in result["enabled_providers"]
    assert set(result["enabled_providers"]) == set(planner.PROVIDERS) - {"nvidia"}
    assert result["results"][0]["blocker"]
    assert all(ops.posts.count(provider) == 1 for provider in ops.posts)


@pytest.mark.parametrize("phase", ["backup", "stage"])
def test_claim_failure_preserves_fixed_receipt(tmp_path, phase):
    ops = FixtureOps(tmp_path, {"phase": phase})
    result = operator.run(ops)
    assert result["status"] == "failed" and ops.claimed
    assert ops.receipts[-1]["status"] == "failed"
    assert not ops.posts and "private-exception" not in json.dumps(result)


def test_preservation_failure_restores_and_stops_if_restore_fails(tmp_path):
    ops = FixtureOps(tmp_path, {"restore": True})

    def fail():
        raise operator.GateError("fixture")

    ops.preservation = fail
    result = operator.run(ops)
    assert result["status"] == "failed" and result["broker_stopped"] is True
    assert result["original_restored"] is False and not ops.posts


def test_missing_model_does_not_prevent_full_success_and_different_model_is_rejected():
    result = {
        "request_key": operator.task_key("nvidia"),
        "provider": "nvidia",
        "model": planner.PROVIDERS["nvidia"][0],
        "state": "completed",
        "http_status": 200,
        "answer": "fixture private output",
        "finish_reason": "stop",
        "response_truncated": False,
        "diagnostics": {"provider_model_status": "missing"},
    }
    safe = operator.project_result("nvidia", result["model"], result)
    assert safe["full_answer_verified"] and safe["provider_reported_model"] is None
    result["diagnostics"] = {
        "provider_model_status": "reported",
        "provider_reported_model": "another-public-model",
    }
    assert not operator.project_result("nvidia", result["model"], result)["full_answer_verified"]
    assert "fixture private output" not in json.dumps(safe)
