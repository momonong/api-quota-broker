import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from quota_broker.client import ClientError, DirectClient, _check_official
from quota_broker.config import Quota, Target
from quota_broker.core import Broker, BrokerError, day_bounds
from quota_broker.server import make_server


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def target(
    clock,
    *,
    provider="google",
    rpm=2,
    tpm=1000,
    rpd=10,
    concurrent=3,
    billing=False,
    eligible=True,
    verified=True,
):
    if provider == "google":
        quotas = (
            Quota("project-rpm", "requests", rpm, "rolling_minute"),
            Quota("project-tpm", "input_tokens", tpm, "rolling_minute"),
            Quota("project-rpd", "requests", rpd, "day", "America/Los_Angeles"),
        )
        model = "gemini-2.5-flash-lite"
    else:
        quotas = (
            Quota("account-neurons", "neurons", 100, "day"),
            Quota("account-rpm", "requests", rpm, "rolling_minute"),
        )
        model = "@cf/meta/llama-3.2-1b-instruct"
    return Target(
        id="target",
        provider=provider,
        model=model,
        account_id="account123",
        enabled=True,
        free_eligible=eligible,
        billing_enabled=billing,
        verified_at=clock.now - timedelta(minutes=1) if verified else None,
        expires_at=clock.now + timedelta(hours=4) if verified else None,
        quotas=quotas,
        concurrency_limit=concurrent,
        max_output_tokens=256,
        priority=0,
        source="test account fact",
    )


def request(key, *, model=None, input_bound=100, output_max=20, neurons=None):
    return {
        "request_key": key,
        "capability": "text_generation",
        "model": model,
        "input_token_bound": input_bound,
        "max_output_tokens": output_max,
        "neuron_bound": neurons,
    }


def report(reservation, key, *, state="completed", usage=None, **extra):
    return {
        "reservation_id": reservation["reservation_id"],
        "report_key": key,
        "state": state,
        "usage": usage
        if usage is not None
        else (None if state == "unknown" else {"requests": 1, "input_tokens": 40}),
        **extra,
    }


def test_atomic_competition_and_restart(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    db = tmp_path / "state.db"
    broker = Broker(db, (target(clock, rpm=1),), clock)
    barrier = threading.Barrier(12)

    def compete(i):
        barrier.wait()
        try:
            return broker.reserve(request(f"request-{i}"))["state"]
        except BrokerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(compete, range(12)))
    assert outcomes.count("reserved") == 1
    assert outcomes.count("unavailable") == 11
    restarted = Broker(db, (target(clock, rpm=1),), clock)
    with pytest.raises(BrokerError, match="no verified free capacity"):
        restarted.reserve(request("next"))


def test_rolling_window_and_pacific_reset(tmp_path):
    clock = Clock(datetime(2026, 3, 8, 7, 59, tzinfo=UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, rpm=1, rpd=1),), clock)
    first = broker.reserve(request("first"))
    broker.dispatch(first["reservation_id"])
    broker.report(report(first, "first-report"))
    with pytest.raises(BrokerError) as exc:
        broker.reserve(request("second"))
    assert exc.value.wait_until is not None
    clock.advance(minutes=2)
    # Pacific midnight on DST transition day occurs at 08:00 UTC.
    assert day_bounds(clock.now, "America/Los_Angeles")[0] == datetime(2026, 3, 8, 8, tzinfo=UTC)
    assert broker.reserve(request("second"))["state"] == "reserved"


def test_unsent_expiry_vs_dispatched_unknown(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, rpm=1, concurrent=1),), clock)
    unsent = broker.reserve(request("unsent"))
    clock.advance(seconds=31)
    assert broker.status(unsent["reservation_id"])["state"] == "expired"
    sent = broker.reserve(request("sent"))
    broker.dispatch(sent["reservation_id"])
    clock.advance(minutes=2)
    broker.report(report(sent, "unknown-report", state="unknown", usage=None))
    assert broker.status(sent["reservation_id"])["state"] == "unknown"
    with pytest.raises(BrokerError, match="no verified free capacity"):
        broker.reserve(request("later"))
    broker.report(report(sent, "reconcile", usage={"requests": 1, "input_tokens": 90}))
    assert broker.reserve(request("later"))["state"] == "reserved"


def test_settlement_difference_idempotency_conflicts_and_overage(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, tpm=150),), clock)
    first = broker.reserve(request("first", input_bound=100))
    assert broker.reserve(request("first", input_bound=100)) == first
    with pytest.raises(BrokerError) as exc:
        broker.reserve(request("first", input_bound=101))
    assert exc.value.code == "conflict"
    broker.dispatch(first["reservation_id"])
    with pytest.raises(BrokerError) as exc:
        broker.dispatch(first["reservation_id"])
    assert exc.value.code == "invalid_transition"
    settlement = report(first, "settle", usage={"requests": 1, "input_tokens": 40})
    assert broker.report(settlement)["state"] == "completed"
    assert broker.report(settlement)["state"] == "completed"
    with pytest.raises(BrokerError) as exc:
        broker.report({**settlement, "usage": {"requests": 1, "input_tokens": 41}})
    assert exc.value.code == "conflict"
    second = broker.reserve(request("second", input_bound=100))
    broker.dispatch(second["reservation_id"])
    broker.report(report(second, "overage", usage={"requests": 1, "input_tokens": 130}))
    with pytest.raises(BrokerError):
        broker.reserve(request("third", input_bound=1))


