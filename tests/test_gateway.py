import base64
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quota_broker.cli import gateway_cli
from quota_broker.config import Capacity, Evidence, Quota, QuotaFact, Target
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_providers import explicit_quota_rejection, official_request, provider_http
from quota_broker.gateway_server import make_gateway_server

NOW = datetime(2026, 9, 30, 1, tzinfo=UTC)
HMAC_KEY = b"fixture-persisted-hmac-key-at-least-32-bytes"


def target(
    name,
    provider,
    model,
    capability="text_generation",
    *,
    priority=0,
    scope=None,
    capacity=None,
    rpm=10,
):
    quotas = [
        Quota(name + "-rpm", "requests", rpm, "rolling_minute"),
        Quota(name + "-rpd", "requests", 100, "day"),
    ]
    if provider == "cloudflare":
        quotas.append(Quota(name + "-neurons", "neurons", 1000, "day"))
    elif provider != "ocrspace":
        quotas.append(Quota(name + "-tpm", "input_tokens", 10000, "rolling_minute"))
    return Target(
        id=name,
        provider=provider,
        model=model,
        account_id="fixture-account",
        enabled=True,
        free_eligible=True,
        billing_enabled=False,
        verified_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        quotas=tuple(quotas),
        concurrency_limit=3,
        max_output_tokens=1 if provider == "ocrspace" else 256,
        priority=priority,
        source="fixture account evidence",
        secret_ref=name.upper(),
        shared_concurrency_scope=scope,
        shared_concurrency_limit=1 if scope else None,
        capacity=capacity or Capacity(),
    )


def task(
    key="fixture-task",
    *,
    capability="text_generation",
    text="fixture input",
    provider=None,
    model=None,
    neurons=None,
):
    return {
        "request_key": key,
        "capability": capability,
        "input": text,
        "max_output_tokens": 16,
        "provider": provider,
        "model": model,
        "source_language": "en" if capability == "translation" else None,
        "target_language": "zh-cn" if capability == "translation" else None,
        "neuron_bound": neurons,
    }


def fixture_transport(calls, *, status=200, missing_usage=False):
    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        if any(
            host in url
            for host in (
                "integrate.api.nvidia.com",
                "api.groq.com",
                "api.mistral.ai",
                "openrouter.ai",
            )
        ):
            response = {
                "choices": [{"message": {"content": "fixture answer"}}],
                "usage": {} if missing_usage else {"prompt_tokens": 11, "completion_tokens": 3},
                "id": "fixture-id",
            }
        elif "generativelanguage.googleapis.com" in url:
            response = {
                "candidates": [{"content": {"parts": [{"text": "fixture answer"}]}}],
                "usageMetadata": {}
                if missing_usage
                else {"promptTokenCount": 11, "candidatesTokenCount": 3},
                "responseId": "fixture-id",
            }
        elif "ocr.space" in url:
            response = {
                "OCRExitCode": 1,
                "IsErroredOnProcessing": False,
                "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "fixture OCR"}],
            }
        else:
            response = {
                "result": {"response": "fixture answer"},
                "usage": {}
                if missing_usage
                else {"prompt_tokens": 11, "completion_tokens": 3, "neurons": 7},
                "request_id": "fixture-id",
            }
        return status, {"Retry-After": "60"} if status == 429 else {}, json.dumps(response).encode()

    return transport


