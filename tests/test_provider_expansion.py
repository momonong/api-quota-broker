"""Shipped provider manifests through the existing adapter and Gateway; offline only."""

import json
import socket
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_framework import QUEUE_KEY, TOKEN, close, invoke, serve
from test_gateway import HMAC_KEY, NOW, task

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.queue import DurableQueue
from quota_broker.registry import Registry, RegistryError

ROOT = Path(__file__).parents[1]
PROVIDERS = ("sambanova", "huggingface", "cerebras")
EXPECTED = {
    "sambanova": (
        "https://api.sambanova.ai/v1/chat/completions",
        "Meta-Llama-3.3-70B-Instruct",
    ),
    "huggingface": (
        "https://router.huggingface.co/v1/chat/completions",
        "openai/gpt-oss-120b:cerebras",
    ),
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "gpt-oss-120b"),
}


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """An accidentally unmocked provider or credential request must fail locally."""
    connect, resolve = socket.socket.connect, socket.getaddrinfo

    def checked_connect(sock, address):
        assert address[0] in {"127.0.0.1", "::1"}, "external connection in offline test"
        return connect(sock, address)

    def checked_resolve(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", "localhost"}, "external DNS in offline test"
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket, "getaddrinfo", checked_resolve)


def build(tmp_path, monkeypatch, *, enabled=True, outcome="success"):
    registry = Registry.load(ROOT / "registry.expansion.example.json")
    targets = load_gateway_config(ROOT / "gateway.expansion.example.json", registry=registry)
    if enabled:
        targets = tuple(
            replace(
                target,
                enabled=True,
                free_eligible=True,
                account_id="fixture-account",
                source="offline fixture only",
                verified_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(hours=1),
                quotas=tuple(replace(cap, limit=100000) for cap in target.quotas),
            )
            for target in targets
        )
    calls, secrets = [], []

    def secret(ref):
        secrets.append(ref)
        return "fixture-provider-secret"

    def http(spec, url, headers, payload, timeout):
        calls.append((spec.provider, url, payload))
        assert headers == {"Authorization": "Bearer fixture-provider-secret"}
        assert 0 < timeout <= 30
        assert (url, payload["model"]) == EXPECTED[spec.provider]
        assert payload["stream"] is False
        assert set(payload) == {"model", "messages", "max_tokens", "stream"}
        if outcome == "timeout":
            raise TimeoutError()
        if outcome in {"429", "503"}:
            return int(outcome), {"Retry-After": "1"}, b'{"error":{"code":"rate_limit_exceeded"}}'
        response = {
            "id": "chatcmpl-fixture",
            "choices": [
                {
                    "message": {"content": "fixture answer"},
                    "finish_reason": "length" if outcome == "truncated" else "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }
        if outcome == "missing_usage":
            response.pop("usage")
        elif outcome == "inconsistent_usage":
            response["usage"]["total_tokens"] = 999
        return 200, {}, json.dumps(response).encode()

    # Exercise OpenAIChatAdapter.request/transport/interpret, replacing only its HTTP I/O.
    monkeypatch.setattr("quota_broker.registry._json_http", http)
    tmp_path.chmod(0o700)
    db = tmp_path / "expansion.sqlite"
    db.touch(mode=0o600)
    gateway = Gateway(db, targets, HMAC_KEY, secret, clock=lambda: NOW, registry=registry)
    return gateway, calls, secrets


@pytest.mark.parametrize("provider", PROVIDERS)
def test_shipped_targets_fail_closed_before_credentials(tmp_path, monkeypatch, provider):
    gateway, calls, secrets = build(tmp_path, monkeypatch, enabled=False)
    candidate = next(t for t in gateway.targets if t.provider == provider)
    assert not candidate.enabled and not candidate.free_eligible
    assert candidate.verified_at is candidate.expires_at is None
    assert candidate.capacity.kind == "unknown" and candidate.provider_quota_facts == ()
    request = task(provider=provider, model=candidate.model)
    assert gateway.explain(request)["selected_target_id"] is None
    with pytest.raises(GatewayError, match="no eligible route"):
        gateway.run(request)
    assert calls == secrets == []
    assert provider not in Registry.builtin().providers


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_sync_cli_queue_and_restart(tmp_path, monkeypatch, capsys, provider):
    gateway, calls, _ = build(tmp_path, monkeypatch)
    queue = DurableQueue(gateway, QUEUE_KEY)
    server, thread, base = serve(gateway, queue)
    token_path = tmp_path / "client.token"
    token_path.write_text(TOKEN)
    try:
        arguments = (
            "--capability",
            "text_generation",
            "--provider",
            provider,
            "--max-output-tokens",
            "16",
        )
        result = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "run",
            "--request-key",
            "sync",
            *arguments,
            content="private fixture input",
        )
        assert result["state"] == "completed"
        assert result["answer"] == "fixture answer"
        assert result["finish_reason"] == "stop" and result["response_truncated"] is False
        assert result["ledger_basis"] == "settled_provider_usage"
        assert result["reported_input_tokens"] == 11 and result["reported_output_tokens"] == 3
        replay = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "run",
            "--request-key",
            "sync",
            *arguments,
            content="private fixture input",
        )
        assert "answer" not in replay and len(calls) == 1
        job = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "submit",
            "--request-key",
            "queued",
            *arguments,
            content="private queued fixture",
        )
        assert job["state"] == "queued"
        assert (
            invoke(monkeypatch, capsys, base, token_path, "worker", "--once")["state"]
            == "completed"
        )
        assert (
            invoke(monkeypatch, capsys, base, token_path, "result", "queued")["answer"]
            == "fixture answer"
        )
        assert len(calls) == 2
    finally:
        close(server, thread)
    restarted = Gateway(
        gateway.db,
        gateway.targets,
        HMAC_KEY,
        gateway.secret_resolver,
        clock=lambda: NOW,
        registry=gateway.registry,
    )
    assert restarted.status("sync")["state"] == "completed"
    assert DurableQueue(restarted, QUEUE_KEY).result("queued")["answer"] == "fixture answer"
    assert len(calls) == 2
    for private in (
        b"private fixture input",
        b"private queued fixture",
        b"fixture answer",
        b"fixture-provider-secret",
        QUEUE_KEY,
    ):
        assert private not in Path(gateway.db).read_bytes()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("outcome", ["missing_usage", "inconsistent_usage"])
