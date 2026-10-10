"""Verified candidate fixture only. No credential, provider, root or service IO."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def need(ok):
    if not ok:
        raise ValueError("seven_offline_gate")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(directory, expected_wrapper, expected_verifier):
    directory = Path(directory)
    meta = directory.lstat()
    need(
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == meta.st_gid == os.getuid()
        and stat.S_IMODE(meta.st_mode) == 0o700
    )

    def read(name, bound=131072):
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            a = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(a.st_mode)
                and a.st_uid == a.st_gid == os.getuid()
                and a.st_nlink == 1
                and stat.S_IMODE(a.st_mode) == 0o600
                and a.st_size <= bound
            )
            raw = stream.read(bound + 1)
            b = os.fstat(stream.fileno())
            need(
                len(raw) == a.st_size
                and (a.st_ino, a.st_mtime_ns, a.st_ctime_ns)
                == (b.st_ino, b.st_mtime_ns, b.st_ctime_ns)
            )
            return raw

    need(
        hashlib.sha256(read("seven-pool-once.sh")).hexdigest() == expected_wrapper
        and hashlib.sha256(read("verify_seven_pool_offline.py")).hexdigest() == expected_verifier
    )
    seal = json.loads(read("seal.json"))
    expected = {
        "seven_pool_operator.py",
        "seven_pool_plan.py",
        "pool_base.py",
        "pool_base_plan.py",
        "ops_history_projection.py",
        "build_asus_release.py",
        "source.tar",
        "policy.json",
    }
    need(
        set(seal) == {"schema", "files"} and seal["schema"] == 1 and set(seal["files"]) == expected
    )
    need(
        {p.name for p in directory.iterdir()}
        == expected | {"seal.json", "seven-pool-once.sh", "verify_seven_pool_offline.py"}
    )
    for name, sha in seal["files"].items():
        need(
            hashlib.sha256(read(name, 134217728 if name == "source.tar" else 131072)).hexdigest()
            == sha
        )
    policy = json.loads(read("policy.json"))
    builder = load(directory / "build_asus_release.py", "public_release_verifier")
    archive = read("source.tar", 134217728)
    need(hashlib.sha256(archive).hexdigest() == policy["source_archive_sha256"])
    builder.verify_archive(archive, policy["payload_manifest_sha256"])
    with tempfile.TemporaryDirectory(prefix="seven-pool-public-") as work:
        root = Path(work)
        release = root / "release"
        builder.extract_archive(archive, policy["payload_manifest_sha256"], release)
        sys.path.insert(0, str(release / "src"))
        from quota_broker.config import load_gateway_config
        from quota_broker.gateway import Gateway

        plan = load(directory / "seven_pool_plan.py", "public_seven_plan")
        base = load(directory / "pool_base_plan.py", "public_base_plan")
        now = datetime.now(UTC)
        cfg = plan.config(
            base,
            set(plan.PROVIDERS) - {"groq"},
            policy["admission"],
            "2026-11-02T03:36:17+00:00",
            now=now,
        )
        path = root / "gateway.json"
        path.write_text(json.dumps(cfg))
        calls = []

        def transport(url, headers, body, timeout):
            hosts = {
                "nvidia": "integrate.api.nvidia.com",
                "google": "generativelanguage.googleapis.com",
                "mistral": "api.mistral.ai",
                "cloudflare": "api.cloudflare.com",
                "openrouter": "openrouter.ai",
                "ocrspace": "api.ocr.space",
            }
            provider = next(name for name, host in hosts.items() if host in url)
            calls.append(provider)
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

        def no_network(event, args):
            if (
                event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
                or event == "os.system"
            ):
                raise ValueError("offline_effect_denied")

        sys.addaudithook(no_network)
        db = root / "ledger.sqlite3"
        gateway = Gateway(
            db,
            load_gateway_config(path),
            b"PUBLIC_HMAC_32_BYTE_FIXTURE_ONLY_VALUE",
            lambda name: "0" * 32 if name == "CLOUDFLARE_ACCOUNT_ID" else "PUBLIC_FIXTURE_KEY",
            transport,
        )
        tasks = plan.tasks(policy["synthetic_ocr_png"])
        for body in tasks.values():
            need(body["max_attempts"] == 1 and body["max_output_tokens"] <= 64)
            value = gateway.run(body)
            need(value["state"] in {"completed", "completed_usage_unknown"})
        cfg = plan.config(
            base,
            set(plan.PROVIDERS) - {"groq", "ocrspace"},
            policy["admission"],
            "2026-11-02T03:36:17+00:00",
            normal=True,
            now=now,
        )
        path.write_text(json.dumps(cfg))
        gateway = Gateway(
            db,
            load_gateway_config(path),
            b"PUBLIC_HMAC_32_BYTE_FIXTURE_ONLY_VALUE",
            lambda name: "0" * 32 if name == "CLOUDFLARE_ACCOUNT_ID" else "PUBLIC_FIXTURE_KEY",
            transport,
        )
        before = len(calls)
        value = gateway.run(plan.auto_task())
        need(
            len(calls) <= 7
            and len(calls) - before <= 1
            and value["provider"] not in {"groq", "ocrspace"}
        )
        import sqlite3

        with sqlite3.connect(db) as con:
            need(con.execute("SELECT count(*) FROM gateway_attempts").fetchone()[0] == 7)
        # Reconstructing the gateway/status never reruns an original key.
        for body in tasks.values():
            gateway.status(body["request_key"])
        need(len(calls) == 7)
    return {
        "status": "passed",
        "mode": "seven_pool_offline_public_fixture",
        "source_payload_verified": True,
        "python": ".".join(map(str, sys.version_info[:3])),
        "six_pinned_fake_dispatches": 6,
        "one_auto_fake_dispatch": 1,
        "new_Groq_calls": 0,
        "max_output_tokens": 64,
        "credential_reads": 0,
        "provider_posts": 0,
        "service_changes": 0,
        "formal_DB_write": 0,
        "root_deployment_executed": False,
    }


if __name__ == "__main__":
    if not sys.argv[1:]:
        print('{"mode":"seven_pool_offline_verifier_plan","host_changes":0}')
    else:
        try:
            need(
                len(sys.argv) == 7
                and sys.argv[1] == "--directory"
                and sys.argv[3] == "--wrapper-sha256"
                and sys.argv[5] == "--verifier-sha256"
            )
            print(json.dumps(verify(sys.argv[2], sys.argv[4], sys.argv[6]), sort_keys=True))
        except BaseException:  # noqa: BLE001 - no raw errors or response values.
            print(
                '{"status":"blocked","code":"seven_pool_offline_unverified","automatic_retry":false}'
            )
            raise SystemExit(1) from None