def make_gateway(tmp_path, targets, calls, **kwargs):
    return Gateway(
        tmp_path / "gateway.db",
        tuple(targets),
        HMAC_KEY,
        lambda ref: "fixture-secret-value" if ref else "",
        fixture_transport(calls, **kwargs),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "provider,model,neurons",
    [
        ("nvidia", "google/gemma-4-31b-it", None),
        ("google", "gemini-2.5-flash-lite", None),
        ("cloudflare", "@cf/meta/llama-3.2-1b-instruct", 30),
        ("groq", "openai/gpt-oss-20b", None),
        ("mistral", "mistral-small-latest", None),
        ("openrouter", "liquid/lfm-2.5-2.6b:free", None),
    ],
)
def test_all_provider_execution_usage_and_secret_free_db(tmp_path, provider, model, neurons):
    calls = []
    gateway = make_gateway(tmp_path, [target(provider, provider, model)], calls)
    original = task(provider=provider, model=model, neurons=neurons)
    result = gateway.run(original)
    assert result["state"] == "completed"
    assert result["answer"] == "fixture answer"
    assert result["provider"] == provider and result["model"] == model
    assert result["ledger_basis"] == "settled_provider_usage"
    assert result["estimated_input_tokens"] > result["reported_input_tokens"]
    assert len(calls) == 1
    assert gateway.run(original)["state"] == "completed"
    assert "answer" not in gateway.status(original["request_key"])
    assert len(calls) == 1
    with pytest.raises(GatewayError) as exc:
        gateway.run({**original, "input": "different text"})
    assert exc.value.code == "conflict"
    restarted = Gateway(
        tmp_path / "gateway.db",
        (target(provider, provider, model),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    assert restarted.run(original)["state"] == "completed"
    assert len(calls) == 1
    usage = gateway.usage(provider=provider, model=model)
    assert usage[0]["requests"] == 1
    assert usage[0]["reported_input_tokens"] == 11
    assert usage[0]["reported_output_tokens"] == 3
    assert usage[0]["estimated_input_tokens"] > 11
    assert usage[0]["ledger_held_count"] == 0
    if provider == "cloudflare":
        assert usage[0]["ledger_neurons"] == 7
    else:
        assert usage[0]["ledger_input_tokens"] == 11
    assert usage[0]["input_unknown_count"] == 0
    db_bytes = (tmp_path / "gateway.db").read_bytes()
    for forbidden in (b"fixture input", b"fixture answer", b"fixture-secret-value"):
        assert forbidden not in db_bytes


def test_ocr_image_is_ephemeral_and_request_usage_is_counted(tmp_path):
    png = base64.b64encode(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l6cAAAAASUVORK5CYII="
        )
    ).decode()
    calls = []
    gateway = make_gateway(tmp_path, [target("ocr", "ocrspace", "ocr.space/engine2")], calls)
    request = {
        "request_key": "ocr-once",
        "capability": "ocr",
        "input": png,
        "max_output_tokens": 1,
        "provider": "ocrspace",
    }
    result = gateway.run(request)
    assert result["state"] == "completed" and result["answer"] == "fixture OCR"
    assert result["attempts"][0]["input_bytes"] == len(base64.b64decode(png))
    assert result["attempts"][0]["estimated_input_tokens"] is None
    assert calls[0][0] == "https://api.ocr.space/parse/image"
    assert calls[0][2]["OCREngine"] == "2"
    assert len(calls) == 1 and gateway.run(request)["state"] == "completed"
    usage = gateway.usage(provider="ocrspace")[0]
    assert usage["requests"] == 1 and usage["input_unknown_count"] is None
    assert usage["input_bytes"] == len(base64.b64decode(png))
    db_bytes = (tmp_path / "gateway.db").read_bytes()
    for forbidden in (png.encode(), b"fixture OCR", b"fixture-secret-value"):
        assert forbidden not in db_bytes


def test_cloudflare_account_id_resolves_only_in_memory(tmp_path):
    calls = []
    configured = replace(
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
        account_id="from-doppler",
        account_id_ref="CLOUDFLARE_ACCOUNT_ID",
    )

    def resolve(ref):
        return (
            "actual-account-identifier"
            if ref == "CLOUDFLARE_ACCOUNT_ID"
            else "fixture-secret-value"
        )

    gateway = Gateway(
        tmp_path / "gateway.db",
        (configured,),
        HMAC_KEY,
        resolve,
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    result = gateway.run(task("account-id", neurons=30))
    assert result["state"] == "completed"
    assert "/accounts/actual-account-identifier/" in calls[0][0]
    assert b"actual-account-identifier" not in (tmp_path / "gateway.db").read_bytes()


def test_ocr_transport_uses_fixed_form_endpoint_and_header_key(monkeypatch):
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    url, headers, payload = official_request(
        "ocrspace", "ocr.space/engine2", "fixture", "fixture-secret-value", png, 1, None, None
    )
    sent = []

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return b'{"OCRExitCode":1}'

    class FakeOpener:
        def open(self, request, timeout):
            sent.append(request)
            return FakeResponse()

    monkeypatch.setattr(
        "quota_broker.gateway_providers.urllib.request.build_opener", lambda *_: FakeOpener()
    )
    status, _, _ = provider_http(url, headers, payload, 30)
    assert status == 200 and len(sent) == 1
    assert sent[0].full_url == "https://api.ocr.space/parse/image"
    assert b"base64Image=data%3Aimage%2Fpng%3Bbase64%2C" in sent[0].data
    assert sent[0].get_header("Apikey") == "fixture-secret-value"
    assert "fixture-secret-value" not in sent[0].full_url


def test_openrouter_platform_429_is_distinct_from_upstream_and_partial_result():
    body = {"error": {"code": 429, "metadata": {"error_type": "rate_limit_exceeded"}}}
    headers = {
        "X-RateLimit-Limit": "50",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "2026-10-03T00:00:00Z",
    }
    assert explicit_quota_rejection("openrouter", 429, headers, json.dumps(body).encode())
    assert not explicit_quota_rejection("openrouter", 429, {}, json.dumps(body).encode())
    upstream = {
        "error": {
            "code": 429,
            "metadata": {"error_type": "rate_limit_exceeded", "provider_code": 429},
        }
    }
    assert not explicit_quota_rejection("openrouter", 429, headers, json.dumps(upstream).encode())
    partial = {**body, "usage": {"prompt_tokens": 1}}
    assert not explicit_quota_rejection("openrouter", 429, headers, json.dumps(partial).encode())


def test_translation_route_and_capability_mismatch_never_dispatch(tmp_path):
    calls = []
    targets = [
        target("riva", "nvidia", "nvidia/riva-translate-4b-instruct-v2"),
        target("gemma", "nvidia", "google/gemma-4-31b-it"),
        target("google", "google", "gemini-2.5-flash-lite"),
    ]
    gateway = make_gateway(tmp_path, targets, calls)
    data = task("translate", capability="translation", text="Hello.")
    explained = gateway.explain(data)
    assert explained["selected_target_id"] == "riva"
    assert all(not row["eligible"] for row in explained["candidates"] if row["target_id"] != "riva")
    assert gateway.run(data)["state"] == "completed"
    assert calls[0][2]["model"] == "nvidia/riva-translate-4b-instruct-v2"
    assert calls[0][2]["messages"][0] == {"role": "system", "content": "en-zh-cn"}
    with pytest.raises(GatewayError) as exc:
        gateway.run(task("bad", capability="translation", model="google/gemma-4-31b-it"))
    assert exc.value.code == "invalid_request"
    assert len(calls) == 1
    assert any(row["target_id"] == "gemma" for row in gateway.explain(task("next"))["candidates"])


def test_capacity_order_shared_scope_and_429_cooldown(tmp_path):
    calls = []
    renewable = Capacity("short_renewable", 60, NOW, "fixture", "fixture", NOW + timedelta(hours=1))
    targets = [
        target("gemma", "nvidia", "google/gemma-4-31b-it", capacity=renewable, scope="shared"),
        target("google", "google", "gemini-2.5-flash-lite", scope="shared"),
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
    ]
    gateway = make_gateway(tmp_path, targets, calls, status=429)
    assert gateway.explain(task(neurons=30))["selected_target_id"] == "gemma"
    first = gateway.run(task("one", neurons=30))
    assert first["state"] == "unknown" and first["http_status"] == 429
    assert gateway.broker.status(first["reservation_id"])["state"] == "unknown"
    second = gateway.explain(task("two", neurons=30))
    assert any(
        "shared_concurrency_limit" in row["reasons"]
        for row in second["candidates"]
        if row["target_id"] == "google"
    )
    assert any(
        "cooldown" in row["reasons"] for row in second["candidates"] if row["target_id"] == "gemma"
    )
    assert second["selected_target_id"] == "cf"
    assert len(calls) == 1


def test_missing_usage_and_concurrent_same_key(tmp_path):
    calls = []
    gateway = make_gateway(
        tmp_path, [target("nvidia", "nvidia", "google/gemma-4-31b-it")], calls, missing_usage=True
    )
    original = task("same")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: gateway.run(original), range(8)))
    assert len(calls) == 1
    assert any(row["state"] == "completed_usage_unknown" for row in results)
    assert gateway.usage()[0]["input_unknown_count"] == 1
    assert gateway.usage()[0]["reported_input_tokens"] is None
    assert gateway.usage()[0]["ledger_held_count"] == 1
    assert gateway.status("same")["ledger_basis"] == "held_estimate"
    assert gateway.broker.status(gateway.status("same")["reservation_id"])["state"] == "unknown"


