import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from test_execution_policy import configure, principal
from test_gateway import task
from test_gateway_access import running

from quota_broker.access import AccessPolicyFile, ProjectAccess
from quota_broker.gateway import GatewayError
from quota_broker.local_jobs import LocalJobs, LocalTarget


class Backend:
    target = LocalTarget("rtx4090", "https://worker.internal", "dep_" + "a" * 64, "Qwen/Qwen3.5-4B")

    def __init__(self):
        self.posts = []
        self.lookups = []
        self.jobs = {}
        self.drop = False
        self.output = {
            "choices": [{"message": {"content": "private local answer"}, "finish_reason": "stop"}]
        }

    def submit(self, key, data):
        self.posts.append((key, data))
        self.jobs[key] = {
            "id": "job_" + "a" * 32,
            "deployment": self.target.deployment_id,
            "state": "queued",
            "result_state": "none",
            "ended": None,
        }
        if self.drop:
            raise TimeoutError
        return self.jobs[key]

    def lookup(self, key):
        self.lookups.append(key)
        return self.jobs.get(key)

    def status(self, jid):
        return next((j for j in self.jobs.values() if j["id"] == jid), None)

    def result(self, jid):
        return self.output

    def cancel(self, jid):
        return self.status(jid)


def local_setup(gateway, queue, path, backend):
    configure(path)
    access = ProjectAccess(gateway, AccessPolicyFile(path))
    local = LocalJobs(queue, access, backend)
    return local, access


def submit(local, access, path, key="local-1", **extra):
    body = {**task(key), "execution_policy": "local_only", **extra}
    internal, data = access.claim(principal(path), "jobs", body, encrypted=True)
    return internal, local.submit(data)


