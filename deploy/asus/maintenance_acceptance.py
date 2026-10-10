"""Ordinary-user acceptance: fixed three original keys, bounded safe projections."""

import ctypes
import hashlib
import json
import os
import re
import resource
import subprocess
from pathlib import Path

PREFIX = "asus-normal-v1-2026-10-08-r1-"
KEYS = [PREFIX + s for s in ("queued-auto-a1", "google-long-a1", "cloudflare-long-a1")]
MODELS = {
    "nvidia": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "google": "gemini-3.5-flash-lite",
    "mistral": "ministral-3b-latest",
    "cloudflare": "@cf/meta/llama-3.2-1b-instruct",
    "openrouter": "liquid/lfm-2.5-2.6b:free",
    "groq": "openai/gpt-oss-20b",
    "ocrspace": "ocr.space/engine2",
}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
STATES = {
    "queued",
    "waiting",
    "running",
    "completed",
    "completed_usage_unknown",
    "failed",
    "unknown",
    "cancelled",
    "expired",
    "quota_rejected",
    "quota_exhausted",
    "rejected",
    "preparing",
    "dispatched",
}
NUMERIC = {
    "http_status",
    "latency_ms",
    "estimated_input_tokens",
    "reported_input_tokens",
    "reported_output_tokens",
    "reported_neurons",
    "input_bytes",
    "max_output_tokens",
    "max_attempts",
    "attempt_count",
}


def guard():
    assert os.getuid() == os.geteuid() == 1000 and os.uname().nodename == "asus-ubuntu2604-server"
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split("::", 1)[
        1
    ].lstrip("/")
    assert (group / "memory.swap.max").read_text().strip() == "0" and (
        group / "memory.swap.current"
    ).read_text().strip() == "0"


def cli(action, key=None, body=None):
    args = ["/usr/local/bin/aqb", "--json", "--http-timeout", "185", action]
    if key is not None:
        args.append(key)
    if action == "recent":
        args += ["--limit", "100"]
    if body is not None:
        args += [
            "--request-key",
            body["request_key"],
            "--capability",
            "text_generation",
            "--task-stdin",
        ]
    try:
        p = subprocess.run(
            args,
            input=json.dumps(body).encode() if body else b"",
            capture_output=True,
            env=ENV,
            timeout=190,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"transport": "unknown_timeout"}, None
    if p.returncode:
        try:
            error = json.loads(p.stderr) if len(p.stderr) < 16384 else {}
        except (ValueError, UnicodeError):
            error = {}
        code = error.get("error") if type(error) is dict else None
        allowed = {
            "not_found",
            "invalid_request",
            "unauthorized",
            "forbidden",
            "broker_request_failed",
            "queue_disabled",
            "no_eligible_target",
            "credential_unavailable",
        }
        code = code if code in allowed else "unclassified"
        return {
            "transport": "not_found" if code == "not_found" else "client_error",
            "rc": p.returncode,
            "error_code": code,
        }, None
    assert len(p.stdout) <= 524288
    return {"transport": "received", "rc": 0}, json.loads(p.stdout)


def task(value):
    assert type(value) is dict
    out = {}
    for k in NUMERIC:
        v = value.get(k)
        if v is None or type(v) in (int, float):
            out[k] = v
    for k in ("request_key", "execution_key"):
        v = value.get(k)
        if v in KEYS or type(v) is str and re.fullmatch(r"q-[a-f0-9]{32}", v):
            out[k] = v
    state = value.get("state")
    out["state"] = state if state in STATES else "unclassified"
    if value.get("provider") in MODELS:
        out["provider"] = value["provider"]
    if value.get("model") in MODELS.values():
        out["model"] = value["model"]
    for k in ("response_truncated",):
        if value.get(k) is None or type(value[k]) is bool:
            out[k] = value.get(k)
    for k in ("finish_reason", "error_code", "usage_source", "ledger_basis"):
        v = value.get(k)
        if v is None or type(v) is str and re.fullmatch(r"[a-zA-Z_]{1,64}", v):
            out[k] = v
    diagnostics = value.get("diagnostics", {})
    if type(diagnostics) is dict:
        out["provider_finish_reason"] = (
            diagnostics.get("provider_finish_reason")
            if diagnostics.get("provider_finish_reason") in {"STOP", "MAX_TOKENS", "stop", "length"}
            else None
        )
    answer = value.get("answer")
    if type(answer) is str:
        out.update(
            answer_characters=len(answer), answer_sha256=hashlib.sha256(answer.encode()).hexdigest()
        )
    attempts = value.get("attempts")
    if type(attempts) is list:
        assert len(attempts) <= 3
        out["attempts"] = [task(a) for a in attempts]
    return out


