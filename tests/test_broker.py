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
        else (
            None if state in {"unknown", "quota_rejected"} else {"requests": 1, "input_tokens": 40}
        ),
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


def test_quota_rejection_requires_429_and_releases_all_held_metrics(tmp_path):
    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    broker = Broker(tmp_path / "quota.db", (target(clock, rpm=1),), clock)
    reservation = broker.reserve(request("quota"))
    broker.dispatch(reservation["reservation_id"])
    bad = report(reservation, "bad", state="quota_rejected", usage=None, error_status=400)
    with pytest.raises(BrokerError) as exc:
        broker.report(bad)
    assert exc.value.code == "invalid_request"
    assert broker.status(reservation["reservation_id"])["state"] == "dispatched"
    good = report(
        reservation,
        "good",
        state="quota_rejected",
        usage=None,
        error_status=429,
        retry_after_seconds=60,
    )
    result = broker.report(good)
    assert result["state"] == "quota_rejected"
    assert all(charge["amount"] == 0 for charge in result["charges"])
    assert broker.report(good)["state"] == "quota_rejected"
    with pytest.raises(BrokerError):
        broker.reserve(request("during"))
    clock.advance(seconds=61)
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
    raw["targets"][0]["local_safety_caps"] = raw["targets"][0]["local_safety_caps"][:2]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="local safety caps require"):
        load_config(bad)


def test_shared_concurrency_across_targets_is_atomic(tmp_path):
    from dataclasses import replace

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    base = target(clock, rpm=50, tpm=100000, rpd=100, concurrent=2)
    one = replace(
        base,
        id="one",
        shared_concurrency_scope="google:project:account123",
        shared_concurrency_limit=1,
    )
    two = replace(
        base,
        id="two",
        priority=1,
        quotas=tuple(replace(q, bucket=q.bucket + "-two") for q in base.quotas),
        shared_concurrency_scope="google:project:account123",
        shared_concurrency_limit=1,
    )
    broker = Broker(tmp_path / "state.db", (one, two), clock)
    barrier = threading.Barrier(10)

    def compete(i):
        barrier.wait()
        try:
            return broker.reserve(request(f"shared-{i}"))
        except BrokerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(compete, range(10)))
    wins = [item for item in outcomes if isinstance(item, dict)]
    assert len(wins) == 1
    assert outcomes.count("unavailable") == 9
    winner = wins[0]
    restarted = Broker(tmp_path / "state.db", (one, two), clock)
    with pytest.raises(BrokerError, match="no verified free capacity"):
        restarted.reserve(request("after-restart"))
    broker.dispatch(winner["reservation_id"])
    broker.report(report(winner, "shared-settle"))
    assert broker.reserve(request("after-shared"))["state"] == "reserved"


def test_same_id_config_drift_cannot_reroute_unsent_or_erase_sent(tmp_path):
    from dataclasses import replace

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    db = tmp_path / "state.db"
    original = target(clock)
    broker = Broker(db, (original,), clock)
    unsent = broker.reserve(request("unsent"))
    sent = broker.reserve(request("sent"))
    broker.dispatch(sent["reservation_id"])
    broker.report(report(sent, "lost", state="unknown"))

    changed = replace(
        original,
        provider="cloudflare",
        model="@cf/meta/llama-3.2-1b-instruct",
        quotas=(
            Quota("new-neurons", "neurons", 100, "day"),
            Quota("new-rpm", "requests", 10, "rolling_minute"),
        ),
    )
    restarted = Broker(db, (changed,), clock)
    old = restarted.status(unsent["reservation_id"])
    assert old["model"] == "gemini-2.5-flash-lite"
    assert old["endpoint"] is None
    assert old["route_current"] is False
    with pytest.raises(BrokerError) as exc:
        restarted.dispatch(unsent["reservation_id"])
    assert exc.value.code == "configuration_changed"
    with pytest.raises(BrokerError, match="no verified free capacity"):
        restarted.reserve(request("new-request", neurons=20))

    unknown = restarted.status(sent["reservation_id"])
    assert unknown["state"] == "unknown"
    assert unknown["model"] == "gemini-2.5-flash-lite"
    assert "generativelanguage.googleapis.com" in unknown["endpoint"]
    assert unknown["route_current"] is False
    assert unknown["original_quotas"]
    assert {charge["metric"] for charge in unknown["charges"]} == {"requests", "input_tokens"}
    settled = restarted.report(
        report(sent, "reconciled", usage={"requests": 1, "input_tokens": 11})
    )
    assert settled["state"] == "completed"
    removed = Broker(db, (), clock)
    assert removed.status(sent["reservation_id"])["provider_request_id"] is None
    assert removed.status(sent["reservation_id"])["model"] == "gemini-2.5-flash-lite"