@pytest.mark.parametrize(
    "provider,model,neurons",
    [
        ("nvidia", "google/gemma-4-31b-it", None),
        ("google", "gemini-2.5-flash-lite", None),
        ("cloudflare", "@cf/meta/llama-3.2-1b-instruct", 30),
        ("groq", "openai/gpt-oss-20b", None),
        ("mistral", "mistral-small-latest", None),
    ],
)
def test_authenticated_http_and_cli_share_gateway(
    tmp_path, monkeypatch, capsys, provider, model, neurons
):
    calls = []
    gateway = make_gateway(tmp_path, [target(provider, provider, model)], calls)
    token = "fixture-client-token-more-than-thirty-two-chars"
    server = make_gateway_server(gateway, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/v1/catalog", timeout=2)
        assert exc.value.code == 401
        request = urllib.request.Request(
            base + "/v1/tasks",
            data=json.dumps(task(provider=provider, model=model, neurons=neurons)).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["answer"] == "fixture answer"
        token_file = tmp_path / "client.token"
        token_file.write_text(token)
        args = SimpleNamespace(
            url=base,
            token_file=str(token_file),
            token_stdin=False,
            json=True,
            action="status",
            request_key="fixture-task",
        )
        gateway_cli(args)
        output = json.loads(capsys.readouterr().out)
        assert output["state"] == "completed" and "answer" not in output
        args.action = "usage"
        args.provider = args.model = args.from_at = args.to_at = None
        gateway_cli(args)
        assert json.loads(capsys.readouterr().out)[0]["requests"] == 1
        args.token_stdin = True
        args.token_file = None
        monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
        gateway_cli(args)
        assert json.loads(capsys.readouterr().out)[0]["requests"] == 1
        args.token_stdin = False
        args.token_file = str(token_file)
        args.action = "run"
        args.capability = "text_generation"
        args.max_output_tokens = 16
        args.source_language = args.target_language = None
        args.neuron_bound = neurons
        args.provider = provider
        args.model = model
        monkeypatch.setattr("sys.stdin", io.StringIO("fixture input"))
        gateway_cli(args)
        assert json.loads(capsys.readouterr().out)["state"] == "completed"
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_usage_time_filters_normalize_offsets_and_neurons_not_applicable(tmp_path):
    calls = []
    gateway = make_gateway(tmp_path, [target("nvidia", "nvidia", "google/gemma-4-31b-it")], calls)
    gateway.run(task())
    # UTC 01:00 is 09:00 in Taipei; string ordering without normalization fails.
    rows = gateway.usage(from_at="2026-09-30T08:59:00+08:00", to_at="2026-09-30T09:01:00+08:00")
    assert rows[0]["requests"] == 1
    assert rows[0]["neurons_unknown_count"] is None
    assert gateway.usage(from_at="2026-09-30T09:01:00+08:00") == []


def test_resolver_failure_is_safe_and_queryable(tmp_path):
    target_config = target("nvidia", "nvidia", "google/gemma-4-31b-it")
    calls = []

    def resolver(_):
        raise RuntimeError("private resolver diagnostic")

    gateway = Gateway(
        tmp_path / "gateway.db",
        (target_config,),
        HMAC_KEY,
        resolver,
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    with pytest.raises(GatewayError) as exc:
        gateway.run(task())
    assert exc.value.code == "unavailable"
    state = gateway.status("fixture-task")
    assert state["state"] == "rejected"
    assert state["error_code"] == "credential_or_dispatch_unavailable"
    assert calls == []
    assert b"private resolver diagnostic" not in (tmp_path / "gateway.db").read_bytes()


def test_pre_send_secret_failure_can_move_to_another_verified_route(tmp_path):
    calls = []
    renewable = Capacity("short_renewable", 60, NOW, "fixture", "fixture", NOW + timedelta(hours=1))
    first = target("gemma", "nvidia", "google/gemma-4-31b-it", capacity=renewable)
    second = target("google", "google", "gemini-2.5-flash-lite")

    def resolver(ref):
        if ref == "GEMMA":
            raise RuntimeError("fixture unavailable credential")
        return "fixture-secret-value"

    gateway = Gateway(
        tmp_path / "gateway.db",
        (first, second),
        HMAC_KEY,
        resolver,
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    result = gateway.run(task("fallback"))
    assert result["provider"] == "google" and result["state"] == "completed"
    assert len(calls) == 1 and "generativelanguage.googleapis.com" in calls[0][0]
    with sqlite3.connect(tmp_path / "gateway.db") as con:
        assert (
            con.execute("SELECT state FROM reservations WHERE target_id='gemma'").fetchone()[0]
            == "cancelled"
        )


def test_documented_quota_refusal_falls_back_once_and_records_both_attempts(tmp_path):
    calls = []
    targets = [
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct", priority=0),
        target("google", "google", "gemini-2.5-flash-lite", priority=1),
    ]

    def transport(url, headers, payload, timeout):
        calls.append(url)
        if "cloudflare" in url:
            return (
                429,
                {"Retry-After": "120"},
                json.dumps(
                    {"success": False, "errors": [{"code": 3036, "message": "daily limit"}]}
                ).encode(),
            )
        return fixture_transport([])(url, headers, payload, timeout)

    gateway = Gateway(
        tmp_path / "gateway.db",
        tuple(targets),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        transport,
        clock=lambda: NOW,
    )
    original = task("quota-fallback", neurons=30)
    result = gateway.run(original)
    assert result["state"] == "completed" and result["provider"] == "google"
    assert len(calls) == 2
    assert [a["state"] for a in result["attempts"]] == ["quota_rejected", "completed"]
    assert (
        gateway.broker.status(result["attempts"][0]["reservation_id"])["charges"][0]["amount"] == 0
    )
    assert gateway.run(original)["state"] == "completed" and len(calls) == 2
    assert gateway.usage(provider="cloudflare")[0]["quota_rejected_count"] == 1
    assert gateway.usage(provider="google")[0]["requests"] == 1
    db_bytes = (tmp_path / "gateway.db").read_bytes()
    assert b"fixture input" not in db_bytes and b"fixture-secret-value" not in db_bytes


@pytest.mark.parametrize(
    "status,body",
    [
        (429, {"success": False, "errors": [{"code": 3040}]}),
        (503, {"success": False, "errors": [{"code": 3036}]}),
        (429, {"error": {"message": "maybe quota"}}),
    ],
)
def test_ambiguous_or_capacity_failure_never_falls_back(tmp_path, status, body):
    calls = []
    targets = [
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct", priority=0),
        target("google", "google", "gemini-2.5-flash-lite", priority=1),
    ]

    def transport(url, headers, payload, timeout):
        calls.append(url)
        return status, {}, json.dumps(body).encode()

    gateway = Gateway(
        tmp_path / "gateway.db",
        tuple(targets),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        transport,
        clock=lambda: NOW,
    )
    result = gateway.run(task("no-fallback", neurons=30))
    assert result["state"] == "unknown"
    assert len(calls) == 1 and len(result["attempts"]) == 1
    assert gateway.broker.status(result["reservation_id"])["state"] == "unknown"


def test_quota_refusal_blocks_same_account_models(tmp_path):
    calls = []
    same_scope = "nvidia:organization:fixture"
    targets = [
        target("first", "nvidia", "google/gemma-4-31b-it", scope=same_scope, priority=0),
        target("second", "nvidia", "google/gemma-4-31b-it", scope=same_scope, priority=1),
        target("google", "google", "gemini-2.5-flash-lite", priority=2),
    ]

    def transport(url, headers, payload, timeout):
        calls.append(url)
        if "nvidia" in url:
            # NVIDIA has no approved quota-refusal classifier yet.
            return 429, {"Retry-After": "60"}, b'{"error":{"code":"rate_limit"}}'
        return fixture_transport([])(url, headers, payload, timeout)

    gateway = Gateway(
        tmp_path / "gateway.db",
        tuple(targets),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        transport,
        clock=lambda: NOW,
    )
    result = gateway.run(task("nvidia-ambiguous"))
    assert result["state"] == "unknown" and len(calls) == 1

    # A documented Gemini quota rejection must block another target in its
    # project scope while still permitting a different provider account.
    google_targets = [
        target("g1", "google", "gemini-2.5-flash-lite", scope="google:project:fixture", priority=0),
        target("g2", "google", "gemini-2.5-flash-lite", scope="google:project:fixture", priority=1),
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct", priority=2),
    ]
    calls.clear()

    def google_transport(url, headers, payload, timeout):
        calls.append(url)
        if "googleapis" in url:
            return 429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'
        return fixture_transport([])(url, headers, payload, timeout)

    gateway = Gateway(
        tmp_path / "google.db",
        tuple(google_targets),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        google_transport,
        clock=lambda: NOW,
    )
    result = gateway.run(task("scope-fallback", neurons=30))
    assert result["state"] == "completed"
    assert [a["target_id"] for a in result["attempts"]] == ["g1", "cf"]
    assert len(calls) == 2
    reasons = gateway.explain(task("next", neurons=30))["candidates"]
    assert "account_quota_cooldown" in next(r for r in reasons if r["target_id"] == "g2")["reasons"]


def test_valid_official_zero_blocks_but_unknown_eligibility_fails_closed(tmp_path):
    calls = []
    observed = Evidence(
        0,
        "official",
        NOW - timedelta(minutes=1),
        "fixture official",
        "account scope",
        NOW + timedelta(minutes=5),
    )
    unknown = Evidence(None, "unknown", None, None, None, None)
    zero = replace(
        target("gemma", "nvidia", "google/gemma-4-31b-it"),
        provider_quota_facts=(QuotaFact("requests", "rolling_minute", unknown, observed),),
    )
    alternative = target("google", "google", "gemini-2.5-flash-lite")
    gateway = make_gateway(tmp_path, [zero, alternative], calls)
    assert gateway.explain(task("official-zero"))["selected_target_id"] == "google"
    assert gateway.run(task("official-zero"))["provider"] == "google"
    closed = replace(alternative, free_eligible=False)
    other = tmp_path / "another"
    other.mkdir()
    second = make_gateway(other, [zero, closed], [])
    assert second.explain(task("no-free"))["selected_target_id"] is None
    with pytest.raises(GatewayError) as exc:
        second.run(task("no-free"))
    assert exc.value.code == "unavailable"


def test_gateway_example_is_disabled_and_has_three_nvidia_models():
    from pathlib import Path

    from quota_broker.config import load_gateway_config

    config = Path(__file__).resolve().parents[1] / "gateway.example.json"
    targets = load_gateway_config(config)
    assert {target.provider for target in targets} == {
        "nvidia",
        "google",
        "cloudflare",
        "groq",
        "mistral",
        "openrouter",
        "ocrspace",
    }
    assert len([target for target in targets if target.provider == "nvidia"]) == 3
    assert all(not target.enabled and not target.free_eligible for target in targets)
    assert all(target.secret_ref for target in targets)
    cloudflare = next(target for target in targets if target.provider == "cloudflare")
    assert cloudflare.capacity.kind == "short_renewable"
    assert cloudflare.capacity.refresh_seconds == 86_400


def test_timeout_preserves_unknown_and_never_replays_after_restart(tmp_path):
    calls = []
    route = target("nvidia", "nvidia", "google/gemma-4-31b-it")

    def timeout(_url, _headers, _payload, _timeout):
        calls.append(1)
        raise TimeoutError("fixture timeout")

    gateway = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        timeout,
        clock=lambda: NOW,
    )
    first = gateway.run(task("timeout"))
    assert first["state"] == "unknown" and first["error_code"] == "TimeoutError"
    assert first["ledger_basis"] == "held_estimate"
    restarted = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        timeout,
        clock=lambda: NOW,
    )
    assert restarted.run(task("timeout"))["state"] == "unknown"
    assert calls == [1]


def test_route_snapshot_preserves_legacy_shape_but_binds_gateway_secret_ref():
    from quota_broker.core import Broker

    current = target("nvidia", "nvidia", "google/gemma-4-31b-it")
    legacy = replace(current, secret_ref=None)
    assert "secret_ref" not in json.loads(Broker._snapshot(legacy))
    assert json.loads(Broker._snapshot(current))["secret_ref"] == "NVIDIA"


def test_cloudflare_tokens_without_neurons_keeps_ledger_hold(tmp_path):
    calls = []
    route = target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct")

    def partial(url, headers, payload, timeout):
        calls.append(1)
        return (
            200,
            {},
            json.dumps(
                {
                    "result": {"response": "fixture answer"},
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                }
            ).encode(),
        )

    gateway = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        partial,
        clock=lambda: NOW,
    )
    result = gateway.run(task("partial", provider="cloudflare", neurons=30))
    assert result["state"] == "completed_usage_unknown"
    assert result["ledger_state"] == "unknown"
    assert result["ledger_basis"] == "held_estimate"
    assert result["reported_input_tokens"] == 11
    assert result["reported_neurons"] is None
    usage = gateway.usage()[0]
    assert usage["neurons_unknown_count"] == 1
    assert usage["ledger_neurons"] == 30
    assert usage["ledger_held_count"] == 1
    assert calls == [1]


def test_all_configured_quota_metrics_required_before_settlement(tmp_path):
    route = target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct")
    route = replace(
        route, quotas=route.quotas + (Quota("cf-input", "input_tokens", 10000, "rolling_minute"),)
    )

    def partial(_url, _headers, _payload, _timeout):
        return (
            200,
            {},
            json.dumps(
                {
                    "result": {"response": "fixture answer"},
                    "usage": {"completion_tokens": 3, "neurons": 7},
                }
            ).encode(),
        )

    gateway = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        partial,
        clock=lambda: NOW,
    )
    result = gateway.run(task("metrics", provider="cloudflare", neurons=30))
    assert result["state"] == "completed_usage_unknown"
    assert result["ledger_state"] == "unknown"
    assert result["error_code"] is None


