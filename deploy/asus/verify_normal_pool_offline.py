"""Nonroot immutable-package/real CLI/Gateway/SQLite queue fixture; no network."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(directory, wrapper_sha, verifier_sha):
    root = Path(directory).absolute()
    assert stat.S_IMODE(root.lstat().st_mode) == 0o700 and not root.is_symlink()
    names = {
        "source.tar",
        "project.whl",
        "policy.json",
        "pool_base.py",
        "pool_base_plan.py",
        "seven_pool_plan.py",
        "seven_pool_operator.py",
        "ops_history_projection.py",
        "build_asus_release.py",
        "normal_pool_plan.py",
        "normal_pool_operator.py",
        "seal.json",
        "normal-pool-once.sh",
        "verify_normal_pool_offline.py",
        "api-quota-broker.service",
        "api-quota-broker-client.service",
    }
    assert {p.name for p in root.iterdir()} == names
    raw = {}
    for name in names:
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            assert (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_gid == os.getegid()
            )
            assert (
                info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 134217728
            )
            raw[name] = stream.read(134217729)
    assert hashlib.sha256(raw["normal-pool-once.sh"]).hexdigest() == wrapper_sha
    assert hashlib.sha256(raw["verify_normal_pool_offline.py"]).hexdigest() == verifier_sha
    seal = json.loads(raw["seal.json"])
    assert set(seal["files"]) == names - {
        "seal.json",
        "normal-pool-once.sh",
        "verify_normal_pool_offline.py",
    }
    for name, sha in seal["files"].items():
        assert hashlib.sha256(raw[name]).hexdigest() == sha
    policy = json.loads(raw["policy.json"])
    builder = load(root / "build_asus_release.py", "offline_normal_builder")
    manifest = builder.verify_archive(raw["source.tar"], policy["payload_manifest_sha256"])

    def guard(event, args):
        if event in {
            "socket.__new__",
            "socket.connect",
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
        }:
            raise AssertionError("offline_effect_forbidden")

    sys.addaudithook(guard)
    with tempfile.TemporaryDirectory(prefix="aqb-normal-offline-") as tmp:
        extracted = Path(tmp) / "source"
        builder.extract_archive(raw["source.tar"], policy["payload_manifest_sha256"], extracted)
        sys.path.insert(0, str(extracted / "src"))
        from quota_broker import cli
        from quota_broker.config import load_gateway_config
        from quota_broker.gateway import Gateway, validate_task
        from quota_broker.queue import DurableQueue

        planner = load(root / "normal_pool_plan.py", "offline_normal_planner")
        r2 = load(root / "seven_pool_plan.py", "offline_normal_r2")
        base = load(root / "pool_base_plan.py", "offline_normal_base")
        operator = load(root / "normal_pool_operator.py", "offline_normal_operator")
        assert len(operator.wheel_files(raw["project.whl"], manifest["release"]["files"])) == 30
        now = datetime.now(UTC)
        original = r2.config(
            base,
            set(r2.PROVIDERS),
            policy["fixture_only_admission"],
            "2026-11-02T03:36:17+00:00",
            normal=True,
            now=now,
        )
        cfg = Path(tmp) / "gateway.json"
        cfg.write_text(json.dumps(planner.config(original)))
        calls = []

        def fake(url, headers, payload, timeout):
            calls.append(url)
            assert (
                payload.get(
                    "max_completion_tokens",
                    payload.get(
                        "max_tokens", payload.get("generationConfig", {}).get("maxOutputTokens")
                    ),
                )
                == 2048
            )
            answer = "Public fictional library reading club guide. " * 45
            if "googleapis" in url:
                value = {
                    "modelVersion": "gemini-3.5-flash-lite",
                    "candidates": [
                        {"content": {"parts": [{"text": answer}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 512},
                }
            elif "cloudflare" in url:
                value = {
                    "success": True,
                    "result": {
                        "response": answer,
                        "usage": {"prompt_tokens": 10, "completion_tokens": 512},
                    },
                }
            else:
                value = {
                    "model": payload["model"],
                    "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 512},
                }
            return 200, {}, json.dumps(value).encode()

        path = Path(tmp) / "state"
        path.mkdir(mode=0o700)
        gateway = Gateway(
            path / "ledger.sqlite3",
            load_gateway_config(cfg),
            b"public-fixture-hmac-over-32-bytes",
            lambda ref: "f" * 32 if "ACCOUNT" in ref else "public-fixture-not-real-key",
            fake,
            clock=lambda: now,
        )
        (path / "ledger.sqlite3").chmod(0o600)
        queue = DurableQueue(gateway, b"Q" * 32)  # Public deterministic fixture only.
        cli.read_runtime_client = lambda: "public-fixture-client-over-32-characters"

        def http(url, body, headers, timeout):
            action = url.removeprefix("http://127.0.0.1:18084")
            if action == "/v1/tasks":
                return gateway.run(body)
            if action == "/v1/queue":
                return queue.submit(body)
            if action.endswith("/result"):
                return queue.result(action.split("/")[-2])
            if action.startswith("/v1/queue/"):
                return queue.status(action.rsplit("/", 1)[1])
            if action == "/v1/catalog":
                return gateway.catalog()
            raise AssertionError("unexpected_fixture_endpoint")

        cli._json_http = http

        def invoke(action, body=None, key=None):
            args = ["quota-broker", "gateway", "--json", action]
            if body:
                args += [
                    "--request-key",
                    body["request_key"],
                    "--capability",
                    "text_generation",
                    "--task-stdin",
                ]
            if key:
                args.append(key)
            sys.argv = args
            sys.stdin = io.StringIO(json.dumps(body) if body else "")
            stream = io.StringIO()
            with redirect_stdout(stream):
                cli.main()
            return json.loads(stream.getvalue())

        catalog = invoke("catalog")
        assert len(catalog) == 7
        outcomes = []
        for body in operator.probes():
            if body["request_key"] == operator.QUEUE_KEY:
                assert invoke("submit", body)["state"] == "queued"
                assert queue.tick("public_fixture_worker")["state"] == "completed"
                value = invoke("result", key=operator.QUEUE_KEY)
            else:
                value = invoke("run", body)
            assert value["http_status"] == 200 and value["reported_output_tokens"] > 64
            outcomes.append(value)
        assert len(calls) == 3 and outcomes[0]["provider"] != "ocrspace"
        assert outcomes[2]["ledger_basis"] == "held_estimate"
        assert (
            validate_task(
                {"request_key": "public-default", "capability": "text_generation", "input": "Hi"}
            )["max_output_tokens"]
            == 1024
        )
        assert queue.tick("public_fixture_restart") == None
    return {
        "status": "passed",
        "mode": "normal_pool_offline_public_fixture",
        "python": ".".join(map(str, sys.version_info[:3])),
        "source_payload_verified": True,
        "wheel_version": "1.0.0",
        "default_output_tokens": 1024,
        "fake_provider_dispatches": 3,
        "cli_submit_worker_result_auto_verified": True,
        "new_network_calls": 0,
        "credential_reads": 0,
        "provider_posts": 0,
        "formal_DB_write": 0,
        "service_changes": 0,
        "root_deployment_executed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--wrapper-sha256", required=True)
    parser.add_argument("--verifier-sha256", required=True)
    args = parser.parse_args()
    try:
        result = run(args.directory, args.wrapper_sha256, args.verifier_sha256)
        print(json.dumps(result, sort_keys=True))
    except BaseException:  # noqa: BLE001 - fixed error only; no input/output/credential logs
        print('{"status":"blocked","code":"normal_pool_offline_unverified"}')
        raise SystemExit(1) from None
