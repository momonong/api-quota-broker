"""Local v1 contracts. All provider responses are fixtures, never network calls."""

import base64
import hashlib
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gateway import HMAC_KEY, NOW, fixture_transport, target, task

from quota_broker.cli import gateway_cli, main
from quota_broker.config import (
    Capacity,
    ConfigError,
    SecretInventory,
    load_gateway_config,
    load_secret_inventory,
)
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.gateway_server import make_gateway_server

REPRESENTATIVES = (
    ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    ("google", "gemini-3.5-flash-lite"),
    ("cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
    ("groq", "openai/gpt-oss-20b"),
    ("mistral", "ministral-3b-latest"),
    ("openrouter", "liquid/lfm-2.5-2.6b:free"),
    ("ocrspace", "ocr.space/engine2"),
)
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture-image").decode()


def inventory(*names, expires=NOW + timedelta(minutes=5)):
    return SecretInventory(frozenset(names), NOW - timedelta(minutes=1), expires, "fixture", "dev")


def gateway_at(tmp_path, routes, calls, *, inv=None, resolve=None, transport=None, clock=None):
    return Gateway(
        tmp_path / "v1.db",
        tuple(routes),
        HMAC_KEY,
        resolve or (lambda _: "fixture-secret-value"),
        transport or fixture_transport(calls),
        clock=clock or (lambda: NOW),
        secret_inventory=inv,
    )


def test_example_matches_successful_representatives_without_account_claims():
    targets = load_gateway_config(Path(__file__).resolve().parents[1] / "gateway.example.json")
    assert set(REPRESENTATIVES) <= {(t.provider, t.model) for t in targets}
    assert all(not t.enabled and not t.free_eligible and t.verified_at is None for t in targets)
    small = next(t for t in targets if t.model == "mistral-small-latest")
    three = next(t for t in targets if t.model == "ministral-3b-latest")
    assert small.account_id == three.account_id and small.quotas == three.quotas
    assert small.shared_concurrency_scope == three.shared_concurrency_scope


@pytest.mark.parametrize("provider,model", REPRESENTATIVES)
def test_representative_can_route_without_provider_constraint(tmp_path, provider, model):
    calls = []
    gw = gateway_at(tmp_path, [target("route", provider, model)], calls, inv=inventory("ROUTE"))
    request = {**task("unconstrained"), "neuron_bound": 30}
    if provider == "ocrspace":
        request.update(capability="ocr", input=PNG, max_output_tokens=1, neuron_bound=None)
    plan = gw.explain(request)
    assert plan["selected_target_id"] == "route" and calls == []
    done = gw.run(request)
    assert done["state"] == "completed" and done["provider"] == provider
    assert done["ledger_basis"] == "settled_provider_usage"
    assert gw.run(request)["state"] == "completed" and len(calls) == 1
    listed = gw.recent(provider=provider)["tasks"][0]
    assert listed["request_key"] == request["request_key"] and "answer" not in listed
    assert listed["attempts"][0]["ledger_basis"] == "settled_provider_usage"
    assert gw.usage(provider=provider)[0]["requests"] == 1
    stored = Path(gw.db).read_bytes()
    assert b"fixture-secret-value" not in stored and b"fixture answer" not in stored
    assert request["input"].encode() not in stored


def test_capability_and_refresh_order_then_unknown_and_gift(tmp_path):
    def renewable(seconds):
        return Capacity(
            "short_renewable", seconds, NOW, "fixture", "fixture", NOW + timedelta(hours=1)
        )

    routes = [
        target("slow", "google", "gemini-3.5-flash-lite", capacity=renewable(86400), priority=-10),
        target(
            "gift",
            "mistral",
            "ministral-3b-latest",
            capacity=Capacity("one_time_gift", None, NOW, "fixture", "fixture"),
            priority=-20,
        ),
        target("unknown", "groq", "openai/gpt-oss-20b", priority=-30),
        target("fast", "nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b", capacity=renewable(60)),
        target(
            "translate", "nvidia", "nvidia/riva-translate-4b-instruct-v2", capacity=renewable(1)
        ),
        target("ocr", "ocrspace", "ocr.space/engine2", capacity=renewable(1)),
    ]
    calls = []
    gw = gateway_at(tmp_path, routes, calls)
    plan = gw.explain(task())
    assert [r["target_id"] for r in plan["candidates"] if r["eligible"]] == [
        "fast",
        "slow",
        "unknown",
        "gift",
    ]
    assert gw.run(task())["target_id"] == "fast"
    translated = gw.run(task("translate", capability="translation"))
    assert (
        translated["target_id"] == "translate"
        and calls[-1][2]["messages"][0]["content"] == "en-zh-cn"
    )
    ocr = gw.run({"request_key": "ocr", "capability": "ocr", "input": PNG})
    assert ocr["target_id"] == "ocr" and len(calls) == 3


def test_names_snapshot_readiness_is_read_only_and_expiry_is_unknown(tmp_path):
    calls, reads = [], []
    now = [NOW]
    routes = [
        replace(
            target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
            account_id_ref="CF_ACCOUNT",
        )
    ]
    gw = gateway_at(
        tmp_path,
        routes,
        calls,
        inv=inventory("CF"),
        resolve=lambda name: reads.append(name),
        clock=lambda: now[0],
    )
    digest = hashlib.sha256(Path(gw.db).read_bytes()).hexdigest()
    missing = gw.diagnostics()["targets"][0]
    assert missing["state"] == "blocked"
    assert missing["credentials"]["missing_names"] == ["CF_ACCOUNT"]
    assert "credential_name_missing" in missing["reasons"]
    assert gw.recent()["tasks"] == [] and gw.usage() == []
    assert calls == reads == [] and hashlib.sha256(Path(gw.db).read_bytes()).hexdigest() == digest
    now[0] += timedelta(minutes=6)
    stale = gw.diagnostics()["targets"][0]
    assert stale["state"] == "unknown" and stale["credentials"]["missing_names"] is None
    assert "credential_inventory_unknown" in stale["reasons"]


def test_present_names_do_not_validate_key_and_missing_names_skip_resolver(tmp_path):
    calls, reads = [], []
    routes = [
        target("missing", "groq", "openai/gpt-oss-20b"),
        target("present", "mistral", "ministral-3b-latest", priority=1),
    ]

    def resolve(name):
        reads.append(name)
        return "fixture-secret-value"

    gw = gateway_at(tmp_path, routes, calls, inv=inventory("PRESENT"), resolve=resolve)
    diagnostic = gw.diagnostics()
    assert diagnostic["ready_targets"] == 1 and reads == []
    assert gw.explain(task())["selected_target_id"] == "present"
    assert gw.run(task())["target_id"] == "present" and reads == ["PRESENT"] and len(calls) == 1


def test_free_evidence_and_cooldown_are_explainable(tmp_path):
    routes = [
        replace(
            target("disabled", "mistral", "ministral-3b-latest"),
            enabled=False,
            free_eligible=False,
            verified_at=None,
        ),
        target("limited", "google", "gemini-3.5-flash-lite"),
    ]
    calls = []

    def refuse(*_args):
        calls.append(1)
        return 429, {"Retry-After": "60"}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'

    gw = gateway_at(tmp_path, routes, calls, inv=inventory("DISABLED", "LIMITED"), transport=refuse)
    result = gw.run(task())
    assert result["state"] == "quota_exhausted"
    rows = {row["target_id"]: row for row in gw.diagnostics()["targets"]}
    assert {"disabled", "free_eligibility_unverified", "free_evidence_missing"} <= set(
        rows["disabled"]["reasons"]
    )
    assert (
        "cooldown" in rows["limited"]["reasons"] and rows["limited"]["cooldown_until"] is not None
    )
    assert rows["limited"]["state"] == "blocked" and len(calls) == 1


def test_recent_stable_pages_and_filters_final_target(tmp_path):
    calls = []
    gw = gateway_at(tmp_path, [target("google", "google", "gemini-3.5-flash-lite")], calls)
    for key in ("a", "c", "b"):
        gw.run(task(key))
    first = gw.recent(limit=2)
    assert [row["request_key"] for row in first["tasks"]] == ["c", "b"]
    assert first["next_before"] == "b"
    second = gw.recent(limit=2, before=first["next_before"])
    assert [row["request_key"] for row in second["tasks"]] == ["a"] and second[
        "next_before"
    ] is None
    assert gw.recent(provider="groq")["tasks"] == []
    assert len(gw.recent(model="gemini-3.5-flash-lite", state="completed")["tasks"]) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"before": "bad/key"},
        {"provider": "bad"},
        {"model": "bad"},
        {"state": "bad"},
    ],
)
def test_recent_invalid_filters_fail_closed(tmp_path, kwargs):
    gw = gateway_at(tmp_path, [], [])
    with pytest.raises(GatewayError) as exc:
        gw.recent(**kwargs)
    assert exc.value.code == "invalid_request"


