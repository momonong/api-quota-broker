"""Resource-pool contracts through real Gateway, queue, HTTP and CLI; no external I/O."""

import io
import json
import os
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gateway import HMAC_KEY, NOW, fixture_transport, target, task
from test_registry import load, manifest

from quota_broker import cli
from quota_broker.client import ClientError, _json_http
from quota_broker.config import Capacity, load_gateway_config
from quota_broker.core import BrokerError, stamp
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_server import make_gateway_server
from quota_broker.queue import DurableQueue

QUEUE_KEY = b"fixture-encryption-key".ljust(32, b"!")
TOKEN = "fixture-client-access-token-longer-than-32"
ADMIN = "fixture-admin-independent-token-longer-than-32"


def build(tmp_path, targets, transport=None, registry=None, resolver=None):
    tmp_path.chmod(0o700)
    db = tmp_path / "pool.sqlite"
    descriptor = os.open(db, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    clock = SimpleNamespace(now=NOW)
    calls = []
    gateway = Gateway(
        db,
        tuple(targets),
        HMAC_KEY,
        resolver or (lambda _: "fixture-secret-value"),
        transport or fixture_transport(calls),
        clock=lambda: clock.now,
        registry=registry,
    )
    return gateway, clock, calls


def observation(remaining, *, limit=100, at=NOW, reset=60, metric="requests", window="day"):
    return {
        "metric": metric,
        "window": window,
        "remaining": remaining,
        "limit": limit,
        "as_of": stamp(at),
        "valid_until": stamp(at + timedelta(seconds=reset)),
        "reset_at": stamp(at + timedelta(seconds=reset)),
        "provenance": "observed",
        "source": "trusted_operator",
        "confidence": "administrator_verified",
    }


def serve(gateway, queue=None, worker=False):
    server = make_gateway_server(
        gateway, TOKEN, port=0, queue=queue, worker=worker, admin_token=ADMIN
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def close(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(2)


def invoke(monkeypatch, capsys, base, token_path, *args, content=""):
    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "gateway",
            "--url",
            base,
            "--token-file",
            str(token_path),
            "--json",
            *args,
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(content))
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_manifest_same_name_config_only_through_http_cli_queue(tmp_path, monkeypatch, capsys):
    registry = load(tmp_path, manifest("openai/gpt-oss-20b"))
    document = json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())
    item = document["targets"][0]
    item.update(
        id="eighth-account",
        provider="eighth",
        model="openai/gpt-oss-20b",
        account_id="fixture-account",
        secret_ref="EIGHTH_KEY",
        enabled=True,
        free_eligible=True,
        verified_at=stamp(NOW - timedelta(seconds=1)),
        expires_at=stamp(NOW + timedelta(hours=1)),
        provider_quota_facts=[],
        shared_concurrency_scope=None,
        shared_concurrency_limit=None,
    )
    for cap in item["local_safety_caps"]:
        cap["limit"] = 10000 if cap["metric"] == "input_tokens" else 10
    config = tmp_path / "targets.json"
    config.write_text(json.dumps({"targets": [item]}))
    eighth = load_gateway_config(config, registry=registry)[0]
    groq = target("groq", "groq", "openai/gpt-oss-20b")
    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url, headers, body, timeout))
        return (
            200,
            {},
            json.dumps(
                {
                    "id": "fixture-id",
                    "choices": [
                        {"message": {"content": "fixture answer"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
                }
            ).encode(),
        )

    gateway, _, _ = build(tmp_path, [eighth, groq], transport, registry)
    queue = DurableQueue(gateway, QUEUE_KEY)
    server, thread, base = serve(gateway, queue)
    token_path = tmp_path / "client.token"
    token_path.write_text(TOKEN)
    try:
        catalog = invoke(monkeypatch, capsys, base, token_path, "catalog")
        assert {row["provider"] for row in catalog} == {"eighth", "groq"}
        result = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "run",
            "--request-key",
            "eighth-run",
            "--capability",
            "text_generation",
            "--provider",
            "eighth",
            "--model",
            "openai/gpt-oss-20b",
            "--require-feature",
            "json_output",
            content="fixture input",
        )
        assert result["state"] == "completed" and result["provider"] == "eighth"
        assert calls[0][0].startswith("https://eighth.example/")
        assert calls[0][2]["response_format"] == {"type": "json_object"}
        assert (
            invoke(monkeypatch, capsys, base, token_path, "status", "eighth-run").get("answer")
            is None
        )
        usage = invoke(monkeypatch, capsys, base, token_path, "usage", "--provider", "eighth")
        assert usage[0]["reported_output_tokens"] == 3
        with pytest.raises(ClientError):
            _json_http(
                base + "/v1/routes/explain",
                task(model="openai/gpt-oss-20b"),
                {"Authorization": "Bearer " + TOKEN},
            )
        queued = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "submit",
            "--request-key",
            "eighth-job",
            "--capability",
            "text_generation",
            "--provider",
            "eighth",
            "--model",
            "openai/gpt-oss-20b",
            content="private queued input",
        )
        assert queued["state"] == "queued" and "input" not in queued
        assert (
            invoke(monkeypatch, capsys, base, token_path, "worker", "--once")["state"]
            == "completed"
        )
        assert (
            invoke(monkeypatch, capsys, base, token_path, "wait", "eighth-job", "--timeout", "0")[
                "state"
            ]
            == "completed"
        )
        assert (
            invoke(monkeypatch, capsys, base, token_path, "result", "eighth-job")["answer"]
            == "fixture answer"
        )
        with pytest.raises(ClientError):
            _json_http(base + "/v1/queue/eighth-job/result", None, {})
        assert len(calls) == 2
        for plain in (
            b"private queued input",
            b"fixture answer",
            b"fixture-secret-value",
            QUEUE_KEY,
        ):
            assert plain not in (tmp_path / "pool.sqlite").read_bytes()
    finally:
        close(server, thread)


def test_queue_busy_reset_restart_and_cancel_deadline(tmp_path):
    gateway, clock, calls = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite", rpm=1)])
    queue = DurableQueue(gateway, QUEUE_KEY)
    queue.submit(task("one"))
    assert queue.tick("worker")["state"] == "completed"
    queue.submit(task("two"))
    waiting = queue.tick("worker")
    assert waiting["state"] == "waiting" and waiting["next_retry_at"] > stamp(NOW)
    restarted = Gateway(
        gateway.db,
        gateway.targets,
        HMAC_KEY,
        gateway.secret_resolver,
        gateway.transport,
        clock=gateway.clock,
    )
    queue = DurableQueue(restarted, QUEUE_KEY)
    clock.now += timedelta(seconds=61)
    assert queue.tick("replacement")["state"] == "completed"
    assert len(calls) == 2
    queue.submit(task("cancel"))
    assert queue.cancel("cancel")["state"] == "cancelled"
    queue.submit({**task("deadline"), "deadline": stamp(clock.now + timedelta(seconds=1))})
    clock.now += timedelta(seconds=2)
    assert queue.status("deadline")["state"] == "expired"
    assert queue.tick("worker") is None and len(calls) == 2


def test_unknown_ledger_and_health_do_not_block_other_provider_queue(tmp_path):
    calls = []
    regular = fixture_transport(calls)

    def transport(url, headers, body, timeout):
        if "api.groq.com" in url:
            calls.append("unknown")
            raise TimeoutError("fixture input secret in exception must not persist")
        return regular(url, headers, body, timeout)

    a = replace(target("a", "groq", "openai/gpt-oss-20b"), concurrency_limit=1)
    b = target("b", "google", "gemini-2.5-flash-lite", priority=1)
    gateway, _, _ = build(tmp_path, [a, b], transport)
    unknown = gateway.run(task("unknown"))
    assert unknown["state"] == "unknown"
    queue = DurableQueue(gateway, QUEUE_KEY)
    queue.submit(task("other-provider"))
    assert queue.tick("worker")["state"] == "completed"
    assert queue.result("other-provider")["provider"] == "google"
    gateway.reset_health("a")
    assert gateway.status("unknown")["ledger_state"] == "unknown"
    forced = gateway.explain(task(provider="groq"))
    assert (
        forced["permanent_rejection"]
        and "unknown_requires_reconciliation"
        in next(row for row in forced["candidates"] if row["target_id"] == "a")["reasons"]
    )
    assert len(calls) == 2


def test_explain_and_atomic_reserve_use_remaining_and_holds(tmp_path):
    gateway, _, _ = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")])
    gateway.observe_quota("a", observation(1, at=NOW - timedelta(seconds=1)))
    assert gateway.explain(task())["selected_target_id"] == "a"
    request = {
        "request_key": "reserve-one",
        "capability": "text_generation",
        "input_token_bound": 300,
        "max_output_tokens": 16,
    }
    first = gateway.broker.reserve(request)
    with pytest.raises(BrokerError):
        gateway.broker.reserve({**request, "request_key": "reserve-two"})
    plan = gateway.explain(task())
    assert (
        plan["temporary"]
        and plan["candidates"][0]["quota_observations"][0]["effective_remaining"] == 0
    )
    gateway.broker.cancel(first["reservation_id"])
    assert gateway.explain(task())["selected_target_id"] == "a"


def test_observation_changed_after_reserve_fences_dispatch(tmp_path):
    gateway = None

    def resolver(_):
        gateway.observe_quota("a", observation(0))
        return "fixture-secret-value"

    gateway, _, calls = build(
        tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], resolver=resolver
    )
    with pytest.raises(GatewayError):
        gateway.run(task("race"))
    assert calls == []
    status = gateway.status("race")
    assert status["attempts"][0]["state"] == "pre_send_failed"
    assert status["ledger_state"] == "cancelled"