def test_dropped_submit_pins_identity_and_restart_only_looks_up(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        backend.drop = True
        local.tick()
        assert local.status(internal)["state"] == "unknown"
        recreated = LocalJobs(queue, access, backend)
        recreated.tick()
        assert len(backend.posts) == 1
        assert backend.lookups == [backend.posts[0][0]]
        assert recreated.status(internal)["state"] == "queued"
        assert cloud == []
        assert b"fixture input" not in Path(gateway.db).read_bytes()


@pytest.mark.parametrize("state", ["failed", "unknown", "expired"])
def test_local_gpu_failure_never_falls_back(tmp_path, state):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        next(iter(backend.jobs.values())).update(state=state, error="oom_or_timeout")
        local.tick()
        local.tick()
        assert len(backend.posts) == 1
        assert local.status(internal)["state"] == state
        assert cloud == []


def test_missing_after_unknown_never_reposts_or_frees_capacity(tmp_path):
    with running(tmp_path, inflight=8) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        backend.drop = True
        internal, _ = submit(local, access, path)
        local.tick()
        backend.jobs.clear()
        initial = gateway.clock()
        gateway.clock = lambda: initial + timedelta(days=8)
        local.tick()
        assert local.status(internal)["state"] == "unknown"
        submit(local, access, path, "local-2")
        with pytest.raises(GatewayError):
            submit(local, access, path, "local-3")
        assert len(backend.posts) == 1
        assert cloud == []


def test_result_pending_and_storage_failed_retain_recovery_then_expire_content(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        job = next(iter(backend.jobs.values()))
        for state in ("pending", "storage_failed"):
            job.update(state="succeeded", result_state=state, ended=gateway.clock().timestamp())
            local.tick()
            assert local.status(internal)["result_available"] is False
            with pytest.raises(GatewayError):
                local.result(internal)
        job["result_state"] = "available"
        local.tick()
        assert local.result(internal)["result"]["text"] == "private local answer"
        assert local.result(internal)["execution_policy"] == "local_only"
        now = gateway.clock()
        gateway.clock = lambda: now + timedelta(hours=2)
        assert local.status(internal)["result_available"] is False
        with pytest.raises(GatewayError):
            local.result(internal)
        local.tick()
        assert len(backend.posts) == 1
        assert cloud == []


def test_known_job_id_is_saved_before_result_download_fails(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        original = backend.submit

        def completed(key, data):
            reply = original(key, data)
            reply.update(state="succeeded", result_state="available")
            return reply

        backend.submit = completed

        def unavailable(_jid):
            raise TimeoutError

        backend.result = unavailable
        local.tick()
        local.tick()
        assert backend.lookups == []
        assert len(backend.posts) == 1
        assert local.status(internal)["state"] == "unknown"


@pytest.mark.parametrize("change", ["revoked", "expired", "target"])
def test_dispatch_rechecks_credential_deadline_and_target(tmp_path, change):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        if change == "revoked":
            data = json.loads(path.read_text())
            data["principals"] = [p for p in data["principals"] if p["project_id"] != "a"]
            path.write_text(json.dumps(data))
        elif change == "expired":
            now = gateway.clock()
            gateway.clock = lambda: now + timedelta(seconds=901)
        else:
            backend.target = LocalTarget(
                "spark", "https://spark.internal", "dep_" + "b" * 64, "Qwen/Qwen3.5-4B"
            )
        local.tick()
        assert local.status(internal)["state"] in {"failed", "expired"}
        assert backend.posts == []
        assert cloud == []


def test_cancel_before_dispatch_and_same_key_conflict(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        assert local.cancel(internal)["state"] == "cancelled"
        with pytest.raises(GatewayError):
            submit(local, access, path, input="changed")
        local.tick()
        assert backend.posts == cloud == []


def test_cancel_unknown_waits_for_original_id_and_never_uses_changed_target(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        backend.drop = True
        cancelled = []

        def cancel(jid):
            cancelled.append(jid)
            reply = backend.status(jid)
            reply.update(state="canceled")
            return reply

        backend.cancel = cancel
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        local.cancel(internal)
        assert cancelled == []
        local.tick()
        assert len(cancelled) == 1
        assert local.status(internal)["state"] == "cancelled"


def test_cancel_dispatched_job_does_not_follow_new_target(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        cancelled = []
        backend.cancel = lambda jid: cancelled.append(jid)
        backend.target = LocalTarget(
            "spark", "https://spark.internal", "dep_" + "b" * 64, "Qwen/Qwen3.5-4B"
        )
        with pytest.raises(GatewayError):
            local.cancel(internal)
        assert cancelled == []


def test_local_catalog_and_explain_expose_full_project_queue(tmp_path):
    with running(tmp_path, inflight=8) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        submit(local, access, path, "one")
        submit(local, access, path, "two")
        assert local.catalog("a")["can_queue"] is False
        plan = local.explain(access.bounds(principal(path), task("three")), "a")
        assert plan["selected_target_id"] is None


def test_idle_worker_and_restart_expire_content_without_status_reads(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        next(iter(backend.jobs.values())).update(
            state="succeeded", result_state="available", ended=gateway.clock().timestamp()
        )
        local.tick()
        now = gateway.clock()
        gateway.clock = lambda: now + timedelta(hours=2)
        LocalJobs(queue, access, backend).tick()
        with sqlite3.connect(gateway.db) as con:
            row = con.execute(
                "SELECT payload,result,intent,downstream_key,downstream_id FROM local_jobs WHERE request_key=?",
                (internal,),
            ).fetchone()
        assert row[:2] == (None, None)
        assert row[2] == 1 and row[3] and row[4]
        assert len(backend.posts) == 1


def test_undispatched_target_change_erases_body_but_keeps_identity(tmp_path):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        backend.target = LocalTarget(
            "spark", "https://spark.internal", "dep_" + "b" * 64, "Qwen/Qwen3.5-4B"
        )
        local.tick()
        with sqlite3.connect(gateway.db) as con:
            row = con.execute(
                "SELECT state,payload,intent,downstream_key FROM local_jobs WHERE request_key=?",
                (internal,),
            ).fetchone()
        assert row[:3] == ("failed", None, 0)
        assert row[3]


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": 42}}]},
    ],
)
def test_malformed_result_is_contained_and_next_job_still_advances(tmp_path, output):
    with running(tmp_path) as (_base, gateway, queue, _cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        internal, _ = submit(local, access, path)
        local.tick()
        next(iter(backend.jobs.values())).update(state="succeeded", result_state="available")
        backend.output = output
        local.tick()  # Must not propagate AttributeError into the shared worker loop.
        assert local.status(internal)["state"] == "unknown"
        submit(local, access, path, "next-job")
        local.tick()
        assert len(backend.posts) == 2
        assert backend.lookups == []  # The original job ID is already durable.


@pytest.mark.parametrize(
    "extra",
    [
        {"provider": "google"},
        {"model": "unapproved-model"},
        {"capability": "translation"},
        {"input": "x" * 8200},
        {"options": {"tools": []}},
    ],
)
def test_unsupported_local_contract_never_uses_cloud(tmp_path, extra):
    with running(tmp_path) as (_base, gateway, queue, cloud, path):
        backend = Backend()
        local, access = local_setup(gateway, queue, path, backend)
        with pytest.raises(GatewayError):
            submit(local, access, path, **extra)
        assert backend.posts == cloud == []