def test_expired_renewable_evidence_downgrades_to_unknown(tmp_path):
    old = Capacity(
        "short_renewable",
        30,
        NOW - timedelta(days=1),
        "fixture",
        "account",
        NOW - timedelta(minutes=1),
    )
    fresh = Capacity(
        "short_renewable",
        60,
        NOW - timedelta(minutes=1),
        "fixture",
        "account",
        NOW + timedelta(minutes=5),
    )
    stale_target = target("gemma", "nvidia", "google/gemma-4-31b-it", capacity=old)
    fresh_target = target("google", "google", "gemini-2.5-flash-lite", capacity=fresh)
    calls = []
    gateway = make_gateway(tmp_path, [stale_target, fresh_target], calls)
    plan = gateway.explain(task("freshness"))
    assert plan["selected_target_id"] == "google"
    assert (
        next(row for row in plan["candidates"] if row["target_id"] == "gemma")[
            "effective_capacity_kind"
        ]
        == "unknown"
    )
    result = gateway.run(task("freshness"))
    assert result["provider"] == "google"
    assert result["route_reason"].startswith("short_renewable:60")


def test_dispatch_crash_gap_is_counted_and_not_replayed(tmp_path):
    calls = []
    route = target("nvidia", "nvidia", "google/gemma-4-31b-it")
    gateway = make_gateway(tmp_path, [route], calls)
    data = task("crash-gap")
    bound = 4 * len(data["input"].encode()) + 256
    reservation = gateway.broker.reserve(
        {
            "request_key": "gw:crash-gap:0",
            "capability": "text_generation",
            "provider": None,
            "model": None,
            "input_token_bound": bound,
            "max_output_tokens": 16,
            "neuron_bound": None,
            "exclude_target_ids": [],
        }
    )
    gateway.broker.dispatch(reservation["reservation_id"])
    with sqlite3.connect(tmp_path / "gateway.db") as con:
        con.execute(
            "INSERT INTO gateway_tasks(request_key,payload_hmac,state,reservation_id,target_id,"
            "provider,model,created_at,estimated_input_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "crash-gap",
                gateway._hmac(data),
                "preparing",
                reservation["reservation_id"],
                route.id,
                route.provider,
                route.model,
                NOW.isoformat(),
                bound,
            ),
        )
    state = gateway.status("crash-gap")
    assert state["state"] == "unknown" and state["ledger_state"] == "dispatched"
    assert state["dispatched_at"] is None and state["ledger_dispatched_at"] is not None
    usage = gateway.usage(from_at="2026-09-30T08:59:00+08:00", to_at="2026-09-30T09:01:00+08:00")
    assert usage[0]["requests"] == 1
    assert usage[0]["outcome_unknown_count"] == 1
    assert usage[0]["ledger_held_count"] == 1
    restarted = Gateway(
        tmp_path / "gateway.db",
        (route,),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    assert restarted.run(data)["state"] == "unknown"
    assert calls == []


def test_current_nvidia_lightning_text_model_uses_nonstreaming_no_thinking(tmp_path):
    model = "nvidia/nemotron-3.5-lightning-30b-a3b"
    calls = []
    gateway = make_gateway(tmp_path, [target("lightning", "nvidia", model)], calls)
    result = gateway.run(task("llm", provider="nvidia", model=model, text="Reply with OK."))
    assert result["state"] == "completed"
    assert result["provider"] == "nvidia" and result["model"] == model
    payload = calls[0][2]
    assert payload["model"] == model
    assert payload["stream"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert calls[0][3] == 60.0


@pytest.mark.parametrize(
    "provider,model,host,output_field",
    [
        ("groq", "openai/gpt-oss-20b", "api.groq.com", "max_completion_tokens"),
        ("mistral", "mistral-small-latest", "api.mistral.ai", "max_tokens"),
    ],
)
def test_new_chat_provider_fixed_request_and_429_hold(
    tmp_path, provider, model, host, output_field
):
    calls = []
    gateway = make_gateway(tmp_path, [target(provider, provider, model)], calls)
    payload = task("new-chat", provider=provider, model=model)
    result = gateway.run(payload)
    assert result["state"] == "completed"
    url, headers, request, timeout = calls[0]
    assert url.startswith("https://" + host + "/")
    assert headers == {"Authorization": "Bearer fixture-secret-value"}
    assert request["messages"] == [{"role": "user", "content": "fixture input"}]
    assert request["stream"] is False and request[output_field] == 16
    assert timeout <= 90
    if provider == "groq":
        assert request["reasoning_effort"] == "low"
        assert request["include_reasoning"] is False
    restarted = Gateway(
        tmp_path / "gateway.db",
        (target(provider, provider, model),),
        HMAC_KEY,
        lambda _: "fixture-secret-value",
        fixture_transport(calls),
        clock=lambda: NOW,
    )
    assert restarted.run(payload)["state"] == "completed"
    assert len(calls) == 1
    denied = target(provider, provider, model)
    other = tmp_path / "429"
    other.mkdir()
    second = make_gateway(other, [denied], calls, status=429)
    unknown = second.run(task("rate-limit", provider=provider, model=model))
    assert unknown["state"] == "unknown" and unknown["http_status"] == 429
    assert unknown["ledger_basis"] == "held_estimate"
    assert second.run(task("rate-limit", provider=provider, model=model))["state"] == "unknown"
    assert len(calls) == 2


def test_unconstrained_route_explains_new_providers_and_blocks_unverified(tmp_path):
    groq = target("groq", "groq", "openai/gpt-oss-20b", priority=0)
    mistral = target("mistral", "mistral", "mistral-small-latest", priority=1)
    google = target("google", "google", "gemini-2.5-flash-lite", priority=2)
    calls = []
    gateway = make_gateway(tmp_path, [groq, mistral, google], calls)
    original = task("unconstrained")
    plan = gateway.explain(original)
    assert plan["selected_target_id"] == "groq"
    assert {row["provider"] for row in plan["candidates"]} == {"groq", "mistral", "google"}
    assert gateway.run(original)["provider"] == "groq"
    assert len(calls) == 1
    other = tmp_path / "unverified"
    other.mkdir()
    blocked = make_gateway(
        other,
        [replace(groq, free_eligible=False), replace(mistral, billing_enabled=True)],
        [],
    )
    denied = blocked.explain(task("unverified"))
    assert denied["selected_target_id"] is None
    assert all("eligibility_or_official_capacity" in row["reasons"] for row in denied["candidates"])


@pytest.mark.parametrize(
    "provider,model",
    [("groq", "openai/gpt-oss-20b"), ("mistral", "mistral-small-latest")],
)
def test_new_chat_provider_transport_accepts_only_fixed_origin(monkeypatch, provider, model):
    from quota_broker.gateway_providers import ProviderError, official_request, provider_http

    class Response:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"choices":[],"usage":{}}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == url and timeout == 30
            return Response()

    monkeypatch.setattr(
        "quota_broker.gateway_providers.urllib.request.build_opener", lambda *_: Opener()
    )
    url, headers, payload = official_request(
        provider, model, "fixture-account", "fixture-secret", "fixture input", 16, None, None
    )
    assert provider_http(url, headers, payload, 30)[0] == 200
    with pytest.raises(ProviderError, match="unapproved provider URL"):
        provider_http(url + "/other", headers, payload, 30)