def test_ranking_uses_observed_headroom_health_and_budget_refresh(tmp_path):
    targets = [
        replace(
            target(name, "google", "gemini-2.5-flash-lite", priority=index),
            account_id="fixture-account-" + name,
        )
        for index, name in enumerate("abc")
    ]
    gateway, clock, _ = build(tmp_path, targets)
    gateway.observe_quota("a", observation(10))
    gateway.observe_quota("b", observation(90))
    plan = gateway.explain(task())
    assert plan["selected_target_id"] == "b"
    assert plan["candidates"][-1]["ranking_factors"]["observed_headroom"] is None
    gateway.routing.record("b", NOW, 500, 100, False, None, None)
    assert gateway.explain(task())["selected_target_id"] == "a"
    gateway.routing.record("a", NOW, 200, 500, True, None, None)
    refreshed = replace(
        targets[2],
        capacity=Capacity(
            "short_renewable", 60, NOW, "fixture budget", "fixture scope", NOW + timedelta(hours=1)
        ),
    )
    restarted = Gateway(
        gateway.db,
        (*targets[:2], refreshed),
        HMAC_KEY,
        gateway.secret_resolver,
        gateway.transport,
        clock=gateway.clock,
    )
    assert restarted.explain(task())["selected_target_id"] == "c"
    clock.now += timedelta(seconds=61)
    assert all(
        o["current"] is False
        for row in gateway.explain(task())["candidates"]
        for o in row["quota_observations"]
    )