def inspect_key(key):
    out = {}
    for action in ("status", "queue-status"):
        transport, value = cli(action, key)
        out[action] = {**transport, **({"task": task(value)} if value else {})}
    if out["queue-status"].get("task", {}).get("state") == "completed":
        transport, value = cli("result", key)
        out["result"] = {**transport, **({"task": task(value)} if value else {})}
    return out


def snapshot():
    out = {"uid": os.getuid(), "keys": {}}
    t, catalog = cli("catalog")
    out["catalog_transport"] = t
    assert catalog is not None and len(catalog) == 7
    out["catalog"] = []
    for row in catalog:
        assert row["provider"] in MODELS and row["model"] == MODELS[row["provider"]]
        limits = {
            k: v
            for k, v in row["request_limits"].items()
            if v is None or type(v) in (int, float, bool)
        }
        out["catalog"].append(
            {
                "provider": row["provider"],
                "model": row["model"],
                "available": row["available"],
                "request_limits": limits,
            }
        )
    t, d = cli("diagnostics")
    out["diagnostics_transport"] = t
    assert d is not None
    out["diagnostics"] = {
        "ready_targets": d["ready_targets"],
        "queue_worker": {
            k: v for k, v in d["queue_worker"].items() if v is None or type(v) in (int, float, bool)
        },
    }
    t, recent = cli("recent")
    out["recent_transport"] = t
    assert recent is not None
    out["recent"] = {
        "count": len(recent["tasks"]),
        "has_more": recent.get("next_before") is not None,
        "sha256": hashlib.sha256(json.dumps(recent, sort_keys=True).encode()).hexdigest(),
        "states": {
            s: sum(x["state"] == s for x in recent["tasks"])
            for s in sorted(STATES)
            if any(x["state"] == s for x in recent["tasks"])
        },
    }
    t, usage = cli("usage")
    out["usage_transport"] = t
    assert usage is not None
    out["usage"] = []
    for row in usage:
        assert row["provider"] in MODELS and row["model"] == MODELS[row["provider"]]
        out["usage"].append(
            {
                k: v
                for k, v in row.items()
                if k in {"provider", "model"} or v is None or type(v) in (int, float, bool)
            }
        )
    for key in KEYS:
        out["keys"][key] = inspect_key(key)
    return out


def post(body):
    # Body is the separately sealed original public test, never an arbitrary user task.
    key = body["request_key"]
    assert key in KEYS
    assert (
        body["max_attempts"] == 1
        and body["max_output_tokens"] == 2048
        and body["wait_policy"] == "reject"
    )
    before = inspect_key(key)
    assert all(before[a]["transport"] == "not_found" for a in ("status", "queue-status"))
    state = Path("/home/morris/.local/state/aqb-normal-v1-acceptance")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = state.lstat()
    assert meta.st_uid == 1000 and meta.st_mode & 0o777 == 0o700
    claim = state / (key + ".claim.json")
    fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            json.dumps(
                {
                    "request_key": key,
                    "input_sha256": hashlib.sha256(
                        json.dumps(body, sort_keys=True).encode()
                    ).hexdigest(),
                    "dispatch_intent": True,
                    "max_provider_posts": 1,
                }
            ).encode()
        )
        f.flush()
        os.fsync(f.fileno())
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(directory)
    os.close(directory)
    t, value = cli("submit" if key == KEYS[0] else "run", body=body)
    out = {
        "request_key": key,
        "dispatch_intent": True,
        **t,
        **({"task": task(value)} if value else {}),
    }
    with (state / (key + ".result.json")).open("x") as f:
        json.dump(out, f)
        f.flush()
        os.fsync(f.fileno())
    return out