def test_429_cooldown_and_free_fail_closed(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    for change in ({"billing": True}, {"eligible": False}, {"verified": False}):
        broker = Broker(tmp_path / (str(change) + ".db"), (target(clock, **change),), clock)
        with pytest.raises(BrokerError) as exc:
            broker.reserve(request("never"))
        assert exc.value.code == "unavailable"
    broker = Broker(tmp_path / "cooldown.db", (target(clock),), clock)
    first = broker.reserve(request("first"))
    broker.dispatch(first["reservation_id"])
    broker.report(
        report(first, "rate", state="unknown", usage=None, error_status=429, retry_after_seconds=90)
    )
    with pytest.raises(BrokerError) as exc:
        broker.reserve(request("during"))
    assert exc.value.wait_until == (clock.now + timedelta(seconds=90)).isoformat()
    clock.advance(seconds=91)
    assert broker.reserve(request("after"))["state"] == "reserved"


def test_direct_client_fixture_and_no_replay(tmp_path):
    clock = Clock(datetime.now(UTC))
    broker = Broker(tmp_path / "state.db", (target(clock),), clock)
    server = make_server(broker, port=0, token="broker-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def provider(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return (
            200,
            {},
            json.dumps(
                {"responseId": "provider-1", "usageMetadata": {"promptTokenCount": 5}}
            ).encode(),
        )

    try:
        client = DirectClient(f"http://127.0.0.1:{server.server_port}", "broker-only", provider)
        result = client.run_text(
            "secret prompt", "same-key", "provider-secret", model="gemini-2.5-flash-lite"
        )
        assert result["state"] == "completed"
        assert len(calls) == 1
        assert calls[0][1] == {"x-goog-api-key": "provider-secret"}
        assert calls[0][2]["contents"][0]["parts"][0]["text"] == "secret prompt"
        with pytest.raises(ClientError, match="will not be repeated"):
            client.run_text(
                "secret prompt", "same-key", "provider-secret", model="gemini-2.5-flash-lite"
            )
        assert len(calls) == 1
        db_bytes = (tmp_path / "state.db").read_bytes()
        assert b"secret prompt" not in db_bytes
        assert b"provider-secret" not in db_bytes
        assert b"broker-only" not in db_bytes
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cloudflare_neurons_and_origin_guard(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, provider="cloudflare"),), clock)
    with pytest.raises(BrokerError, match="Neurons bound"):
        broker.reserve(request("no-bound"))
    reserved = broker.reserve(request("bounded", neurons=70))
    assert reserved["state"] == "reserved"
    with pytest.raises(ClientError, match="unapproved provider origin"):
        _check_official({**reserved, "endpoint": "https://evil.example/secret"})
    broker.dispatch(reserved["reservation_id"])
    broker.report(report(reserved, "unknown", state="unknown", usage=None))
    assert broker.status(reserved["reservation_id"])["state"] == "unknown"


def test_dispatch_rechecks_new_day_after_reservation(tmp_path):
    clock = Clock(datetime(2026, 3, 8, 7, 59, 50, tzinfo=UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, rpm=3, rpd=1),), clock)
    old_window = broker.reserve(request("old-window"))
    clock.advance(seconds=11)
    new_window = broker.reserve(request("new-window"))
    broker.dispatch(new_window["reservation_id"])
    with pytest.raises(BrokerError, match="quota changed before dispatch"):
        broker.dispatch(old_window["reservation_id"])
    assert broker.status(old_window["reservation_id"])["state"] == "reserved"


def test_cloudflare_direct_fixture_keeps_unmetered_call_unknown(tmp_path):
    clock = Clock(datetime.now(UTC))
    broker = Broker(tmp_path / "state.db", (target(clock, provider="cloudflare"),), clock)
    server = make_server(broker, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def provider(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return 200, {}, json.dumps({"result": {"response": "fixture"}}).encode()

    try:
        client = DirectClient(f"http://127.0.0.1:{server.server_port}", provider_transport=provider)
        sent_to_broker = []
        original = client._broker

        def capture(path, body=None):
            sent_to_broker.append((path, body))
            return original(path, body)

        client._broker = capture
        result = client.run_text(
            "private text",
            "cf-key",
            "provider-secret",
            model="@cf/meta/llama-3.2-1b-instruct",
            neuron_bound=50,
        )
        assert result["state"] == "unknown"
        assert len(calls) == 1
        assert calls[0][1] == {"Authorization": "Bearer provider-secret"}
        assert calls[0][2]["prompt"] == "private text"
        assert all(
            "private text" not in str(body) and "provider-secret" not in str(body)
            for _, body in sent_to_broker
        )
        with pytest.raises(ClientError, match="will not be repeated"):
            client.run_text(
                "private text",
                "cf-key",
                "provider-secret",
                model="@cf/meta/llama-3.2-1b-instruct",
                neuron_bound=50,
            )
        assert len(calls) == 1
        restarted = Broker(tmp_path / "state.db", (target(clock, provider="cloudflare"),), clock)
        assert restarted.status(result["reservation_id"])["state"] == "unknown"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_expired_verification_blocks_dispatch(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    fact = target(clock)
    fact = Target(**{**fact.__dict__, "expires_at": clock.now + timedelta(seconds=5)})
    broker = Broker(tmp_path / "state.db", (fact,), clock)
    plan = broker.reserve(request("plan"))
    clock.advance(seconds=6)
    with pytest.raises(BrokerError, match="eligibility no longer verified"):
        broker.dispatch(plan["reservation_id"])


def test_example_config_is_disabled_and_missing_quota_fails(tmp_path):
    from pathlib import Path

    from quota_broker.config import ConfigError, load_config

    example = Path(__file__).resolve().parents[1] / "config.example.json"
    facts = load_config(example)
    assert len(facts) == 2
    assert not any(fact.available(datetime.now(UTC)) for fact in facts)
    raw = json.loads(example.read_text())
    raw["targets"][0]["quotas"] = raw["targets"][0]["quotas"][:2]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="RPD"):
        load_config(bad)