def test_circuit_half_open_auth_repair_and_generic_task_errors(tmp_path):
    gateway, clock, _ = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")])
    state = gateway.routing
    state.record("a", NOW, 500, 20, False, None, None)
    assert gateway.explain(task())["selected_target_id"] == "a"
    state.record("a", NOW, 500, 20, False, None, None)
    assert gateway.explain(task())["temporary"]
    clock.now += timedelta(seconds=61)
    with gateway.broker._tx() as con:
        assert state.permit(con, "a", "first", clock.now)
        assert not state.permit(con, "a", "second", clock.now)
    state.record("a", clock.now, 400, 20, False, None, None)
    assert gateway.explain(task())["selected_target_id"] == "a"
    state.record("a", clock.now, 401, 20, False, None, None)
    assert gateway.explain(task())["permanent_rejection"]
    gateway.reset_health("a")
    assert gateway.explain(task())["selected_target_id"] == "a"


def test_quota_evidence_stale_contradictory_and_groq_dimensions(tmp_path):
    gateway, _, _ = build(tmp_path, [target("a", "groq", "openai/gpt-oss-20b")])
    with pytest.raises(GatewayError):
        gateway.observe_quota("a", observation(101))
    gateway.observe_quota("a", observation(20))
    gateway.observe_quota("a", observation(90, at=NOW - timedelta(seconds=1)))
    assert gateway.explain(task())["candidates"][0]["quota_observations"][0]["remaining"] == 20
    scope = "groq:account:fixture-account"
    gateway.routing.capture(
        "groq",
        scope,
        {
            "x-ratelimit-remaining-requests": "10",
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-reset-requests": "2h",
            "x-ratelimit-remaining-tokens": "8000",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-reset-tokens": "59.56s",
        },
        NOW,
        (),
    )
    rows = gateway.explain(task())["candidates"][0]["quota_observations"]
    assert {(row["metric"], row["window"]) for row in rows} == {
        ("requests", "day"),
        ("tokens", "rolling_minute"),
    }
    assert gateway.targets[0].capacity.kind == "unknown"