def test_legacy_sqlite_route_is_unknown_and_dispatched_can_reconcile(tmp_path):
    import sqlite3

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE reservations (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,"
            "fingerprint TEXT NOT NULL,target_id TEXT NOT NULL,state TEXT NOT NULL,"
            "created_at TEXT NOT NULL,expires_at TEXT NOT NULL,dispatched_at TEXT,"
            "provider_request_id TEXT,error_status INTEGER)"
        )
        con.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "old",
                "old-key",
                "old-fingerprint",
                "target",
                "dispatched",
                clock.now.isoformat(),
                (clock.now + timedelta(minutes=1)).isoformat(),
                clock.now.isoformat(),
                None,
                None,
            ),
        )
        con.execute(
            "CREATE TABLE charges (reservation_id TEXT,bucket TEXT,metric TEXT,"
            "amount INTEGER,at TEXT,day_start TEXT,PRIMARY KEY(reservation_id,bucket))"
        )
        con.execute(
            "INSERT INTO charges VALUES(?,?,?,?,?,?)",
            ("old", "old-rpm", "requests", 1, clock.now.isoformat(), None),
        )
    broker = Broker(db, (target(clock),), clock)
    status = broker.status("old")
    assert status["state"] == "dispatched"
    assert status["route_evidence"] == "legacy_missing"
    assert status["endpoint"] is None
    with pytest.raises(BrokerError, match="legacy active"):
        broker.reserve(request("new"))
    assert (
        broker.report(
            {
                "reservation_id": "old",
                "report_key": "legacy-report",
                "state": "completed",
                "usage": {"requests": 1},
            }
        )["state"]
        == "completed"
    )
    assert broker.reserve(request("new"))["state"] == "reserved"


def test_target_limit_still_applies_with_shared_scope(tmp_path):
    from dataclasses import replace

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    base = target(clock, rpm=50, tpm=100000, rpd=100, concurrent=1)
    one = replace(
        base, id="one", shared_concurrency_scope="project-shared", shared_concurrency_limit=3
    )
    two = replace(
        base,
        id="two",
        priority=1,
        concurrency_limit=2,
        quotas=tuple(replace(q, bucket=q.bucket + "-two") for q in base.quotas),
        shared_concurrency_scope="project-shared",
        shared_concurrency_limit=3,
    )
    broker = Broker(tmp_path / "state.db", (one, two), clock)
    assert broker.reserve(request("a"))["target_id"] == "one"
    assert broker.reserve(request("b"))["target_id"] == "two"
    assert broker.reserve(request("c"))["target_id"] == "two"
    with pytest.raises(BrokerError, match="no verified free capacity"):
        broker.reserve(request("d"))


def test_quota_only_drift_invalidates_unsent_route(tmp_path):
    from dataclasses import replace

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    db = tmp_path / "state.db"
    original = target(clock)
    plan = Broker(db, (original,), clock).reserve(request("quota-drift"))
    changed = replace(
        original,
        quotas=tuple(replace(q, limit=q.limit + 1) for q in original.quotas),
    )
    restarted = Broker(db, (changed,), clock)
    assert restarted.status(plan["reservation_id"])["route_current"] is False
    assert restarted.status(plan["reservation_id"])["endpoint"] is None
    with pytest.raises(BrokerError) as exc:
        restarted.dispatch(plan["reservation_id"])
    assert exc.value.code == "configuration_changed"


def test_local_caps_and_unknown_provider_facts_do_not_claim_official_quota(tmp_path):
    from pathlib import Path

    from quota_broker.config import load_config

    now = datetime.now(UTC)
    raw = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
    item = raw["targets"][0]
    item["local_safety_caps"][0]["limit"] = 2
    item["local_safety_caps"][1]["limit"] = 1000
    item["local_safety_caps"][2]["limit"] = 2
    item["capacity"] = {
        "kind": "unknown",
        "refresh_seconds": None,
        "as_of": None,
        "source": None,
        "scope": None,
        "expires_at": None,
    }
    unknown = {
        "value": None,
        "provenance": "unknown",
        "as_of": None,
        "source": None,
        "scope": None,
        "valid_until": None,
    }
    item["provider_quota_facts"] = [
        {"metric": "requests", "window": "day", "limit": unknown, "remaining": unknown}
    ]
    item["enabled"] = True
    item["free_eligible"] = True
    item["verified_at"] = now.isoformat()
    item["expires_at"] = (now + timedelta(hours=1)).isoformat()
    item["source"] = "fixture free-eligibility evidence"
    raw["targets"] = [item]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    loaded = load_config(path)
    catalog = Broker(tmp_path / "broker.db", loaded).catalog()[0]
    assert catalog["quota_basis"] == "local_safety_cap"
    assert catalog["quotas"] == []
    assert catalog["local_safety_caps"][0]["limit"] == 2
    assert catalog["provider_quota_facts"][0]["remaining"] == unknown
    assert catalog["available"] is True


