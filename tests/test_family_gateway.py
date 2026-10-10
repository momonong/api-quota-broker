"""Typed contracts through the normal ledger and encrypted queue; no provider IO."""

import base64
import json
import sqlite3
from dataclasses import replace

import pytest
from test_gateway import HMAC_KEY, NOW, target

from quota_broker.config import Quota
from quota_broker.families import MediaLimits
from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
from quota_broker.gateway import Gateway, GatewayError, validate_task
from quota_broker.queue import DurableQueue
from quota_broker.registry import Registry, RegistryError


def catalog(
    provider="mistral",
    profile="mistral_embeddings",
    capability="embedding",
    model="fixture-embed",
    features=None,
    capabilities=None,
):
    adapter = PACKAGED_FAMILY_ADAPTERS[profile]
    endpoint = adapter.endpoints[provider]
    document = {
        "schema_version": 1,
        "providers": [
            {
                "id": provider,
                "adapter": profile,
                "origin": endpoint.split("/", 3)[0] + "//" + endpoint.split("/", 3)[2],
                "endpoint": endpoint,
            }
        ],
        "models": [
            {
                "provider": provider,
                "model": model,
                "capability": capability,
                "capabilities": capabilities or [capability],
                "context_tokens": 100000,
                "max_output_tokens": 10000,
                "features": features or [capability, "text"],
            }
        ],
    }
    return Registry.from_manifest(document)


def embedding_task(key="typed"):
    return {
        "request_key": key,
        "capability": "embedding",
        "input": {"texts": ["private fixture source", "second"]},
    }


def setup(tmp_path, *, response=None, quotas=None, limits=None):
    registry = catalog()
    item = replace(
        target("embed", "mistral", "fixture-embed", capability="embedding"),
        quotas=tuple(quotas or [Quota("requests", "requests", 10, "day")]),
        model_info=registry.resolve("fixture-embed", "mistral"),
    )
    calls, secrets = [], []

    def resolve(ref):
        secrets.append(ref)
        return "test-private-key"

    def transport(request, timeout, bound):
        calls.append(request)
        return (
            200,
            {},
            json.dumps(
                response
                or {
                    "data": [
                        {"index": 0, "embedding": [0.1, 0.2]},
                        {"index": 1, "embedding": [0.3, 0.4]},
                    ],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                }
            ).encode(),
        )

    gateway = Gateway(
        tmp_path / "typed.sqlite",
        (item,),
        HMAC_KEY,
        resolve,
        clock=lambda: NOW,
        registry=registry,
        family_transport=transport,
        media_limits=limits,
    )
    return gateway, calls, secrets


def test_embedding_normal_gateway_settles_requests_and_returns_vectors_once(tmp_path):
    gateway, calls, secrets = setup(tmp_path)
    task = embedding_task()
    assert gateway.validate_task(task)["max_output_tokens"] == 0
    assert gateway.explain(task)["selected_target_id"] == "embed"
    assert calls == secrets == []
    result = gateway.run(task)
    assert result["state"] == "completed" and result["ledger_state"] == "completed"
    assert result["result"] == {"vectors": [[0.1, 0.2], [0.3, 0.4]]}
    assert result["reported_resources"]["requests"] == 1
    assert len(calls) == len(secrets) == 1
    assert calls[0].payload["input"] == task["input"]["texts"]
    repeat = gateway.run(task)
    assert "result" not in repeat and len(calls) == 1
    assert gateway.db and b"private fixture source" not in (tmp_path / "typed.sqlite").read_bytes()
    assert b"test-private-key" not in (tmp_path / "typed.sqlite").read_bytes()


def test_queue_vectors_encrypted_and_authenticated_result(tmp_path):
    gateway, calls, _secrets = setup(tmp_path)
    (tmp_path / "typed.sqlite").chmod(0o600)
    queue = DurableQueue(gateway, b"Q" * 32)
    queue.submit(embedding_task())
    done = queue.tick("fixture-worker")
    assert done["state"] == "completed" and "result" not in done
    assert queue.result("typed")["result"]["vectors"] == [[0.1, 0.2], [0.3, 0.4]]
    assert len(calls) == 1 and queue.tick("fixture-worker") is None
    stored = (tmp_path / "typed.sqlite").read_bytes()
    assert b"private fixture source" not in stored and b'"vectors"' not in stored