def test_attempt_policy_can_use_fourth_unsent_target(tmp_path):
    targets = [target(str(i), "google", "gemini-2.5-flash-lite", priority=i) for i in range(4)]
    gateway, _, calls = build(
        tmp_path, targets, resolver=lambda name: "fixture-secret" if name == "3" else ""
    )
    result = gateway.run({**task("four-attempts"), "max_attempts": 4})
    assert result["state"] == "completed" and len(result["attempts"]) == 4 and len(calls) == 1


def test_admin_auth_and_worker_failure_are_visible_without_secret_logs(
    tmp_path, monkeypatch, capsys
):
    gateway, _, _ = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")])
    queue = DurableQueue(gateway, QUEUE_KEY)

    def broken(_):
        raise ValueError("fixture-sensitive-input-and-key")

    monkeypatch.setattr(queue, "tick", broken)
    server, thread, base = serve(gateway, queue, worker=True)
    try:
        headers = {"Authorization": "Bearer " + TOKEN}
        for _ in range(50):
            diagnosis = _json_http(base + "/v1/diagnostics", None, headers)
            if diagnosis["queue_worker"]["stopped"]:
                break
            time.sleep(0.01)
        assert diagnosis["queue_worker"]["error_code"] == "worker_stopped"
        assert "fixture-sensitive" not in json.dumps(diagnosis) + capsys.readouterr().out
        with pytest.raises(ClientError):
            _json_http(base + "/v1/admin/targets/a/quota", observation(0), headers)
        admin_headers = {"Authorization": "Bearer " + ADMIN}
        _json_http(base + "/v1/admin/targets/a/quota", observation(0), admin_headers)
        assert gateway.explain(task())["selected_target_id"] is None
        token_path = tmp_path / "admin.token"
        token_path.write_text(ADMIN)
        gateway.routing.record("a", NOW, 401, 0, False, None, None)
        assert (
            invoke(monkeypatch, capsys, base, token_path, "reset-health", "a")["state"] == "updated"
        )
        assert gateway.diagnostics()["targets"][0]["health"]["state"] == "closed"
    finally:
        close(server, thread)


def test_cli_slow_provider_and_worker_wait_bound_is_not_old_15_seconds(
    tmp_path, monkeypatch, capsys
):
    calls = []
    fast = fixture_transport(calls)

    def slow(url, headers, payload, timeout):
        assert timeout >= 15.05
        time.sleep(15.05)
        return fast(url, headers, payload, timeout)

    gateway, _, _ = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")], slow)
    queue = DurableQueue(gateway, QUEUE_KEY)
    server, thread, base = serve(gateway, queue)
    original = cli._json_http
    observed = []

    def checked(url, data, headers, timeout=15):
        observed.append(timeout)
        assert timeout >= 180
        return original(url, data, headers, timeout)

    monkeypatch.setattr(cli, "_json_http", checked)
    token_path = tmp_path / "client.token"
    token_path.write_text(TOKEN)
    try:
        result = invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "run",
            "--request-key",
            "bounded",
            "--capability",
            "text_generation",
            content="fixture input",
        )
        assert result["state"] == "completed"
        invoke(
            monkeypatch,
            capsys,
            base,
            token_path,
            "submit",
            "--request-key",
            "worker-bounded",
            "--capability",
            "text_generation",
            content="fixture input",
        )
        assert (
            invoke(monkeypatch, capsys, base, token_path, "worker", "--once")["state"]
            == "completed"
        )
        assert observed == [185, 185, 185]
    finally:
        close(server, thread)


