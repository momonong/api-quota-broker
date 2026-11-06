"""Scoped HTTP authorization exercises only fixture transports and temporary state."""

import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_gateway import NOW, make_gateway, target, task

from quota_broker.config import Evidence, QuotaFact
from quota_broker.gateway_server import make_gateway_server
from quota_broker.queue import DurableQueue, prepare_queue_storage

LEGACY = "fixture-legacy-client-token-more-than-32-characters"
TOKENS = {
    name: f"fixture-{name}-scoped-token-more-than-thirty-two-characters"
    for name in ("web", "a", "b")
}


def policy_file(tmp_path, *, inflight=2):
    path = tmp_path / "access.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": [
                    {
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "project_id": None if name == "web" else name,
                        "scopes": ["read", "plan"]
                        if name == "web"
                        else ["read", "plan", "execute"],
                        "limits": {
                            "max_input_bytes": 1024,
                            "max_output_tokens": 32,
                            "max_inflight": inflight,
                            "max_deadline_seconds": 3600,
                        },
                    }
                    for name, token in TOKENS.items()
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


@contextmanager
def running(tmp_path, *, private=False, inflight=2):
    from quota_broker.access import AccessPolicyFile

    calls = []
    configured = target("google", "google", "gemini-2.5-flash-lite")
    evidence = Evidence(
        10000,
        "official",
        NOW - timedelta(minutes=1),
        "fixture evidence",
        "fixture",
        NOW + timedelta(minutes=30),
    )
    configured = replace(
        configured,
        provider_quota_facts=tuple(
            QuotaFact(q.metric, q.window, evidence, evidence) for q in configured.quotas
        ),
    )
    gateway = make_gateway(tmp_path, [configured], calls)
    key = tmp_path / "queue.key"
    key.write_bytes(b"fixture-queue-key".ljust(32, b"!"))
    key.chmod(0o600)
    Path(gateway.db).chmod(0o600)
    prepare_queue_storage(gateway.db, key)
    queue = DurableQueue(gateway, key)
    path = policy_file(tmp_path, inflight=inflight)
    server = make_gateway_server(
        gateway,
        LEGACY,
        port=0,
        queue=queue,
        access_policy=AccessPolicyFile(path),
        private_api=private,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", gateway, queue, calls, path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base, who, method, path, body=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + TOKENS.get(who, who),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/v1/tasks", task()),
        ("POST", "/v1/queue", task()),
        ("POST", "/v1/queue/tick", {}),
        ("GET", "/v1/queue", None),
        ("GET", "/v1/tasks/legacy", None),
        ("POST", "/v1/admin/targets/google/quota", {}),
    ],
)
def test_read_plan_cannot_execute_or_read_job_content(tmp_path, method, path, body):
    with running(tmp_path) as (base, _gateway, _queue, calls, _path):
        assert request(base, "web", method, path, body)[0] == 403
        assert calls == []
        assert request(base, "web", "GET", "/v1/catalog")[0] == 200
        assert request(base, "web", "GET", "/v1/usage") == (200, [])
        assert request(base, "web", "POST", "/v1/routes/explain", task())[0] == 200


def test_project_keys_isolated_and_cross_project_objects_hidden(tmp_path):
    with running(tmp_path) as (base, gateway, _queue, calls, _path):
        gateway.run(task("old"))
        assert request(base, "a", "GET", "/v1/tasks/old")[0] == 404
        for who in ("a", "b"):
            status, data = request(base, who, "POST", "/v1/tasks", task("same"))
            assert status == 200 and data["request_key"] == "same"
        assert len(calls) == 3
        assert request(base, "a", "POST", "/v1/queue", task("only-a"))[0] == 200
        for suffix, method, body in (
            ("", "GET", None),
            ("/result", "GET", None),
            ("/cancel", "POST", {}),
        ):
            assert request(base, "b", method, "/v1/queue/only-a" + suffix, body)[0] == 404
        assert request(base, "a", "GET", "/v1/queue")[1]["tasks"][0]["request_key"] == "only-a"
        assert request(base, "b", "GET", "/v1/queue")[1]["tasks"] == []
        assert request(base, "a", "POST", "/v1/tasks", {**task(), "project_id": "b"})[0] == 400
        assert request(base, "a", "GET", "/v1/usage")[0] == 403
        assert request(base, "a", "POST", "/v1/queue/tick", {})[0] == 403


