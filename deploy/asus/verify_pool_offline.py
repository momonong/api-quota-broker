"""Verify a sealed pool bundle using native runtime and fixture-only transports.

No apply mode, credentials, production database, systemd, network or subprocesses.
All extracted artifacts and fixture SQLite files live in a disposable private dir.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

MEMBERS = {
    "source.tar",
    "enable_provider_pool.py",
    "provider_pool_plan.py",
    "build_asus_release.py",
    "policy.json",
}


def check(value):
    if not value:
        raise ValueError("offline_gate")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    spec = importlib.util.spec_from_file_location("offline_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbid(*_, **__):
    raise RuntimeError("offline_external_operation_forbidden")


class OfflineSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        forbid()


def verify(bundle, expected_sha, *, require_native=True):
    check(not bundle.is_symlink())
    info = bundle.stat()
    check(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and info.st_size < 4 * 1024 * 1024
    )
    raw = bundle.read_bytes()
    check(sha(raw) == expected_sha)
    check(not require_native or sys.version_info[:2] == (3, 14))
    with tempfile.TemporaryDirectory(prefix="quota-pool-offline-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            seen = set()
            for member in archive:
                check(
                    member.name in MEMBERS
                    and member.name not in seen
                    and member.isfile()
                    and member.mode == 0o600
                    and member.uid == member.gid == 0
                    and not member.pax_headers
                    and member.mtime == 0
                )
                seen.add(member.name)
                value = archive.extractfile(member).read()
                check(len(value) == member.size)
                (root / member.name).write_bytes(value)
            check(seen == MEMBERS)
        policy = json.loads((root / "policy.json").read_bytes())
        for name, field in (
            ("provider_pool_plan.py", "planner_sha256"),
            ("build_asus_release.py", "verifier_sha256"),
            ("enable_provider_pool.py", "operator_sha256"),
            ("source.tar", "source_archive_sha256"),
        ):
            check(sha((root / name).read_bytes()) == policy[field])
        verifier = load(root / "build_asus_release.py")
        source = (root / "source.tar").read_bytes()
        manifest = verifier.verify_archive(source, policy["payload_manifest_sha256"])
        candidate = root / "candidate"
        verifier.extract_archive(source, policy["payload_manifest_sha256"], candidate)
        sys.path.insert(0, str(candidate / "src"))
        # Any unexpected external call fails before it can send bytes.
        socket.socket = OfflineSocket
        subprocess.run = forbid
        from quota_broker.config import cloudflare_neuron_upper_bound, load_gateway_config
        from quota_broker.gateway import Gateway
        from quota_broker.gateway_providers import safe_response_diagnostics

        planner = load(root / "provider_pool_plan.py")
        operator = load(root / "enable_provider_pool.py")
        config = root / "gateway.json"
        config.write_text(json.dumps(planner.disabled_config(normal=True)))
        check(
            len(load_gateway_config(config)) == 7
            and all(not t.enabled for t in load_gateway_config(config))
        )
        expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        qualified = planner.qualified_config(
            set(planner.PROVIDERS), expiry, policy["free_attested_at"], normal=True
        )
        config.write_text(json.dumps(qualified))
        targets = load_gateway_config(config)
        check(cloudflare_neuron_upper_bound(4096, 256) == 15)
        check(next(t for t in targets if t.provider == "cloudflare").neuron_estimate.amount == 16)
        calls = []

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
            calls.append(provider)
            if provider == "google":
                body = {
                    "modelVersion": "gemini-3.5-flash-lite-001",
                    "candidates": [
                        {"content": {"parts": [{"text": "READY"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
                }
            elif provider == "cloudflare":
                body = {
                    "success": True,
                    "result": {
                        "response": "READY",
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                }
            elif provider == "ocrspace":
                body = {
                    "OCRExitCode": 1,
                    "IsErroredOnProcessing": False,
                    "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "OK"}],
                }
            else:
                body = {
                    "model": planner.PROVIDERS[provider][0],
                    "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }
            return 200, {}, json.dumps(body).encode()

        db = root / "fixture.sqlite3"
        gateway = Gateway(
            db,
            targets,
            b"fixture-digest-more-than-32-bytes",
            lambda _: "fixture-credential",
            transport,
        )
        results = {}
        for provider, (model, _, output) in planner.PROVIDERS.items():
            task = {
                "request_key": operator.task_key(provider),
                "provider": provider,
                "model": model,
                "capability": "ocr" if provider == "ocrspace" else "text_generation",
                "input": policy["synthetic_ocr_png"]
                if provider == "ocrspace"
                else "fixture prompt",
                "max_output_tokens": output,
                "max_attempts": 1,
            }
            result = gateway.run(task)
            check(
                result["state"] in {"completed", "completed_usage_unknown"}
                and result["http_status"] == 200
            )
            if provider != "groq":
                check(operator.project_result(provider, model, result)["full_answer_verified"])
            results[provider] = (task, result)
        check(len(calls) == 7)
        restarted = Gateway(
            db,
            targets,
            b"fixture-digest-more-than-32-bytes",
            lambda _: "fixture-credential",
            transport,
        )
        for task, first in results.values():
            saved = restarted.status(task["request_key"])
            check(
                saved["state"] == first["state"]
                and saved["reported_input_tokens"] == first["reported_input_tokens"]
                and "answer" not in saved
            )
            restarted.run(task)
        check(len(calls) == 7)
        check(
            results["cloudflare"][1]["ledger_basis"] == "held_estimate"
            and results["cloudflare"][1]["reported_neurons"] is None
        )
        check(len(restarted.usage()) == 7)
        missing = safe_response_diagnostics(
            "google", 200, {}, b"{}", requested_model="gemini-3.5-flash-lite"
        )
        check(missing["provider_reported_model"] is None)
        reflected = safe_response_diagnostics(
            "groq",
            200,
            {},
            b'{"model":"openai/gpt-oss-20b"}',
            requested_model="openai/gpt-oss-20b",
            sensitive_values=("openai/gpt-oss-20b",),
        )
        check(reflected["provider_reported_model"] is None)
        for private in ("fixture prompt", "fixture-credential", '"answer"'):
            check(private not in json.dumps(restarted.usage()))
        return {
            "status": "passed",
            "python": sys.version.split()[0],
            "bundle_sha256": expected_sha,
            "source_archive_sha256": policy["source_archive_sha256"],
            "payload_manifest_sha256": policy["payload_manifest_sha256"],
            "source_files": sum(
                row["path"].startswith("src/quota_broker/") for row in manifest["release"]["files"]
            ),
            "fixture_provider_results": 7,
            "restart_no_replay": True,
            "cf_neurons_actual_unknown": True,
            "provider_calls": 0,
            "credential_reads": 0,
            "service_changes": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("expected_sha256")
    args = parser.parse_args()
    try:
        result = verify(args.bundle, args.expected_sha256)
    except Exception:  # noqa: BLE001 - no uncontrolled values in offline receipt.
        result = {
            "status": "failed",
            "code": "offline_gate",
            "provider_calls": 0,
            "credential_reads": 0,
            "service_changes": 0,
        }
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