def test_generic_cloudflare_estimation_unknown_usage_frees_only_execution_slot(tmp_path):
    from quota_broker.config import NeuronEstimate

    calls = []

    def cf_response(url, headers, payload, timeout):
        calls.append(payload)
        return (
            200,
            {},
            json.dumps(
                {
                    "result": {
                        "response": "fixture answer",
                        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                    }
                }
            ).encode(),
        )

    estimate = NeuronEstimate(
        500, 4096, 128, "trusted_operator", NOW - timedelta(seconds=1), NOW + timedelta(hours=1)
    )
    cf = replace(
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
        concurrency_limit=1,
        shared_concurrency_scope="cf-fixture-account",
        shared_concurrency_limit=1,
        neuron_estimate=estimate,
    )
    gateway, _, _ = build(tmp_path, [cf], cf_response)
    generic = task("cf-one")
    plan = gateway.explain(generic)
    assert plan["selected_target_id"] == "cf"
    assert plan["candidates"][0]["effective_neuron_bound"] == 500
    assert plan["candidates"][0]["neuron_estimate"]["basis"] == "estimated_per_request_upper_bound"
    queue = DurableQueue(gateway, QUEUE_KEY)
    queue.submit(generic)
    first = queue.tick("worker")
    assert first["state"] == "completed"
    result = queue.result("cf-one")
    assert result["state"] == "completed_usage_unknown" and result["reported_neurons"] is None
    assert result["ledger_state"] == "unknown" and result["attempts"][0]["execution_finished_at"]
    # A trusted config change is permitted after proven execution completion;
    # the unknown quota charges continue under the original bucket identity.
    changed = replace(cf, max_output_tokens=128)
    gateway = Gateway(
        gateway.db,
        (changed,),
        HMAC_KEY,
        gateway.secret_resolver,
        gateway.transport,
        clock=gateway.clock,
    )
    queue = DurableQueue(gateway, QUEUE_KEY)
    queue.submit(task("cf-two"))
    assert queue.tick("worker")["state"] == "completed"
    assert len(calls) == 2
    assert queue.submit(generic)["state"] == "completed" and len(calls) == 2
    usage = gateway.usage()[0]
    assert usage["ledger_neurons"] == 1000 and usage["reported_neurons"] is None
    assert usage["neurons_unknown_count"] == 2 and usage["ledger_held_count"] == 2
    assert gateway.explain(task("third"))["temporary"]
    assert changed.neurons(300, 16, NOW, 1) == 500
    assert changed.neurons(300, 16, NOW, 700) == 700
    unconfigured = replace(changed, neuron_estimate=None)
    other = Gateway(
        gateway.db,
        (unconfigured,),
        HMAC_KEY,
        gateway.secret_resolver,
        gateway.transport,
        clock=gateway.clock,
    )
    reasons = other.explain(task("no-estimate"))["candidates"][0]["reasons"]
    assert "estimation_unconfigured" in reasons


def test_stale_completion_does_not_overwrite_newer_health_or_observation(tmp_path):
    gateway, clock, _ = build(tmp_path, [target("a", "groq", "openai/gpt-oss-20b")])
    clock.now = NOW + timedelta(seconds=5)
    gateway.routing.record(
        "a", clock.now, 401, 20, False, None, None, as_of=NOW + timedelta(seconds=2)
    )
    gateway.routing.record("a", clock.now, 200, 20, True, None, None, as_of=NOW)
    assert gateway.diagnostics()["targets"][0]["health"]["state"] == "repair_required"
    headers = {
        "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-reset-requests": "1m",
    }
    gateway.routing.capture(
        "groq",
        "groq:account:fixture-account",
        headers,
        clock.now,
        (),
        as_of=NOW + timedelta(seconds=2),
    )
    gateway.routing.capture(
        "groq",
        "groq:account:fixture-account",
        {**headers, "x-ratelimit-remaining-requests": "80"},
        clock.now,
        (),
        as_of=NOW,
    )
    assert gateway.diagnostics()["targets"][0]["quota_observations"][0]["remaining"] == 20