@pytest.mark.parametrize("ledger_state", ["dispatched", "unknown", "quota_rejected"])
def test_restart_crash_gap_list_filter_matches_status_and_never_replays(tmp_path, ledger_state):
    calls = []
    route = target("google", "google", "gemini-3.5-flash-lite")
    gw = gateway_at(tmp_path, [route], calls)
    request = task("gap")
    reservation = gw.broker.reserve(
        {
            "request_key": "gw:gap:0",
            "capability": "text_generation",
            "input_token_bound": 308,
            "max_output_tokens": 16,
        }
    )
    rid = reservation["reservation_id"]
    gw.broker.dispatch(rid)
    if ledger_state != "dispatched":
        gw.broker.report(
            {
                "reservation_id": rid,
                "report_key": "crash",
                "state": ledger_state,
                "error_status": 429 if ledger_state == "quota_rejected" else None,
            }
        )
    with sqlite3.connect(gw.db) as con:
        con.execute(
            "INSERT INTO gateway_tasks(request_key,payload_hmac,state,reservation_id,target_id,provider,model,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "gap",
                gw._hmac(request),
                "preparing",
                rid,
                route.id,
                route.provider,
                route.model,
                NOW.isoformat(),
            ),
        )
    restarted = gateway_at(tmp_path, [route], calls)
    expected = "quota_rejected" if ledger_state == "quota_rejected" else "unknown"
    assert restarted.status("gap")["state"] == expected
    assert restarted.recent(state=expected)["tasks"][0]["state"] == expected
    assert restarted.recent(state="preparing")["tasks"] == []
    assert restarted.run(request)["state"] == expected and calls == []