def test_policy_revocation_and_project_limits(tmp_path):
    with running(tmp_path, inflight=1) as (base, _gateway, _queue, calls, path):
        assert request(base, "a", "POST", "/v1/tasks", task(text="x" * 1025))[0] == 403
        assert (
            request(base, "a", "POST", "/v1/tasks", {**task(), "max_output_tokens": 33})[0] == 403
        )
        assert request(base, "a", "POST", "/v1/queue", task("first"))[0] == 200
        assert request(base, "a", "POST", "/v1/queue", task("second"))[0] == 429
        assert request(base, "a", "POST", "/v1/queue/first/cancel", {})[0] == 200
        assert request(base, "a", "POST", "/v1/queue", task("second"))[0] == 200
        policy = json.loads(path.read_text())
        policy["principals"] = [p for p in policy["principals"] if p["project_id"] != "a"]
        replacement = path.with_suffix(".next")
        replacement.write_text(json.dumps(policy))
        replacement.chmod(0o600)
        replacement.replace(path)
        assert request(base, "a", "GET", "/v1/catalog")[0] == 401
        assert calls == []


def test_private_listener_explicit_alias_allowlist(tmp_path):
    with running(tmp_path, private=True) as (base, _gateway, _queue, calls, _path):
        assert request(base, "a", "POST", "/quota/api/v1/jobs", task("private"))[0] == 200
        assert request(base, "a", "GET", "/quota/api/v1/jobs/private")[0] == 200
        for path in (
            "/v1/tasks",
            "/quota/api/v1/admin/x",
            "/quota/api/v1/jobs/tick",
            "/quota/api/v1/diagnostics",
        ):
            assert request(base, "a", "POST", path, {})[0] == 404
        assert request(base, LEGACY, "POST", "/quota/api/v1/tasks", task())[0] == 401
        assert calls == []


def test_dashboard_global_metadata_has_no_job_identifiers(tmp_path):
    with running(tmp_path) as (base, gateway, _queue, _calls, _path):
        gateway.run(task("private-request-marker"))
        status, data = request(base, "web", "GET", "/v1/tasks?limit=6")
        assert status == 200 and data["tasks"][0]["state"] == "completed"
        assert "private-request-marker" not in json.dumps(data)
        assert "request_key" not in json.dumps(data)
        assert "reservation_id" not in json.dumps(data)
        assert data["next_before"] is None
        assert request(base, "web", "GET", "/v1/diagnostics")[0] == 200
        assert request(base, "a", "GET", "/v1/diagnostics")[0] == 403


@pytest.mark.parametrize(
    "mutation", ["mode", "symlink", "unknown_scope", "digest", "duplicate", "limit", "too_large"]
)
def test_invalid_policy_fails_closed(tmp_path, mutation):
    from quota_broker.access import AccessPolicyFile

    path = policy_file(tmp_path)
    data = json.loads(path.read_text())
    if mutation == "mode":
        path.chmod(0o644)
    elif mutation == "symlink":
        link = tmp_path / "linked"
        link.symlink_to(path)
        path = link
    else:
        if mutation == "unknown_scope":
            data["principals"][0]["scopes"] = ["execute_all"]
        elif mutation == "digest":
            data["principals"][0]["token_sha256"] = TOKENS["web"]
        elif mutation == "duplicate":
            data["principals"].append(data["principals"][0])
        elif mutation == "limit":
            data["principals"][0]["limits"]["max_inflight"] = True
        elif mutation == "too_large":
            data["extra"] = "x" * 65536
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        AccessPolicyFile(path).read()


def test_scoped_queue_dispatch_rechecks_current_evidence(tmp_path):
    with running(tmp_path) as (base, gateway, queue, calls, _path):
        assert request(base, "a", "POST", "/v1/queue", task("stale"))[0] == 200
        gateway.clock = lambda: NOW + timedelta(minutes=31)
        queue.tick("fixture-worker")
        assert calls == []
        assert request(base, "a", "GET", "/v1/queue/stale")[1]["state"] != "completed"