def test_batch_count_mismatch_is_unknown_and_never_replayed(tmp_path):
    gateway, calls, _ = setup(tmp_path, response={"data": [{"index": 0, "embedding": [1.0]}]})
    result = gateway.run(embedding_task())
    assert result["state"] == result["ledger_state"] == "unknown"
    assert "result" not in result
    gateway.run(embedding_task())
    assert len(calls) == 1


def test_missing_required_actual_usage_holds_reservation_after_valid_response(tmp_path):
    gateway, calls, _ = setup(
        tmp_path,
        response={"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0]}]},
        quotas=[Quota("tokens", "input_tokens", 10000, "day")],
    )
    result = gateway.run(embedding_task())
    assert result["state"] == "completed_usage_unknown" and result["ledger_state"] == "unknown"
    assert result["result"]["vectors"] == [[1.0], [2.0]]
    assert result["attempts"][0]["execution_finished_at"]
    assert len(calls) == 1


def test_input_and_options_admission_before_key_or_reservation(tmp_path):
    gateway, calls, secrets = setup(tmp_path)
    with pytest.raises(GatewayError):
        gateway.run({**embedding_task(), "options": {"stream": True}})
    assert calls == secrets == []
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_typed_request_bound_before_dispatch(tmp_path):
    gateway, calls, _ = setup(tmp_path, limits=MediaLimits(request_bytes=50))
    with pytest.raises(GatewayError):
        gateway.run(embedding_task())
    assert calls == []


def test_fixed_family_profile_rejects_manifest_endpoint_override():
    with pytest.raises(RegistryError):
        Registry.from_manifest(
            {
                "schema_version": 1,
                "providers": [
                    {
                        "id": "mistral",
                        "adapter": "mistral_embeddings",
                        "origin": "https://api.mistral.ai",
                        "endpoint": "https://api.mistral.ai/admin",
                    }
                ],
                "models": [],
            }
        )


def test_multifamily_registered_same_model_and_model_specific_profile():
    registry = catalog(
        "google",
        "gemini_inference",
        "text_generation",
        "fixture-gemini",
        features=["text_generation", "vision", "text"],
        capabilities=["text_generation", "vision"],
    )
    spec = registry.resolve("fixture-gemini", "google")
    assert registry.supports_family(spec, "text_generation") and registry.supports_family(
        spec, "vision"
    )
    assert not registry.supports_family(spec, "tts")


def test_legacy_normalized_fingerprint_fields_stay_unchanged():
    legacy = validate_task(
        {
            "request_key": "legacy",
            "capability": "text_generation",
            "input": "hello",
            "max_output_tokens": 42,
        }
    )
    assert set(legacy) == {
        "request_key",
        "capability",
        "input",
        "max_output_tokens",
        "provider",
        "model",
        "source_language",
        "target_language",
        "neuron_bound",
    }


from test_family_adapters import CASES, specification


@pytest.mark.parametrize("profile_name,provider,capability,content,options,body", CASES)
def test_seven_provider_families_use_normal_gateway_ledger(
    tmp_path, profile_name, provider, capability, content, options, body
):
    spec = specification(profile_name, provider, capability)
    spec = replace(
        spec,
        endpoint_template=PACKAGED_FAMILY_ADAPTERS[profile_name].endpoint_for(
            spec, capability, "{account_id}"
        ),
    )
    models = dict(Registry.builtin().models)
    models[(provider, spec.model)] = spec
    registry = Registry(models)
    item = replace(
        target("family", provider, spec.model, capability),
        model_info=spec,
        account_id="a" * 32,
        quotas=(Quota("requests", "requests", 100, "day"),),
    )
    calls = []

    def transport(request, timeout, bound):
        calls.append(request)
        return 200, {}, body if isinstance(body, bytes) else json.dumps(body).encode()

    gateway = Gateway(
        tmp_path / "family.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-private-key",
        clock=lambda: NOW,
        registry=registry,
        family_transport=transport,
    )
    task = {
        "request_key": "family-task",
        "capability": capability,
        "input": content,
        "options": options,
        "max_output_tokens": 100 if gateway.family_registry.uses_output_tokens(capability) else 0,
    }
    result = gateway.run(task)
    assert result["state"] == "completed", result
    assert result["ledger_state"] == "completed" and isinstance(result["result"], dict)
    assert len(calls) == 1
    assert len(gateway.usage(provider=provider)) == 1
    assert "result" not in gateway.status("family-task")
    gateway.run(task)
    assert len(calls) == 1


def test_typed_metadata_reflected_input_is_scrubbed(tmp_path):
    gateway, _, _ = setup(
        tmp_path,
        response={
            "id": "private_fixture_source",
            "data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0]}],
        },
    )
    task = embedding_task()
    task["input"]["texts"][0] = "private_fixture_source"
    result = gateway.run(task)
    assert result["provider_request_id"] is None
    assert "private_fixture_source" not in json.dumps(gateway.status("typed"))
    assert b"private_fixture_source" not in (tmp_path / "typed.sqlite").read_bytes()


def test_json_escaped_key_reflection_is_never_returned(tmp_path):
    spec = specification("openai_inference", "mistral", "text_generation")
    models = dict(Registry.builtin().models)
    models[(spec.provider, spec.model)] = spec
    key = 'fixture-key"marker'
    item = replace(target("chat", "mistral", spec.model), model_info=spec)
    response = {
        "choices": [{"message": {"role": "assistant", "content": key}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    gateway = Gateway(
        tmp_path / "reflected.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: key,
        clock=lambda: NOW,
        registry=Registry(models),
        family_transport=lambda *_: (200, {}, json.dumps(response).encode()),
    )
    result = gateway.run(
        {
            "request_key": "reflect",
            "max_output_tokens": 16,
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": "hello"}]},
        }
    )
    assert result["state"] == "unknown" and "result" not in result
    assert "fixture-key" not in json.dumps(result)


def test_manifest_one_model_can_bind_multiple_fixed_protocols():
    document = {
        "schema_version": 1,
        "providers": [
            {
                "id": "mistral",
                "adapter": "openai_inference",
                "origin": "https://api.mistral.ai",
                "endpoint": "https://api.mistral.ai/v1/chat/completions",
            }
        ],
        "models": [
            {
                "provider": "mistral",
                "model": "fixture-multi",
                "capability": "text_generation",
                "capabilities": ["text_generation", "code_completion"],
                "family_adapters": {"code_completion": "mistral_fim"},
                "context_tokens": 10000,
                "max_output_tokens": 1000,
                "features": ["text", "text_generation", "code_completion"],
            }
        ],
    }
    registry = Registry.from_manifest(document)
    spec = registry.resolve("fixture-multi", "mistral")
    assert registry.supports_family(spec, "code_completion")
    request = registry.request_task(
        spec,
        "",
        "fixture-secret",
        {
            "capability": "code_completion",
            "input": {"messages": [{"role": "user", "content": "prefix"}]},
            "options": {"suffix": "tail"},
            "max_output_tokens": 50,
        },
    )
    assert request.url == "https://api.mistral.ai/v1/fim/completions"


def test_non_token_catalog_has_explicit_not_applicable_zero_limits():
    registry = catalog("mistral", "mistral_embeddings", "embedding", "fixture-nontoken")
    spec = registry.resolve("fixture-nontoken", "mistral")
    row = {
        "provider": "mistral",
        "model": "new-embedding",
        "capability": "embedding",
        "protocol": "mistral_embeddings",
        "endpoint": spec.endpoint_template,
        "context_tokens": None,
        "max_output_tokens": None,
        "features": ["embedding", "text"],
    }
    declaration, proposal = registry.candidate_definition(row)
    assert proposal["context_tokens"] == proposal["max_output_tokens"] == 0
    new = Registry.from_manifest(
        {"schema_version": 1, "providers": [declaration], "models": [proposal]}
    )
    assert new.resolve("new-embedding", "mistral").max_output_tokens == 0
    row.update(
        capability="text_generation",
        protocol="openai_inference",
        endpoint="https://api.mistral.ai/v1/chat/completions",
    )
    with pytest.raises(RegistryError, match="unknown"):
        registry.candidate_definition(row)


def test_invalid_credential_fails_before_dispatch(tmp_path):
    gateway, calls, _ = setup(tmp_path)
    gateway.secret_resolver = lambda _: "invalid\x00key"
    with pytest.raises(GatewayError):
        gateway.run(embedding_task())
    assert calls == []
    with sqlite3.connect(gateway.db) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM reservations WHERE dispatched_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_explicit_builtin_extension_is_scoped_and_not_automatic():
    spec = Registry.builtin().resolve("ocr.space/engine2", "ocrspace")
    document = {
        "schema_version": 1,
        "providers": [
            {
                "id": "ocrspace",
                "adapter": "ocrspace_inference",
                "origin": spec.origin,
                "endpoint": spec.endpoint_template,
            }
        ],
        "models": [
            {
                "provider": "ocrspace",
                "model": spec.model,
                "capability": "ocr",
                "context_tokens": 0,
                "max_output_tokens": 0,
                "features": ["ocr", "text", "vision", "document_input"],
            }
        ],
    }
    with pytest.raises(RegistryError, match="duplicate"):
        Registry.from_manifest(document)
    document["models"][0]["replace_builtin"] = True
    extended = Registry.from_manifest(document)
    assert extended.resolve(spec.model, "ocrspace").adapter == "ocrspace_inference"
    assert Registry.builtin().resolve(spec.model, "ocrspace") == spec


def test_typed_resource_estimate_can_supply_neurons_without_fake_token_value(tmp_path):
    from datetime import timedelta

    from quota_broker.config import ResourceEstimate

    spec = specification("cloudflare_tts", "cloudflare", "tts")
    spec = replace(
        spec,
        endpoint_template=PACKAGED_FAMILY_ADAPTERS[spec.adapter].endpoint_for(
            spec, "tts", "{account_id}"
        ),
    )
    models = dict(Registry.builtin().models)
    models[(spec.provider, spec.model)] = spec
    item = replace(
        target("tts", "cloudflare", spec.model, "tts"),
        model_info=spec,
        account_id="a" * 32,
        quotas=(Quota("neurons", "neurons", 100, "day"),),
        resource_estimates=(
            ResourceEstimate(
                "neurons",
                40,
                1000,
                0,
                "trusted_operator",
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=1),
            ),
        ),
    )
    gateway = Gateway(
        tmp_path / "tts-resource.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-private-key",
        clock=lambda: NOW,
        registry=Registry(models),
        family_transport=lambda *_: (200, {}, b"ID3" + b"\0" * 20),
    )
    task = {
        "request_key": "tts",
        "capability": "tts",
        "input": {"text": "hello"},
        "options": {"format": "mp3"},
    }
    assert gateway.explain(task)["selected_target_id"] == "tts"
    result = gateway.run(task)
    assert result["state"] == "completed_usage_unknown" and result["ledger_state"] == "unknown"
    assert result["ledger_charges"] == [{"bucket": "neurons", "metric": "neurons", "amount": 40}]


def test_inline_binary_key_reflection_is_never_returned(tmp_path):
    from test_family_adapters import WAV

    spec = specification("mistral_tts", "mistral", "tts")
    models = dict(Registry.builtin().models)
    models[(spec.provider, spec.model)] = spec
    secret = "fixture-hidden-binary-key"
    item = replace(
        target("tts", "mistral", spec.model, "tts"),
        model_info=spec,
        quotas=(Quota("request", "requests", 10, "day"),),
    )
    raw = json.dumps({"audio_data": base64.b64encode(WAV + secret.encode()).decode()}).encode()
    gateway = Gateway(
        tmp_path / "binary-reflect.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: secret,
        clock=lambda: NOW,
        registry=Registry(models),
        family_transport=lambda *_: (200, {}, raw),
    )
    result = gateway.run(
        {"request_key": "binary-reflect", "capability": "tts", "input": {"text": "hello"}}
    )
    assert result["state"] == "unknown" and "result" not in result
    assert secret not in json.dumps(result)


def test_candidates_merge_same_model_families_without_copying_account_bindings():
    from quota_broker.discovery_sources import candidate_bundle

    common = {
        "provider": "mistral",
        "model": "fixture-candidate",
        "status": "listed",
        "free_eligibility": "unknown",
        "account_availability": "unknown",
        "adapter_support": "compatible_unregistered",
        "context_tokens": 10000,
        "max_output_tokens": 1000,
        "features": ["text"],
        "configured_target_ids": [],
    }
    rows = [
        {
            **common,
            "capability": "embedding",
            "protocol": "mistral_embeddings",
            "endpoint": "https://api.mistral.ai/v1/embeddings",
            "context_tokens": None,
            "max_output_tokens": None,
        },
        {
            **common,
            "capability": "text_generation",
            "protocol": "openai_inference",
            "endpoint": "https://api.mistral.ai/v1/chat/completions",
        },
    ]
    bundle = candidate_bundle(rows, Registry.builtin())
    assert len(bundle["registry_manifest"]["models"]) == 1
    registry = Registry.from_manifest(bundle["registry_manifest"])
    spec = registry.resolve(common["model"], "mistral")
    assert spec.capabilities == ("embedding", "text_generation")
    assert spec.max_output_tokens == 1000 and spec.context_tokens == 10000
    assert all(
        row["disabled"] and not row["activation_allowed"] for row in bundle["target_candidates"]
    )
    assert "secret_ref" not in json.dumps(bundle["registry_manifest"])


def test_google_signed_tool_continuation_is_scoped_and_encrypted(tmp_path):
    spec = specification("gemini_inference", "google", "text_generation")
    spec = replace(
        spec,
        endpoint_template=PACKAGED_FAMILY_ADAPTERS[spec.adapter].endpoint_for(
            spec, spec.capability, "{account_id}"
        ),
    )
    models = dict(Registry.builtin().models)
    models[(spec.provider, spec.model)] = spec
    item = replace(
        target("google", "google", spec.model),
        model_info=spec,
        quotas=(Quota("requests", "requests", 10, "day"),),
    )
    parts = [
        {"text": "private thought text", "thought": True},
        {"text": "checking"},
        {"functionCall": {"name": "lookup"}, "thoughtSignature": "opaque_signature_for_fixture"},
    ]
    response = {
        "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }
    calls = []

    def transport(request, *args):
        calls.append(request)
        return 200, {}, json.dumps(response).encode()

    gateway = Gateway(
        tmp_path / "google-state.sqlite",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-private-key",
        clock=lambda: NOW,
        registry=Registry(models),
        family_transport=transport,
    )
    first = {
        "request_key": "first",
        "capability": "text_generation",
        "input": {"messages": [{"role": "user", "content": "hello"}]},
        "provider": "google",
        "model": spec.model,
        "max_output_tokens": 16,
    }
    result = gateway.run(first)
    assert result["state"] == "completed", result
    message = result["result"]["messages"][0]
    assert message["provider_state"]["parts"] == parts
    tool_id = message["tool_calls"][0]["id"]
    continuation = {
        **first,
        "request_key": "second",
        "input": {
            "messages": [
                first["input"]["messages"][0],
                message,
                {"role": "tool", "content": '{"found":true}', "tool_call_id": tool_id},
            ]
        },
    }
    prepared = gateway.validate_task(continuation)
    assert "google_continuation" in prepared["requirements"]["features"]
    for changes in (
        {"provider": None},
        {"model": None},
        {"model": "gemini-3.5-flash-lite"},
        {"provider": "mistral"},
    ):
        with pytest.raises(GatewayError):
            gateway.validate_task({**continuation, **changes})
    result2 = gateway.run(continuation)
    assert result2["state"] == "completed", result2
    assert calls[1].payload["contents"][1]["parts"] == parts
    assert calls[1].payload["contents"][2]["parts"][0]["functionResponse"]["name"] == "lookup"
    assert "provider_state" not in json.dumps(gateway.status("second"))
    (tmp_path / "google-state.sqlite").chmod(0o600)
    queue = DurableQueue(gateway, b"Q" * 32)
    queue.submit({**continuation, "request_key": "queued"})
    assert queue.tick("worker")["state"] == "completed"
    assert queue.result("queued")["result"]["messages"][0]["provider_state"]["parts"] == parts
    stored = (tmp_path / "google-state.sqlite").read_bytes()
    assert b"private thought text" not in stored and b"opaque_signature_for_fixture" not in stored