def test_legacy_gateway_columns_are_migrated_without_rewriting_metadata(tmp_path):
    gw = gateway_at(tmp_path, [], [])
    with sqlite3.connect(gw.db) as con:
        con.execute(
            "INSERT INTO gateway_tasks(request_key,payload_hmac,state,created_at) VALUES('legacy','hmac','unknown',?)",
            (NOW.isoformat(),),
        )
        for table in ("gateway_tasks", "gateway_attempts"):
            for column in ("finish_reason", "response_truncated", "diagnostics_json"):
                con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    restarted = gateway_at(tmp_path, [], [])
    state = restarted.recent(state="unknown")["tasks"][0]
    assert (
        state["request_key"] == "legacy"
        and state["finish_reason"] is None
        and state["diagnostics"] is None
    )
    with sqlite3.connect(gw.db) as con:
        assert con.execute("SELECT payload_hmac FROM gateway_tasks").fetchone()[0] == "hmac"


@pytest.mark.parametrize("provider,model", REPRESENTATIVES)
def test_all_provider_errors_expose_safe_categories_not_prose(tmp_path, provider, model):
    calls = []

    def forbidden(*_args):
        calls.append(1)
        return (
            403,
            {"Set-Cookie": "fixture-secret-value"},
            b'{"error":{"message":"fixture-secret-value fixture input private account","type":"opaque"}}',
        )

    gw = gateway_at(tmp_path, [target("route", provider, model)], calls, transport=forbidden)
    request = task(neurons=30)
    if provider == "ocrspace":
        request.update(capability="ocr", input=PNG, max_output_tokens=1, neuron_bound=None)
    result = gw.run(request)
    assert result["state"] == "unknown" and len(calls) == 1
    assert result["diagnostics"]["http_category"] == "access_rejected"
    assert result["diagnostics"]["non_execution_quota_proven"] is False
    serialized = json.dumps(gw.recent())
    for forbidden in ("fixture-secret-value", "fixture input", "private account", "Set-Cookie"):
        assert forbidden not in serialized and forbidden.encode() not in Path(gw.db).read_bytes()