def test_scoped_queue_results_and_revocation_at_dispatch(tmp_path):
    with running(tmp_path) as (base, _gateway, queue, calls, path):
        assert request(base, "a", "POST", "/v1/queue", task("good"))[0] == 200
        queue.tick("fixture-worker")
        status, outcome = request(base, "a", "GET", "/v1/queue/good/result")
        assert status == 200 and outcome["answer"] == "fixture answer"
        assert outcome["request_key"] == "good" and "q-" not in json.dumps(outcome)
        assert request(base, "b", "GET", "/v1/queue/good/result")[0] == 404
        assert request(base, "a", "POST", "/v1/queue", task("revoked"))[0] == 200
        data = json.loads(path.read_text())
        data["principals"] = [p for p in data["principals"] if p["project_id"] != "a"]
        path.write_text(json.dumps(data))
        queue.tick("fixture-worker")
        assert len(calls) == 1


def test_unknown_quota_refuses_scoped_but_preserves_legacy_gate(tmp_path):
    with running(tmp_path) as (base, gateway, _queue, calls, _path):
        gateway.targets = tuple(replace(t, provider_quota_facts=()) for t in gateway.targets)
        gateway.broker.targets = gateway.targets
        status, outcome = request(base, "a", "POST", "/v1/tasks", task("no-evidence"))
        assert status != 200 or outcome["state"] != "completed"
        assert calls == []
        assert (
            request(base, LEGACY, "POST", "/v1/tasks", task("legacy-allowed"))[1]["state"]
            == "completed"
        )
        assert len(calls) == 1


def test_concurrent_project_claim_limit_is_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with running(tmp_path, inflight=1) as (base, _gateway, _queue, calls, _path):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(request, base, "a", "POST", "/v1/queue", task(key))
                for key in ("race-1", "race-2")
            ]
            statuses = [future.result()[0] for future in futures]
        assert sorted(statuses) == [200, 429]
        assert calls == []


def test_scoped_identity_survives_server_restart_and_unknown_never_replays(tmp_path):
    from quota_broker.access import AccessPolicyFile
    from quota_broker.gateway_providers import ProviderPhaseTimeout

    with running(tmp_path) as (base, gateway, queue, calls, path):

        def timeout(*_args):
            calls.append("unknown-fixture")
            raise ProviderPhaseTimeout("read")

        gateway.transport = timeout
        status, outcome = request(base, "a", "POST", "/v1/tasks", task("uncertain"))
        assert status == 200 and outcome["state"] == "unknown"
        count = len(calls)
        second = make_gateway_server(
            gateway, LEGACY, port=0, queue=queue, access_policy=AccessPolicyFile(path)
        )
        worker = threading.Thread(target=second.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{second.server_port}"
            assert request(url, "b", "GET", "/v1/tasks/uncertain")[0] == 404
            assert (
                request(url, "a", "POST", "/v1/tasks", task("uncertain"))[1]["state"] == "unknown"
            )
            assert len(calls) == count == 1
        finally:
            second.shutdown()
            second.server_close()
            worker.join(timeout=2)


def test_policy_expiration_and_credential_collisions(tmp_path):
    from quota_broker.access import AccessPolicyFile

    path = policy_file(tmp_path)
    data = json.loads(path.read_text())
    data["principals"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))
    assert AccessPolicyFile(path).authenticate(TOKENS["web"]) is None