def test_registered_adapter_error_usage_and_observation_need_no_provider_branch(tmp_path):
    from quota_broker.registry import Registry

    class FixtureAdapter:
        def request(
            self,
            spec,
            account_id,
            secret,
            content,
            max_output_tokens,
            source_language,
            target_language,
        ):
            return (
                spec.endpoint_template,
                {"Authorization": "Bearer " + secret},
                {"model": spec.model, "account": account_id, "input": content},
            )

        def transport(self, spec, url, headers, payload, timeout):
            raise AssertionError("fixture transport is injected")

        def interpret(self, spec, status, raw):
            if status == 200:
                return "fixture answer", 11, 3, None, "fixture-id"
            return None, None, None, None, None

        def quota_rejection(self, spec, status, raw, headers, sensitive=()):
            return status == 429 and json.loads(raw).get("not_executed") is True

        def quota_observations(self, spec, status, raw, headers, now, *, as_of=None):
            if status == 429:
                return [observation(0, at=as_of or now)]
            return []

    doc = manifest("custom-protocol")
    doc["providers"][0]["adapter"] = "fixture_protocol"
    manifest_file = tmp_path / "registered.json"
    manifest_file.write_text(json.dumps(doc))
    registry = Registry.load(manifest_file, adapters={"fixture_protocol": FixtureAdapter()})
    spec = registry.resolve("custom-protocol", "eighth")
    a = replace(
        target("a", "google", "gemini-2.5-flash-lite"),
        provider="eighth",
        model=spec.model,
        model_info=spec,
        account_id="fixture-account-a",
    )
    b = replace(
        a,
        id="b",
        account_id="fixture-account-b",
        priority=1,
        quotas=tuple(replace(q, bucket="b-" + q.bucket) for q in a.quotas),
    )
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if payload["account"] == "fixture-account-a":
            return 429, {"retry-after": "60"}, b'{"not_executed":true}'
        return 200, {}, b"{}"

    gateway, _, _ = build(tmp_path, [a, b], transport, registry)
    result = gateway.run(task("registered-error"))
    assert result["state"] == "completed" and result["target_id"] == "b"
    assert result["attempts"][0]["ledger_state"] == "quota_rejected"
    assert result["attempts"][1]["reported_output_tokens"] == 3 and len(calls) == 2
    first = next(row for row in gateway.diagnostics()["targets"] if row["target_id"] == "a")
    assert first["quota_observations"][0]["source"] == "adapter:fixture_protocol"
    assert first["quota_observations"][0]["remaining"] == 0
    # A packaged compatible provider has no automatic quota-semantic inference.
    openai_registry = load(tmp_path, manifest("plain"))
    plain = openai_registry.resolve("plain")
    assert openai_registry.quota_rejection(plain, 429, b'{"not_executed":true}', {}) is False
    assert openai_registry.quota_observations(plain, 429, b"{}", {}, NOW) == []


def test_estimation_requirement_follows_resource_dimension_not_provider_name(tmp_path):
    from quota_broker.config import Quota

    google = target("g", "google", "gemini-2.5-flash-lite")
    google = replace(
        google, quotas=(*google.quotas, Quota("fixture-neurons", "neurons", 1000, "day"))
    )
    gateway, _, calls = build(tmp_path, [google])
    plan = gateway.explain(task())
    assert (
        plan["permanent_rejection"]
        and "estimation_unconfigured" in plan["candidates"][0]["reasons"]
    )
    with pytest.raises(GatewayError):
        gateway.run(task("unsent"))
    assert calls == []


def test_service_worker_runs_and_stops_with_server_lifecycle(tmp_path):
    gateway, _, calls = build(tmp_path, [target("a", "google", "gemini-2.5-flash-lite")])
    queue = DurableQueue(gateway, QUEUE_KEY)
    server, thread, base = serve(gateway, queue, worker=True)
    try:
        headers = {"Authorization": "Bearer " + TOKEN}
        _json_http(base + "/v1/queue", task("background"), headers)
        for _ in range(150):
            state = _json_http(base + "/v1/queue/background", None, headers)
            if state["state"] == "completed":
                break
            time.sleep(0.01)
        assert state["state"] == "completed" and len(calls) == 1
        diagnosis = _json_http(base + "/v1/diagnostics", None, headers)
        assert diagnosis["queue_worker"]["running"] and diagnosis["queue_worker"]["last_tick_at"]
        assert (
            _json_http(base + "/v1/queue/background/result", None, headers)["answer"]
            == "fixture answer"
        )
    finally:
        close(server, thread)
    assert not server.worker_thread.is_alive()