def test_capacity_order_and_current_official_zero(tmp_path):
    from dataclasses import replace

    from quota_broker.config import Capacity, Evidence, QuotaFact

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    base = target(clock, rpm=20, rpd=20, tpm=10000)

    def variant(name, kind, priority, seconds=None, facts=()):
        return replace(
            base,
            id=name,
            priority=priority,
            capacity=Capacity(kind, seconds, clock.now, "fixture", "account")
            if kind != "unknown"
            else Capacity(),
            provider_quota_facts=facts,
            quotas=tuple(replace(q, bucket=q.bucket + "-" + name) for q in base.quotas),
        )

    fresh = Evidence(
        0,
        "official",
        clock.now,
        "provider account page",
        "account",
        clock.now + timedelta(minutes=5),
    )
    observed = Evidence(
        0, "observed", clock.now, "manual note", "account", clock.now + timedelta(minutes=5)
    )
    stale = Evidence(
        0,
        "official",
        clock.now - timedelta(days=1),
        "provider account page",
        "account",
        clock.now - timedelta(hours=1),
    )
    fact = lambda value: (QuotaFact("requests", "day", value, value),)
    assert not variant("blocked", "unknown", 0, facts=fact(fresh)).available(clock.now)
    assert variant("observed", "unknown", 0, facts=fact(observed)).available(clock.now)
    assert variant("stale", "unknown", 0, facts=fact(stale)).available(clock.now)
    candidates = (
        variant("gift", "one_time_gift", -20),
        variant("unknown", "unknown", -10),
        variant("slow", "short_renewable", 0, 3600),
        variant("fast", "short_renewable", 10, 60),
    )
    broker = Broker(tmp_path / "order.db", candidates, clock)
    assert broker.reserve(request("ranked"))["target_id"] == "fast"


def test_retry_after_http_date_and_persistent_fallback_backoff(tmp_path):
    import sqlite3

    from quota_broker.retry import parse_retry_after

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    assert parse_retry_after("90", clock.now) == 90
    assert parse_retry_after("Fri, 25 Sep 2026 12:02:00 GMT", clock.now) == 120
    assert parse_retry_after("invalid", clock.now) is None
    assert parse_retry_after("9" * 10000, clock.now) is None
    broker = Broker(tmp_path / "backoff.db", (target(clock, rpm=20, rpd=20, tpm=10000),), clock)
    first = broker.reserve(request("first"))
    broker.dispatch(first["reservation_id"])
    broker.report(report(first, "rate-1", state="failed", error_status=429))
    with sqlite3.connect(broker.db) as con:
        assert con.execute("SELECT backoff_level FROM cooldowns").fetchone()[0] == 1
    clock.advance(seconds=61)
    restarted = Broker(tmp_path / "backoff.db", (target(clock, rpm=20, rpd=20, tpm=10000),), clock)
    second = restarted.reserve(request("second"))
    restarted.dispatch(second["reservation_id"])
    restarted.report(report(second, "rate-2", state="failed", error_status=429))
    with sqlite3.connect(broker.db) as con:
        until, level = con.execute("SELECT until_at,backoff_level FROM cooldowns").fetchone()
    assert level == 2
    assert until == (clock.now + timedelta(seconds=120)).isoformat()


def test_existing_cooldown_table_migrates_without_losing_deadline(tmp_path):
    import sqlite3

    clock = Clock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    db = tmp_path / "old-cooldown.db"
    deadline = (clock.now + timedelta(minutes=3)).isoformat()
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE cooldowns(target_id TEXT PRIMARY KEY, until_at TEXT NOT NULL)")
        con.execute("INSERT INTO cooldowns VALUES(?,?)", ("google:test", deadline))
    broker = Broker(db, (target(clock),), clock)
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT until_at,backoff_level FROM cooldowns").fetchone()
    assert row == (deadline, 0)
    assert broker.catalog()[0]["available"] is True


def test_direct_client_http_date_429_sets_cooldown_without_replay(tmp_path):
    from email.utils import format_datetime

    clock = Clock(datetime.now(UTC))
    broker = Broker(tmp_path / "date-429.db", (target(clock, rpm=20, rpd=20),), clock)
    server = make_server(broker, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def provider(_url, _headers, _payload, _timeout):
        calls.append(1)
        return (
            429,
            {
                "Retry-After": format_datetime(
                    datetime.now(UTC) + timedelta(seconds=120), usegmt=True
                )
            },
            b"rate limited",
        )

    try:
        client = DirectClient(f"http://127.0.0.1:{server.server_port}", provider_transport=provider)
        result = client.run_text("hi", "date-429", "fixture-key", model="gemini-2.5-flash-lite")
        assert result["state"] == "unknown"
        with pytest.raises(BrokerError) as exc:
            broker.reserve(request("during-date-cooldown"))
        assert exc.value.code == "unavailable"
        assert (
            115 <= (datetime.fromisoformat(exc.value.wait_until) - clock.now).total_seconds() <= 121
        )
        with pytest.raises(ClientError, match="will not be repeated"):
            client.run_text("hi", "date-429", "fixture-key", model="gemini-2.5-flash-lite")
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