def test_two_listener_serialization_uses_one_work_gate(tmp_path):
    import time

    from quota_broker.access import AccessPolicyFile

    with running(tmp_path) as (_base, gateway, _queue, _calls, path):
        entered, release = threading.Event(), threading.Event()
        original = gateway.explain

        def slow(body):
            entered.set()
            release.wait(2)
            return original(body)

        gateway.explain = slow
        servers = [
            make_gateway_server(
                gateway, LEGACY, port=0, access_policy=AccessPolicyFile(path), serialize_work=True
            )
            for _ in range(2)
        ]
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        pending = threading.Thread(
            target=request,
            args=(
                f"http://127.0.0.1:{servers[0].server_port}",
                "web",
                "POST",
                "/v1/routes/explain",
                task(),
            ),
        )
        pending.start()
        try:
            assert entered.wait(2)
            started = time.monotonic()
            status, _ = request(
                f"http://127.0.0.1:{servers[1].server_port}",
                "web",
                "POST",
                "/v1/routes/explain",
                task(),
            )
            assert status == 503 and time.monotonic() - started < 1
        finally:
            release.set()
            pending.join(timeout=3)
            for server, thread in zip(servers, threads, strict=True):
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


def test_cli_rejects_private_listener_without_policy_before_io(monkeypatch):
    from quota_broker.cli import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "gateway-serve",
            "--config",
            "/missing",
            "--db",
            "/missing",
            "--digest-key-file",
            "/missing",
            "--client-token-file",
            "/missing",
            "--doppler-token-file",
            "/missing",
            "--doppler-project",
            "fixture",
            "--doppler-config",
            "fixture",
            "--private-api-port",
            "18085",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_plain_project_headers_cannot_change_credential_identity(tmp_path):
    with running(tmp_path) as (base, _gateway, _queue, calls, _path):
        req = urllib.request.Request(
            base + "/v1/queue",
            method="POST",
            data=json.dumps(task("header")).encode(),
            headers={
                "Authorization": "Bearer " + TOKENS["a"],
                "Content-Type": "application/json",
                "X-Project-ID": "b",
                "X-Forwarded-For": "127.0.0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            assert response.status == 200
        assert request(base, "a", "GET", "/v1/queue/header")[0] == 200
        assert request(base, "b", "GET", "/v1/queue/header")[0] == 404
        assert calls == []


def test_scoped_policy_cannot_reuse_legacy_credential(tmp_path):
    from quota_broker.access import AccessPolicyFile

    gateway = make_gateway(tmp_path, [], [])
    path = policy_file(tmp_path)
    data = json.loads(path.read_text())
    data["principals"][0]["token_sha256"] = hashlib.sha256(LEGACY.encode()).hexdigest()
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="legacy"):
        make_gateway_server(gateway, LEGACY, port=0, access_policy=AccessPolicyFile(path))


def offline_access(tmp_path, *, inflight=2):
    """Real Gateway/queue/ownership with only an in-process provider transport."""
    from quota_broker.access import AccessPolicyFile, ProjectAccess

    calls = []
    item = target("google", "google", "gemini-2.5-flash-lite")
    evidence = Evidence(
        10000,
        "official",
        NOW - timedelta(minutes=1),
        "fixture evidence",
        "fixture",
        NOW + timedelta(minutes=30),
    )
    item = replace(
        item,
        provider_quota_facts=tuple(
            QuotaFact(q.metric, q.window, evidence, evidence) for q in item.quotas
        ),
    )
    gateway = make_gateway(tmp_path, [item], calls)
    Path(gateway.db).chmod(0o600)
    queue = DurableQueue(gateway, b"fixture-queue-key".ljust(32, b"!"))
    policy = AccessPolicyFile(policy_file(tmp_path, inflight=inflight))
    access = ProjectAccess(gateway, policy)
    return access, policy.authenticate(TOKENS["a"]), queue, calls


@pytest.mark.parametrize("kind", ["tasks", "jobs"])
def test_claim_binds_payload_before_dispatch_even_after_crash_gap(tmp_path, kind):
    from quota_broker.gateway import GatewayError

    access, principal, queue, calls = offline_access(tmp_path)
    internal, first = access.claim(principal, kind, task("same", text="x"))
    with pytest.raises(GatewayError) as error:
        access.claim(principal, kind, task("same", text="x" * 500))
    assert error.value.code == "conflict"
    # Canonical equivalence, a later clock, and a new listener cannot rotate ID/deadline.
    access.gateway.clock = lambda: NOW + timedelta(seconds=2)
    again, second = access.claim(
        principal, kind, dict(reversed(list(task("same", text="x").items())))
    )
    assert again == internal and second == first
    if kind == "jobs":
        queue.submit(second)
        assert queue.tick("fixture-worker")["state"] == "completed"
    else:
        assert access.gateway.run(second)["state"] == "completed"
    assert len(calls) == 1


def test_claim_race_cannot_choose_a_different_payload(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from quota_broker.gateway import GatewayError

    access, principal, _queue, calls = offline_access(tmp_path)

    def claim(text):
        try:
            return access.claim(principal, "jobs", task("same", text=text))[1]
        except GatewayError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["x", "x" * 500]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("conflict") == 1
    assert calls == []


def test_unfingerprinted_ownership_migration_fails_closed(tmp_path):
    import sqlite3

    from quota_broker.access import ProjectAccess
    from quota_broker.gateway import GatewayError

    access, principal, queue, calls = offline_access(tmp_path)
    # Model the prior additive schema, with a real queued request and its old claim.
    internal, body = access.claim(principal, "jobs", task("unbound"))
    queue.submit(body)
    with sqlite3.connect(access.gateway.db) as con:
        if "payload_hmac" in {
            row[1] for row in con.execute("PRAGMA table_info(gateway_project_objects)")
        }:
            con.execute("ALTER TABLE gateway_project_objects DROP COLUMN payload_hmac")
    ProjectAccess(access.gateway, access.policy)
    with pytest.raises(GatewayError) as error:
        access.claim(principal, "jobs", task("unbound"))
    assert error.value.code == "conflict"
    queue.tick("fixture-worker")
    assert calls == []
    assert access.lookup(principal, "jobs", "unbound") == internal


@pytest.mark.parametrize("option", ["tools", "schema"])
def test_project_input_limit_includes_provider_options(tmp_path, option):
    from quota_broker.gateway import GatewayError

    access, principal, _queue, calls = offline_access(tmp_path)
    options = (
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object"},
                        "description": "x" * 5000,
                    },
                }
            ]
        }
        if option == "tools"
        else {"json_schema": {"type": "object", "description": "x" * 2000}}
    )
    body = {
        "request_key": "large-options",
        "capability": "text_generation",
        "input": {"messages": [{"role": "user", "content": "hi"}]},
        "options": options,
        "max_output_tokens": 16,
    }
    access.gateway.validate_task(body)  # This is a valid provider-bound payload.
    with pytest.raises(GatewayError) as error:
        access.bounds(principal, body)
    assert error.value.code == "forbidden"
    assert calls == []