def test_legacy_schema_has_no_invented_execution_completion(tmp_path):
    gateway, _, calls = build(
        tmp_path, [replace(target("a", "google", "gemini-2.5-flash-lite"), concurrency_limit=1)]
    )
    reservation = gateway.broker.reserve(
        {
            "request_key": "old-dispatch",
            "capability": "text_generation",
            "input_token_bound": 300,
            "max_output_tokens": 16,
        }
    )
    gateway.broker.dispatch(reservation["reservation_id"])
    gateway.broker.report(
        {
            "reservation_id": reservation["reservation_id"],
            "report_key": "old-report",
            "state": "unknown",
        }
    )
    with sqlite3.connect(gateway.db) as con:
        con.execute("DROP TABLE execution_completion")
    restarted = Gateway(
        gateway.db,
        gateway.targets,
        HMAC_KEY,
        gateway.secret_resolver,
        gateway.transport,
        clock=gateway.clock,
    )
    assert restarted.explain(task())["permanent_rejection"]
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT count(*) FROM execution_completion").fetchone()[0] == 0
    assert (
        restarted.broker.status(reservation["reservation_id"])["state"] == "unknown" and calls == []
    )


def test_gateway_serve_loads_manifest_and_private_queue_before_start(tmp_path, monkeypatch):
    doc = manifest("config-only-service")
    registry_file = tmp_path / "manifest.json"
    registry_file.write_text(json.dumps(doc))
    configuration = json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())[
        "targets"
    ][0]
    configuration.update(
        provider="eighth",
        model="config-only-service",
        account_id="fixture",
        secret_ref="FIXTURE_KEY",
        provider_quota_facts=[],
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [configuration]}))
    tmp_path.chmod(0o700)
    for name, value in (
        ("queue.key", QUEUE_KEY),
        ("digest.key", HMAC_KEY),
        ("client.token", TOKEN.encode()),
    ):
        path = tmp_path / name
        path.write_bytes(value)
        path.chmod(0o600)
    captured = {}

    class Server:
        def serve_forever(self):
            captured["started"] = True

        def server_close(self):
            captured["closed"] = True

    def factory(gateway, token, **kwargs):
        captured.update(gateway=gateway, token=token, **kwargs)
        return Server()

    monkeypatch.setattr(cli, "make_gateway_server", factory)
    monkeypatch.setattr(cli, "doppler_resolver", lambda *_: lambda _: "fixture-secret")
    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "gateway-serve",
            "--config",
            str(config),
            "--db",
            str(tmp_path / "private.sqlite"),
            "--digest-key-file",
            str(tmp_path / "digest.key"),
            "--client-token-file",
            str(tmp_path / "client.token"),
            "--doppler-token-file",
            str(tmp_path / "not-a-real-token"),
            "--doppler-project",
            "fixture-project",
            "--doppler-config",
            "fixture-config",
            "--registry-file",
            str(registry_file),
            "--queue-key-file",
            str(tmp_path / "queue.key"),
        ],
    )
    cli.main()
    assert captured["started"] and captured["closed"] and captured["worker"]
    assert captured["gateway"].registry.resolve("config-only-service").provider == "eighth"
    assert isinstance(captured["queue"], DurableQueue)
    assert (tmp_path / "private.sqlite").stat().st_mode & 0o777 == 0o600


def test_configured_remaining_enforces_same_atomic_admission(tmp_path):
    from quota_broker.config import Evidence, QuotaFact

    limit = Evidence(
        10, "observed", NOW, "fixture evidence", "fixture scope", NOW + timedelta(minutes=1)
    )
    remaining = replace(limit, value=1)
    configured = replace(
        target("a", "google", "gemini-2.5-flash-lite"),
        provider_quota_facts=(QuotaFact("requests", "day", limit, remaining),),
    )
    gateway, _, _ = build(tmp_path, [configured])
    request = {
        "request_key": "configured-one",
        "capability": "text_generation",
        "input_token_bound": 300,
        "max_output_tokens": 16,
    }
    first = gateway.broker.reserve(request)
    plan = gateway.explain(task())
    assert plan["selected_target_id"] is None and plan["temporary"]
    assert plan["candidates"][0]["quota_observations"][0]["effective_remaining"] == 0
    with pytest.raises(BrokerError):
        gateway.broker.reserve({**request, "request_key": "configured-two"})
    gateway.broker.cancel(first["reservation_id"])
    assert gateway.explain(task())["selected_target_id"] == "a"


def test_generic_task_diagnostics_do_not_invent_neurons_estimate(tmp_path):
    gateway, _, _ = build(tmp_path, [target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct")])
    assert "estimation_unconfigured" in gateway.diagnostics()["targets"][0]["reasons"]
    assert not gateway.catalog()[0]["available"]