def test_missing_usage_retains_hold(tmp_path, monkeypatch, provider, outcome):
    gateway, calls, _ = build(tmp_path, monkeypatch, outcome=outcome)
    request = task(provider=provider)
    result = gateway.run(request)
    assert result["state"] == "completed_usage_unknown"
    assert result["answer"] == "fixture answer" and result["ledger_basis"] == "held_estimate"
    assert result["reported_input_tokens"] is result["reported_output_tokens"] is None
    assert gateway.run(request)["state"] == "completed_usage_unknown" and len(calls) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("outcome", ["429", "503", "timeout"])
def test_unknown_does_not_reroute_or_replay_after_restart(tmp_path, monkeypatch, provider, outcome):
    gateway, calls, _ = build(tmp_path, monkeypatch, outcome=outcome)
    # Leave all three routes eligible, with this provider first, to detect unsafe fallback.
    gateway.targets = tuple(
        replace(t, priority=0 if t.provider == provider else 1) for t in gateway.targets
    )
    gateway.broker.targets = gateway.targets
    request = {**task(), "max_attempts": 3}
    result = gateway.run(request)
    assert result["provider"] == provider and result["state"] == "unknown"
    assert result["ledger_basis"] == "held_estimate" and len(calls) == 1
    restarted = Gateway(
        gateway.db,
        gateway.targets,
        HMAC_KEY,
        gateway.secret_resolver,
        clock=lambda: NOW,
        registry=gateway.registry,
    )
    assert restarted.run(request)["state"] == "unknown" and len(calls) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_truncation_and_fixed_route_contract(tmp_path, monkeypatch, provider):
    gateway, calls, _ = build(tmp_path, monkeypatch, outcome="truncated")
    result = gateway.run(task(provider=provider))
    assert result["response_truncated"] is True and result["finish_reason"] == "length"
    spec = gateway.registry.resolve(EXPECTED[provider][1], provider)
    url, headers, payload = gateway.registry.request(
        spec, "fixture", "fixture-secret", "hi", 16, None, None
    )
    with pytest.raises(RegistryError, match="unapproved"):
        gateway.registry.transport(
            spec, "https://evil.example/v1/chat/completions", headers, payload, 1
        )
    with pytest.raises(RegistryError):
        gateway.registry.transport(spec, url, headers, {**payload, "model": "unapproved:auto"}, 1)
    with pytest.raises(GatewayError):
        gateway.validate_task(
            {**task("different", provider=provider), "endpoint": "https://evil.example"}
        )
    assert len(calls) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("change", ["unverified", "expired", "paid"])
def test_account_gate_independent_of_protocol_support(tmp_path, monkeypatch, provider, change):
    gateway, calls, secrets = build(tmp_path, monkeypatch)
    changes = {
        "unverified": {"free_eligible": False},
        "expired": {"expires_at": NOW},
        "paid": {"billing_enabled": True},
    }[change]
    gateway.targets = tuple(
        replace(t, **changes) if t.provider == provider else t for t in gateway.targets
    )
    gateway.broker.targets = gateway.targets
    assert gateway.explain(task(provider=provider))["selected_target_id"] is None
    with pytest.raises(GatewayError, match="no eligible route"):
        gateway.run(task(provider=provider))
    assert calls == secrets == []