@pytest.mark.parametrize("mutate", ["scope", "secret_value", "duplicate", "bad_expiry", "extra"])
def test_inventory_rejects_unscoped_values_and_invalid_metadata(tmp_path, mutate):
    data = {
        "project": "fixture",
        "config": "dev",
        "names": ["GROQ_API_KEY"],
        "verified_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if mutate == "scope":
        data["project"] = "other"
    elif mutate == "secret_value":
        data["names"] = ["gsk-fixture-value"]
    elif mutate == "duplicate":
        data["names"] *= 2
    elif mutate == "bad_expiry":
        data["expires_at"] = data["verified_at"]
    else:
        data["values"] = {"GROQ_API_KEY": "fixture-secret-value"}
    file = tmp_path / "names.json"
    file.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        load_secret_inventory(file, "fixture", "dev")


def test_inventory_loads_names_only(tmp_path):
    file = tmp_path / "names.json"
    file.write_text(
        json.dumps(
            {
                "project": "fixture",
                "config": "dev",
                "names": ["GROQ_API_KEY"],
                "verified_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
        )
    )
    loaded = load_secret_inventory(file, "fixture", "dev")
    assert loaded.current(NOW) and loaded.names == frozenset({"GROQ_API_KEY"})


def test_http_cli_fallback_queries_auth_and_no_query_side_effects(tmp_path, monkeypatch, capsys):
    calls, reads = [], []
    routes = [
        target("cf", "cloudflare", "@cf/meta/llama-3.2-1b-instruct"),
        target("google", "google", "gemini-3.5-flash-lite", priority=1),
    ]

    def resolve(name):
        reads.append(name)
        return "fixture-secret-value"

    def transport(url, headers, payload, timeout):
        calls.append(url)
        if "cloudflare" in url:
            return (
                429,
                {"Retry-After": "120"},
                b'{"success":false,"errors":[{"code":3036}],"result":null}',
            )
        return fixture_transport([])(url, headers, payload, timeout)

    gw = gateway_at(
        tmp_path, routes, calls, inv=inventory("CF", "GOOGLE"), resolve=resolve, transport=transport
    )
    token = "fixture-client-authentication-more-than-32-characters"
    server = make_gateway_server(gw, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path, authenticated=True):
        request = urllib.request.Request(
            base + path, headers={"Authorization": "Bearer " + token} if authenticated else {}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    try:
        for path in ("/v1/diagnostics", "/v1/tasks", "/v1/usage"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                get(path, False)
            assert exc.value.code == 401
        request = urllib.request.Request(
            base + "/v1/tasks",
            data=json.dumps(task("http-fallback", neurons=30)).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            done = json.load(response)
        assert [a["state"] for a in done["attempts"]] == ["quota_rejected", "completed"]
        assert len(calls) == 2
        # provider filters use the final selected target; every attempt is still visible.
        assert get("/v1/tasks?provider=cloudflare")["tasks"] == []
        digest = hashlib.sha256(Path(gw.db).read_bytes()).hexdigest()
        for action in ("diagnostics", "recent", "status", "usage"):
            monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
            monkeypatch.setattr(
                "sys.argv",
                ["quota-broker", "gateway", "--url", base, "--token-stdin", "--json", action]
                + (["http-fallback"] if action == "status" else []),
            )
            main()
            out = capsys.readouterr().out
            assert "fixture-secret-value" not in out and "fixture input" not in out
            if action == "status":
                assert "answer" not in json.loads(out)
            if action == "recent":
                rows = json.loads(out)["tasks"]
                assert len(rows[0]["attempts"]) == 2
            if action == "usage":
                by_provider = {r["provider"]: r for r in json.loads(out)}
                assert by_provider["cloudflare"]["quota_rejected_count"] == 1
                assert by_provider["google"]["ledger_input_tokens"] == 11
        assert len(calls) == len(reads) == 2
        assert hashlib.sha256(Path(gw.db).read_bytes()).hexdigest() == digest
        for query in (
            "limit=101",
            "limit=1&limit=2",
            "state=no",
            "extra=1",
            "limit=",
            "before=bad%2Fkey",
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                get("/v1/tasks?" + query)
            assert exc.value.code == 400
        args = SimpleNamespace(
            url=base,
            token_file=None,
            token_stdin=True,
            json=False,
            action="recent",
            limit=20,
            before=None,
            provider=None,
            model=None,
            state=None,
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
        gateway_cli(args)
        assert "attempts=2" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_queries_never_expire_unsent_reservations(tmp_path):
    calls, reads = [], []
    now = [NOW]
    route = target("google", "google", "gemini-3.5-flash-lite")
    gw = gateway_at(
        tmp_path, [route], calls, resolve=lambda ref: reads.append(ref), clock=lambda: now[0]
    )
    reserved = gw.broker.reserve(
        {
            "request_key": "gw:unsent:0",
            "capability": "text_generation",
            "input_token_bound": 308,
            "max_output_tokens": 16,
        }
    )
    with sqlite3.connect(gw.db) as con:
        con.execute(
            "INSERT INTO gateway_tasks(request_key,payload_hmac,state,reservation_id,target_id,provider,model,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "unsent",
                gw._hmac(task("unsent")),
                "preparing",
                reserved["reservation_id"],
                route.id,
                route.provider,
                route.model,
                NOW.isoformat(),
            ),
        )
    now[0] += timedelta(seconds=31)
    digest = hashlib.sha256(Path(gw.db).read_bytes()).hexdigest()
    assert gw.status("unsent")["ledger_state"] == "reserved"
    assert gw.recent()["tasks"][0]["ledger_state"] == "reserved"
    assert gw.usage() == []
    gw.diagnostics()
    assert hashlib.sha256(Path(gw.db).read_bytes()).hexdigest() == digest
    assert calls == reads == []
    # Historical broker API keeps its expiration behavior, separate from Gateway queries.
    assert gw.broker.status(reserved["reservation_id"])["state"] == "expired"
