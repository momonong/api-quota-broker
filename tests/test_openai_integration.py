import json
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, NOW, task

from quota_broker.config import load_gateway_config
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.registry import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-5.4-mini"
ENDPOINT = "https://api.openai.com/v1/chat/completions"


def manifest(origin="https://api.openai.com", path="/v1/chat/completions"):
    return {
        "schema_version": 1,
        "providers": [
            {"id": "openai", "adapter": "openai_chat", "origin": origin, "endpoint": origin + path}
        ],
        "models": [
            {
                "provider": "openai",
                "model": MODEL,
                "capability": "text_generation",
                "context_tokens": 400_000,
                "max_output_tokens": 128_000,
            }
        ],
    }


@pytest.mark.parametrize(
    ("origin", "path"),
    [
        ("https://relay.example", "/v1/chat/completions"),
        ("https://api.openai.com", "/v1/responses"),
    ],
)
def test_openai_credential_destination_is_fixed(origin, path):
    with pytest.raises(RegistryError, match="OpenAI.*endpoint"):
        Registry.from_manifest(manifest(origin, path))


def test_openai_chat_payload_and_usage(monkeypatch):
    registry = Registry.from_manifest(manifest())
    spec = registry.resolve(MODEL, "openai")
    url, headers, payload = registry.request(
        spec, "fixture", "fixture-secret", "Reply OK.", 64, None, None
    )
    assert url == ENDPOINT
    assert payload == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply OK."}],
        "max_completion_tokens": 64,
        "stream": False,
        "store": False,
    }

    def fixture_http(spec, url, headers, payload, timeout):
        assert url == ENDPOINT and headers == {"Authorization": "Bearer fixture-secret"}
        response = {
            "id": "chatcmpl-fixture",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }
        return 200, {}, json.dumps(response).encode()

    monkeypatch.setattr("quota_broker.registry._json_http", fixture_http)
    status, _, raw = registry.transport(spec, url, headers, payload, 5)
    assert registry.interpret(spec, status, raw) == ("OK", 4, 3, None, "chatcmpl-fixture")
    with pytest.raises(RegistryError, match="unsupported adapter transport"):
        registry.transport(spec, url, headers, {**payload, "store": True}, 5)


def test_shipped_openai_route_stops_before_resolving_key(tmp_path):
    registry = Registry.load(ROOT / "registry.openai.example.json")
    targets = load_gateway_config(ROOT / "gateway.openai.example.json", registry=registry)
    (target,) = targets
    assert (target.provider, target.model, target.secret_ref) == ("openai", MODEL, "OPENAI_API_KEY")
    assert target.max_output_tokens == 64
    assert not target.enabled and not target.free_eligible and target.billing_enabled
    assert target.provider_quota_facts == ()

    def unexpected_secret(ref):
        pytest.fail(f"disabled route resolved {ref}")

    db = tmp_path / "gateway.sqlite"
    db.touch(mode=0o600)
    gateway = Gateway(
        db, targets, HMAC_KEY, unexpected_secret, clock=lambda: NOW, registry=registry
    )
    request = task(provider="openai", model=MODEL)
    assert gateway.explain(request)["selected_target_id"] is None
    with pytest.raises(GatewayError, match="no eligible route"):
        gateway.run(request)
