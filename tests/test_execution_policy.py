"""Execution locality is a server contract, not a caller hint or model name."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_gateway import make_gateway, target, task
from test_gateway_access import TOKENS, policy_file, request, running

from quota_broker.access import AccessPolicyFile, ProjectAccess
from quota_broker.gateway import GatewayError, validate_task


def configure(path, *, allowed=("local_only",), default="local_only", encrypted=True):
    data = json.loads(path.read_text())
    for item in data["principals"]:
        if item["project_id"] == "a":
            item.update(
                allowed_execution=list(allowed),
                default_execution=default,
                encryption_required=encrypted,
            )
    path.write_text(json.dumps(data))


def principal(path):
    return AccessPolicyFile(path).authenticate(TOKENS["a"])


def test_legacy_policy_defaults_to_cloud_only(tmp_path):
    p = principal(policy_file(tmp_path))
    assert getattr(p, "allowed_execution", None) == frozenset({"cloud_only"})
    assert getattr(p, "default_execution", None) == "cloud_only"
    assert getattr(p, "encryption_required", None) is False


def test_local_project_policy_inherits_default_before_admission(tmp_path):
    with running(tmp_path) as (_base, gateway, _queue, calls, path):
        configure(path)
        p = principal(path)
        data = ProjectAccess(gateway, AccessPolicyFile(path)).bounds(p, task())
        assert data["execution_policy"] == "local_only"
        with pytest.raises(GatewayError, match="execution"):
            ProjectAccess(gateway, AccessPolicyFile(path)).bounds(
                p, {**task(), "execution_policy": "cloud_only"}
            )
        assert calls == []


def test_same_project_credentials_cannot_have_different_policies(tmp_path):
    path = policy_file(tmp_path)
    configure(path)
    data = json.loads(path.read_text())
    item = next(p for p in data["principals"] if p["project_id"] == "a")
    other = {
        **item,
        "token_sha256": hashlib.sha256(b"synthetic-rotated-token").hexdigest(),
        "allowed_execution": ["cloud_only"],
        "default_execution": "cloud_only",
    }
    data["principals"].append(other)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        AccessPolicyFile(path).read()


@pytest.mark.parametrize("value", ["auto", "local", "cloud", "", None, True, ["local_only"]])
def test_unknown_execution_policy_is_not_silently_ignored(value):
    with pytest.raises(GatewayError):
        validate_task({**task(), "execution_policy": value})


def test_cloud_normalization_preserves_existing_payload_identity():
    assert validate_task(task()) == validate_task({**task(), "execution_policy": "cloud_only"})
    assert validate_task({**task(), "execution_policy": "local_only"}) != validate_task(task())


def test_local_request_never_reaches_cloud_or_count_tokens(tmp_path):
    calls = []
    configured = replace(
        target("looks-local-4090", "google", "gemini-2.5-flash-lite"),
        source="self-hosted local model",
    )
    gateway = make_gateway(tmp_path, [configured], calls)
    raw = {**task(), "execution_policy": "local_only"}
    plan = gateway.explain(raw)
    assert plan["selected_target_id"] is None
    assert plan["permanent_rejection"] is True
    assert "execution_policy:cloud_target" in plan["candidates"][0]["reasons"]
    gateway.google_pdf_count_tokens = True
    with pytest.raises(GatewayError) as error:
        gateway.run(raw)
    assert error.value.code == "local_unavailable"
    assert calls == []


@pytest.mark.parametrize("route", ["/quota/api/v1/catalog", "/v1/catalog"])
def test_encryption_required_cannot_be_bypassed_via_legacy_routes(tmp_path, route):
    with running(tmp_path, private=route.startswith("/quota")) as (base, _g, _q, calls, path):
        configure(path)
        code, body = request(base, "a", "GET", route)
        assert code == 403
        assert body["error"] == "encryption_required"
        assert calls == []


def test_changing_policy_for_same_job_is_a_conflict(tmp_path):
    with running(tmp_path) as (_base, gateway, _queue, _calls, path):
        configure(path, allowed=("local_only", "cloud_only"))
        access = ProjectAccess(gateway, AccessPolicyFile(path))
        p = principal(path)
        internal, local = access.claim(p, "jobs", task(), encrypted=True)
        assert local["execution_policy"] == "local_only"
        assert access.lookup(p, "jobs", task()["request_key"]) == internal
        with pytest.raises(GatewayError) as error:
            access.claim(p, "jobs", {**task(), "execution_policy": "cloud_only"}, encrypted=True)
        assert error.value.code == "conflict"


@pytest.mark.parametrize("change", ["local_only", "encryption_required"])
def test_queued_cloud_work_rechecks_stricter_policy_before_dispatch(tmp_path, change):
    with running(tmp_path) as (_base, gateway, queue, calls, path):
        access = ProjectAccess(gateway, AccessPolicyFile(path))
        internal, data = access.claim(principal(path), "jobs", task())
        queue.submit(data)
        configure(
            path,
            allowed=("local_only",) if change == "local_only" else ("cloud_only",),
            default="local_only" if change == "local_only" else "cloud_only",
        )
        queue.tick("fixture-worker")
        assert calls == []
        assert queue.status(internal)["state"] != "completed"


def test_relaxing_project_policy_does_not_reroute_existing_local_work(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, calls, path):
        configure(path)
        access = ProjectAccess(gateway, AccessPolicyFile(path))
        internal, data = access.claim(principal(path), "jobs", task(), encrypted=True)
        queue.submit(data)
        configure(path, allowed=("local_only", "cloud_only"), default="cloud_only")
        queue.tick("fixture-worker")
        assert calls == []
        assert queue.status(internal)["state"] != "completed"