def test_quota_exhausted_releases_project_inflight(tmp_path):

    access, principal, _queue, calls = offline_access(tmp_path, inflight=1)

    def refusal(*_args):
        calls.append("refused")
        return 429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'

    access.gateway.transport = refusal
    _internal, first = access.claim(principal, "tasks", task("refused"))
    outcome = access.gateway.run(first)
    assert outcome["state"] == "quota_exhausted" and outcome["ledger_state"] == "quota_rejected"
    _internal, unknown = access.claim(principal, "tasks", task("unknown"))
    assert unknown["request_key"].startswith("qp-")
    assert calls == ["refused"]


def test_unknown_still_holds_project_inflight(tmp_path):
    from quota_broker.gateway import GatewayError
    from quota_broker.gateway_providers import ProviderPhaseTimeout

    access, principal, _queue, calls = offline_access(tmp_path, inflight=1)

    def timeout(*_args):
        calls.append("timeout")
        raise ProviderPhaseTimeout("read")

    access.gateway.transport = timeout
    _internal, unknown = access.claim(principal, "tasks", task("unknown"))
    assert access.gateway.run(unknown)["state"] == "unknown"
    with pytest.raises(GatewayError) as error:
        access.claim(principal, "tasks", task("blocked"))
    assert error.value.code == "project_limit"
    assert calls == ["timeout"]


@pytest.mark.parametrize("kind", ["tasks", "jobs", "jobs-shrunk"])
def test_scoped_typed_result_preserves_business_ids_and_signed_continuation(tmp_path, kind):
    from test_family_adapters import specification
    from test_gateway import HMAC_KEY

    from quota_broker.access import AccessPolicyFile, ProjectAccess
    from quota_broker.config import Quota
    from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
    from quota_broker.gateway import Gateway
    from quota_broker.registry import Registry

    spec = specification("gemini_inference", "google", "text_generation")
    spec = replace(
        spec,
        endpoint_template=PACKAGED_FAMILY_ADAPTERS[spec.adapter].endpoint_for(
            spec, spec.capability, "{account_id}"
        ),
    )
    models = {**Registry.builtin().models, (spec.provider, spec.model): spec}
    evidence = Evidence(
        10000,
        "official",
        NOW - timedelta(minutes=1),
        "fixture evidence",
        "fixture",
        NOW + timedelta(minutes=30),
    )
    item = replace(
        target("google", "google", spec.model),
        model_info=spec,
        quotas=(Quota("requests", "requests", 10, "day"),),
        provider_quota_facts=(QuotaFact("requests", "day", evidence, evidence),),
    )
    args = {
        "request_key": "business-key",
        "reservation_id": "business-reservation",
        "provider_request_id": "business-provider",
        "execution_key": "business-execution",
    }
    parts = [
        {"functionCall": {"name": "lookup", "args": args}, "thoughtSignature": "fixture-signature"}
    ]
    response = {
        "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }
    calls = []

    def transport(request, *_args):
        calls.append(request)
        return 200, {}, json.dumps(response).encode()

    gateway = Gateway(
        tmp_path / "typed.db",
        (item,),
        HMAC_KEY,
        lambda _: "fixture-private-key",
        clock=lambda: NOW,
        registry=Registry(models),
        family_transport=transport,
    )
    Path(gateway.db).chmod(0o600)
    queue = DurableQueue(gateway, b"fixture-queue-key".ljust(32, b"!"))
    access = ProjectAccess(gateway, AccessPolicyFile(policy_file(tmp_path)))
    principal = access.policy.authenticate(TOKENS["a"])
    body = {
        "request_key": "typed",
        "capability": "text_generation",
        "input": {"messages": [{"role": "user", "content": "hi"}]},
        "provider": "google",
        "model": spec.model,
        "max_output_tokens": 16,
    }
    if kind == "jobs-shrunk":
        body["options"] = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object"},
                        "description": "x" * 300,
                    },
                }
            ]
        }
    object_kind = "jobs" if kind == "jobs-shrunk" else kind
    internal, data = access.claim(principal, object_kind, body)
    if kind == "jobs-shrunk":
        import sqlite3

        # Raw input is small; the persisted bound must include the tools as well.
        with sqlite3.connect(gateway.db) as con:
            stored = con.execute(
                "SELECT input_bytes FROM gateway_project_objects WHERE internal_key=?", (internal,)
            ).fetchone()[0]
        assert 300 < stored <= 1024
        assert gateway.explain(data)["selected_target_id"] == "google"
        queue.submit(data)
        policy = json.loads(access.policy.path.read_text())
        for row in policy["principals"]:
            if row["project_id"] == "a":
                row["limits"]["max_input_bytes"] = 300
        access.policy.path.write_text(json.dumps(policy))
        queue.tick("fixture-worker")
        assert calls == []
        assert access.lookup(principal, "jobs", "typed") == internal
        return
    if kind == "tasks":
        raw = gateway.run(data)
    else:
        queue.submit(data)
        assert queue.tick("fixture-worker")["state"] == "completed"
        raw = queue.result(internal)
    projected = access.external(raw, internal, "typed")
    assert projected["result"] == raw["result"]
    assert projected["request_key"] == "typed"
    assert "reservation_id" not in projected and all(
        "reservation_id" not in row for row in projected["attempts"]
    )
    message = projected["result"]["messages"][0]
    assert message["provider_state"]["parts"] == parts
    continuation = {
        **body,
        "request_key": "next",
        "input": {
            "messages": [
                body["input"]["messages"][0],
                message,
                {"role": "tool", "content": "{}", "tool_call_id": message["tool_calls"][0]["id"]},
            ]
        },
    }
    assert "google_continuation" in gateway.validate_task(continuation)["requirements"]["features"]
    assert len(calls) == 1
