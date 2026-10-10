"""Durable queue behavior with clock-controlled SQLite and fixture-only execution."""

import base64
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY, fixture_transport, target, task
from test_gateway import NOW as GATEWAY_NOW

from quota_broker.core import stamp
from quota_broker.gateway import Gateway, GatewayError
from quota_broker.queue import DurableQueue, prepare_queue_storage

KEY = b"fixture-queue-key".ljust(32, b"!")
NOW = datetime(2026, 10, 2, tzinfo=UTC)


class Crash(BaseException):
    """Model process termination, bypassing ordinary execution error handling."""


class FakeGateway:
    def __init__(self, path):
        self.db = str(path)
        self.now = NOW
        self.digest_key = b"fixture-gateway-independent-digest-key"
        self.calls = []
        self.plans = []
        self.mode = "ready"
        self.before_dispatch = None
        self.after_dispatch = None
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(self.db) as con:
            con.executescript("""
                CREATE TABLE gateway_tasks (
                    request_key TEXT PRIMARY KEY, state TEXT, dispatched_at TEXT,
                    reservation_id TEXT
                );
                CREATE TABLE gateway_attempts (
                    request_key TEXT, state TEXT, dispatched_at TEXT, reservation_id TEXT
                );
                CREATE TABLE reservations (id TEXT PRIMARY KEY, state TEXT, dispatched_at TEXT);
            """)

    def clock(self):
        return self.now

    def validate_task(self, raw):
        assert isinstance(raw["input"], str) and raw["input"]
        return {"priority": 0, "deadline": None, "wait_policy": "reject", "max_attempts": 32, **raw}

    def explain(self, raw):
        self.plans.append(raw["request_key"])
        if self.mode == "busy":
            return {
                "selected_target_id": None,
                "temporary": True,
                "next_retry_at": stamp(self.now + timedelta(seconds=20)),
            }
        if self.mode == "permanent":
            return {"selected_target_id": None, "temporary": False, "permanent_rejection": True}
        return {"selected_target_id": "fixture-target", "temporary": False}

    def run(self, raw, *, dispatch_guard):
        key = raw["request_key"]
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO gateway_tasks VALUES (?, 'preparing', NULL, ?)", (key, key))
            con.execute("INSERT INTO reservations VALUES (?, 'reserved', NULL)", (key,))
        if self.mode == "race":
            with sqlite3.connect(self.db) as con:
                con.execute("UPDATE gateway_tasks SET state='rejected' WHERE request_key=?", (key,))
                con.execute("UPDATE reservations SET state='cancelled' WHERE id=?", (key,))
            raise GatewayError("unavailable", "fixture reserve race")
        if self.before_dispatch:
            self.before_dispatch()
        with sqlite3.connect(self.db, isolation_level=None) as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            try:
                dispatch_guard(con)
            except GatewayError:
                con.execute("ROLLBACK")
                raise
            con.execute(
                "UPDATE reservations SET state='dispatched',dispatched_at=? WHERE id=?",
                (stamp(self.now), key),
            )
            # Exercise crash-gap protection: ledger durable before task dispatch marker.
            con.execute("COMMIT")
        self.calls.append(key)
        if self.after_dispatch:
            self.after_dispatch()
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE gateway_tasks SET state='completed',dispatched_at=? WHERE request_key=?",
                (stamp(self.now), key),
            )
            con.execute("UPDATE reservations SET state='completed' WHERE id=?", (key,))
        return {
            "request_key": key,
            "state": "completed",
            "provider": "fixture",
            "answer": "sensitive fixture answer",
            "reported_output_tokens": 3,
        }


def raw(key="job", **kwargs):
    return {
        "request_key": key,
        "capability": "text_generation",
        "input": "sensitive fixture input",
        "max_output_tokens": 10,
        **kwargs,
    }


def setup(tmp_path, **kwargs):
    gateway = FakeGateway(tmp_path / "queue.sqlite")
    return gateway, DurableQueue(gateway, KEY, **kwargs)


def test_encrypted_success_metadata_only_and_idempotent(tmp_path):
    gateway, queue = setup(tmp_path)
    assert queue.submit(raw())["state"] == "queued"
    with pytest.raises(GatewayError, match="not ready"):
        queue.result("job")
    done = queue.tick("worker")
    assert done["state"] == "completed" and "answer" not in done
    assert len(gateway.calls) == 1 and gateway.calls[0] != "job"
    assert queue.result("job")["answer"] == "sensitive fixture answer"
    assert queue.submit(raw())["state"] == "completed"
    assert queue.tick("worker") is None
    assert queue.status("job") == queue.recent()["tasks"][0]
    with pytest.raises(GatewayError) as error:
        queue.submit(raw(input="changed"))
    assert error.value.code == "conflict"
    stored = Path(gateway.db).read_bytes()
    for secret in (b"sensitive fixture input", b"sensitive fixture answer", KEY):
        assert secret not in stored
    assert "sensitive" not in json.dumps(queue.recent())
    with sqlite3.connect(gateway.db) as con:
        payload, result = con.execute("SELECT payload,result FROM queue_jobs").fetchone()
    assert payload is None and result


def test_busy_waits_without_loop_then_single_dispatch(tmp_path):
    gateway, queue = setup(tmp_path)
    gateway.mode = "busy"
    queue.submit(raw())
    waiting = queue.tick("worker")
    assert waiting["state"] == "waiting" and waiting["attempt_count"] == 0
    assert queue.tick("worker") is None and gateway.calls == []
    gateway.now += timedelta(seconds=19)
    assert queue.tick("worker") is None
    gateway.now += timedelta(seconds=1)
    gateway.mode = "ready"
    assert queue.tick("worker")["state"] == "completed"
    assert gateway.plans == ["job", "job"] and len(gateway.calls) == 1


def test_permanent_and_reject_policy_fail_without_dispatch(tmp_path):
    gateway, queue = setup(tmp_path)
    gateway.mode = "permanent"
    queue.submit(raw("permanent"))
    assert queue.tick("worker")["error_code"] == "no_compatible_route"
    gateway.mode = "busy"
    queue.submit(raw("reject", wait_policy="reject"))
    assert queue.tick("worker")["state"] == "failed"
    assert gateway.calls == []


def test_priority_and_deadline_order(tmp_path):
    _gateway, queue = setup(tmp_path)
    queue.submit(raw("low", priority=-1))
    queue.submit(raw("later", priority=3, deadline=stamp(NOW + timedelta(minutes=10))))
    queue.submit(raw("soon", priority=3, deadline=stamp(NOW + timedelta(minutes=1))))
    assert [queue.tick("worker")["request_key"] for _ in range(3)] == ["soon", "later", "low"]


def test_multiple_workers_claim_once(tmp_path):
    gateway, queue = setup(tmp_path)
    queue.submit(raw())
    second = DurableQueue(gateway, KEY)
    gate = threading.Barrier(2)

    def tick(q, worker):
        gate.wait()
        return q.tick(worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda pair: tick(*pair), [(queue, "first"), (second, "second")]))
    assert sum(answer is not None for answer in answers) == 1
    assert len(gateway.calls) == 1


def test_restart_claim_before_run_is_recoverable(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=5)
    queue.submit(raw())
    assert queue._claim("crashed")["state"] == "running"
    gateway.now += timedelta(seconds=6)
    restarted = DurableQueue(gateway, KEY, lease_seconds=5)
    assert restarted.tick("new")["state"] == "completed" and len(gateway.calls) == 1


def test_restart_after_preparing_without_dispatch_is_recoverable(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=5)
    queue.submit(raw())
    gateway.before_dispatch = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        queue.tick("crashed")
    gateway.now += timedelta(seconds=6)
    gateway.before_dispatch = None
    restarted = DurableQueue(gateway, KEY, lease_seconds=5)
    assert restarted.tick("new")["state"] == "completed"
    assert len(gateway.calls) == 1
    assert len(restarted.status("job")["attempts"]) == 2


def test_dispatch_crash_ledger_gap_is_unknown_and_hold_remains(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=5, execution_lease_seconds=5)
    queue.submit(raw())
    gateway.after_dispatch = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        queue.tick("crashed")
    gateway.now += timedelta(seconds=6)
    gateway.after_dispatch = None
    restarted = DurableQueue(gateway, KEY, lease_seconds=5, execution_lease_seconds=5)
    assert restarted.tick("new") is None
    assert restarted.status("job")["state"] == "unknown"
    with sqlite3.connect(gateway.db) as con:
        assert con.execute("SELECT state FROM reservations").fetchone()[0] == "dispatched"
    queue.submit(raw("other-provider"))
    assert restarted.tick("new")["state"] == "completed"
    assert len(gateway.calls) == 2


def test_stale_worker_cannot_dispatch_after_replacement_lease(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=5)
    queue.submit(raw())
    entered, resume = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert resume.wait(5)

    gateway.before_dispatch = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(queue.tick, "old")
        assert entered.wait(5)
        gateway.now += timedelta(seconds=6)
        gateway.before_dispatch = None
        replacement = DurableQueue(gateway, KEY, lease_seconds=5)
        assert replacement.tick("new")["state"] == "completed"
        resume.set()
        assert old.result(timeout=5)["state"] == "completed"
    assert len(gateway.calls) == 1


def test_reserve_race_uses_new_execution_key_and_bounded_attempts(tmp_path):
    gateway, queue = setup(tmp_path, retry_seconds=5)
    gateway.mode = "race"
    queue.submit(raw(max_attempts=2))
    first = queue.tick("worker")
    assert first["state"] == "waiting"
    assert queue.tick("worker") is None
    gateway.now += timedelta(seconds=5)
    second = queue.tick("worker")
    assert second["state"] == "failed" and second["attempt_count"] == 2
    assert first["execution_key"] != second["execution_key"] and gateway.calls == []
    assert queue.tick("worker") is None


def test_deadline_ttl_cleanup_and_cancel_preserve_no_replay_tombstone(tmp_path):
    gateway, queue = setup(tmp_path, ttl_seconds=20)
    queue.submit(raw("expired", deadline=stamp(NOW - timedelta(seconds=1))))
    assert queue.status("expired")["state"] == "expired"
    queue.submit(raw("cancel"))
    assert queue.cancel("cancel")["state"] == "cancelled"
    queue.submit(raw("result"))
    queue.tick("worker")
    with sqlite3.connect(gateway.db) as con:
        ciphertext = con.execute(
            "SELECT result FROM queue_jobs WHERE request_key='result'"
        ).fetchone()[0]
    gateway.now += timedelta(seconds=21)
    assert queue.status("result")["state"] == "completed"
    with pytest.raises(GatewayError) as error:
        queue.result("result")
    assert error.value.code == "queue_content_expired"
    with sqlite3.connect(gateway.db) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM queue_jobs WHERE payload IS NOT NULL OR result IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
    assert ciphertext not in Path(gateway.db).read_bytes()
    assert queue.submit(raw("result"))["state"] == "completed"
    assert queue.tick("worker") is None and len(gateway.calls) == 1


def test_pending_ttl_and_max_waiting(tmp_path):
    gateway, queue = setup(tmp_path, ttl_seconds=5, max_waiting=1)
    queue.submit(raw())
    with pytest.raises(GatewayError) as error:
        queue.submit(raw("second"))
    assert error.value.code == "queue_full"
    gateway.now += timedelta(seconds=6)
    assert queue.status("job")["state"] == "expired"
    assert queue.submit(raw("second"))["state"] == "queued"


def test_keys_and_permissions_fail_closed(tmp_path):
    gateway, queue = setup(tmp_path)
    queue.submit(raw())
    with pytest.raises(GatewayError) as error:
        DurableQueue(gateway, b"different-fixture-key".ljust(32, b"!"))
    assert error.value.code == "queue_key_mismatch"
    with pytest.raises(GatewayError):
        DurableQueue(gateway, None)
    with pytest.raises(GatewayError):
        DurableQueue(gateway, gateway.digest_key)
    Path(gateway.db).chmod(0o644)
    with pytest.raises(GatewayError) as error:
        queue.tick("worker")
    assert error.value.code == "queue_security" and gateway.calls == []
    Path(gateway.db).chmod(0o600)
    tmp_path.chmod(0o755)
    with pytest.raises(GatewayError):
        DurableQueue(gateway, KEY)


def test_private_key_file_persists_and_lost_key_does_not_replay(tmp_path):
    gateway = FakeGateway(tmp_path / "queue.sqlite")
    path = tmp_path / "queue.key"
    path.write_bytes(KEY)
    path.chmod(0o600)
    queue = DurableQueue(gateway, path)
    queue.submit(raw())
    assert DurableQueue(gateway, path).status("job")["state"] == "queued"
    path.unlink()
    with pytest.raises(GatewayError):
        DurableQueue(gateway, path)
    assert gateway.calls == []


def test_tamper_and_swapped_ciphertext_do_not_dispatch(tmp_path):
    gateway, queue = setup(tmp_path)
    queue.submit(raw("one"))
    queue.submit(raw("two"))
    with sqlite3.connect(gateway.db) as con:
        token = con.execute("SELECT payload FROM queue_jobs WHERE request_key='one'").fetchone()[0]
        con.execute("UPDATE queue_jobs SET payload=? WHERE request_key='two'", (token,))
        con.execute("UPDATE queue_jobs SET payload=? WHERE request_key='one'", (b"tampered",))
    assert queue.tick("worker")["state"] == "failed"
    assert queue.tick("worker")["state"] == "failed" and gateway.calls == []


def test_encoded_key_cannot_reuse_gateway_digest_key(tmp_path):
    gateway = FakeGateway(tmp_path / "queue.sqlite")
    gateway.digest_key = KEY
    with pytest.raises(GatewayError) as error:
        DurableQueue(gateway, base64.urlsafe_b64encode(KEY))
    assert error.value.code == "queue_config"


def test_result_finished_after_ttl_is_not_retained(tmp_path):
    gateway, queue = setup(tmp_path, ttl_seconds=5)
    queue.submit(raw())

    def time_passes():
        gateway.now += timedelta(seconds=6)

    gateway.after_dispatch = time_passes
    done = queue.tick("worker")
    assert done["state"] == "completed" and not done["result_available"]
    with pytest.raises(GatewayError):
        queue.result("job")


def test_pending_deadline_expires_before_dispatch(tmp_path):
    gateway, queue = setup(tmp_path)
    queue.submit(raw(deadline=stamp(NOW + timedelta(seconds=2))))
    gateway.now += timedelta(seconds=3)
    assert queue.tick("worker") is None
    assert queue.status("job")["state"] == "expired" and gateway.calls == []


class QuotaGateway(FakeGateway):
    def __init__(self, path, refusal_attempts=1):
        super().__init__(path)
        self.refusal_attempts = refusal_attempts
        self.refuse = True
        self.cooldown = None
        self.budgets = []

    def explain(self, raw):
        if self.cooldown and self.now < self.cooldown:
            return {
                "selected_target_id": None,
                "temporary": True,
                "next_retry_at": stamp(self.cooldown),
            }
        return super().explain(raw)

    def run(self, raw, *, dispatch_guard):
        self.budgets.append(raw["max_attempts"])
        if not self.refuse:
            return super().run(raw, dispatch_guard=dispatch_guard)
        key = raw["request_key"]
        attempts = min(self.refusal_attempts, raw["max_attempts"])
        with sqlite3.connect(self.db, isolation_level=None) as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            dispatch_guard(con)
            for number in range(attempts):
                reservation = f"{key}-{number}"
                con.execute(
                    "INSERT INTO reservations VALUES (?, 'quota_rejected', ?)",
                    (reservation, stamp(self.now)),
                )
                con.execute(
                    "INSERT INTO gateway_attempts VALUES (?, 'quota_rejected', ?, ?)",
                    (key, stamp(self.now), reservation),
                )
                self.calls.append(reservation)
            con.execute(
                "INSERT INTO gateway_tasks VALUES (?, 'quota_exhausted', ?, ?)",
                (key, stamp(self.now), reservation),
            )
            con.execute("COMMIT")
        self.cooldown = self.now + timedelta(seconds=20)
        return {"request_key": key, "state": "quota_exhausted"}


def test_proven_quota_refusal_waits_for_reset_then_completes(tmp_path):
    gateway = QuotaGateway(tmp_path / "queue.sqlite")
    queue = DurableQueue(gateway, KEY)
    queue.submit(raw(max_attempts=3))
    waiting = queue.tick("worker")
    assert waiting["state"] == "waiting" and waiting["attempt_budget_used"] == 1
    assert waiting["next_retry_at"] == stamp(NOW + timedelta(seconds=20))
    assert queue.tick("worker") is None and len(gateway.calls) == 1
    gateway.now += timedelta(seconds=20)
    gateway.refuse = False
    done = queue.tick("worker")
    assert done["state"] == "completed" and len(done["attempts"]) == 2
    assert gateway.budgets == [3, 2] and len(gateway.calls) == 2
    assert queue.result("job")["answer"] == "sensitive fixture answer"


def test_refusal_retry_budget_covers_all_gateway_attempts(tmp_path):
    gateway = QuotaGateway(tmp_path / "queue.sqlite", refusal_attempts=2)
    queue = DurableQueue(gateway, KEY)
    queue.submit(raw(max_attempts=3))
    waiting = queue.tick("worker")
    assert waiting["state"] == "waiting" and waiting["attempt_budget_used"] == 2
    gateway.now += timedelta(seconds=20)
    done = queue.tick("worker")
    assert done["state"] == "failed" and done["attempt_budget_used"] == 3
    assert gateway.budgets == [3, 1] and len(gateway.calls) == 3
    assert queue.tick("worker") is None


def test_refusal_evidence_does_not_hide_prior_unknown_attempt(tmp_path):
    gateway = QuotaGateway(tmp_path / "queue.sqlite")
    queue = DurableQueue(gateway, KEY)
    queue.submit(raw())
    # The fake refusal follows an earlier uncertain dispatch in the same run.
    original = gateway.run

    def run(raw, *, dispatch_guard):
        result = original(raw, dispatch_guard=dispatch_guard)
        key = raw["request_key"]
        with sqlite3.connect(gateway.db) as con:
            con.execute(
                "INSERT INTO reservations VALUES (?, 'unknown', ?)",
                (key + "-unknown", stamp(gateway.now)),
            )
            con.execute(
                "INSERT INTO gateway_attempts VALUES (?, 'unknown', ?, ?)",
                (key, stamp(gateway.now), key + "-unknown"),
            )
        return result

    gateway.run = run
    done = queue.tick("worker")
    assert done["state"] == "unknown" and queue.tick("worker") is None


def test_slow_response_uses_bounded_execution_lease(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=2, execution_lease_seconds=30)
    queue.submit(raw())

    def slow():
        gateway.now += timedelta(seconds=10)
        assert queue.status("job")["state"] == "running"

    gateway.after_dispatch = slow
    assert queue.tick("worker")["state"] == "completed"
    assert queue.result("job")["answer"] == "sensitive fixture answer"
    assert len(gateway.calls) == 1


def test_late_success_after_execution_lease_unknown_is_saved_without_replay(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=2, execution_lease_seconds=5)
    queue.submit(raw())

    def very_slow():
        gateway.now += timedelta(seconds=6)
        assert queue.status("job")["state"] == "unknown"
        assert queue.tick("other-worker") is None

    gateway.after_dispatch = very_slow
    assert queue.tick("worker")["state"] == "completed"
    assert queue.result("job")["answer"] == "sensitive fixture answer"
    assert len(gateway.calls) == 1 and queue.tick("worker") is None


def test_late_success_without_polling_is_saved(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=2, execution_lease_seconds=5)
    queue.submit(raw())

    def very_slow():
        gateway.now += timedelta(seconds=6)

    gateway.after_dispatch = very_slow
    assert queue.tick("worker")["state"] == "completed"
    assert queue.result("job")["answer"] == "sensitive fixture answer"
    assert len(gateway.calls) == 1


def test_execution_lease_cannot_renew_forever_or_dispatch_after_deadline(tmp_path):
    gateway, queue = setup(tmp_path, lease_seconds=2, execution_lease_seconds=5)
    queue.submit(raw(deadline=stamp(NOW + timedelta(seconds=3))))
    original = gateway.run

    def bounded(raw, *, dispatch_guard):
        outcome = original(raw, dispatch_guard=dispatch_guard)
        gateway.now += timedelta(seconds=4)
        with sqlite3.connect(gateway.db) as con:
            con.row_factory = sqlite3.Row
            with pytest.raises(GatewayError):
                dispatch_guard(con)
        gateway.now += timedelta(seconds=2)
        with sqlite3.connect(gateway.db) as con:
            con.row_factory = sqlite3.Row
            with pytest.raises(GatewayError):
                dispatch_guard(con)
        return outcome

    gateway.run = bounded
    assert queue.tick("worker")["state"] == "completed"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("has_deadline", [False, True])
def test_real_gateway_queue_lifecycle_preserves_normalized_task_contract(tmp_path, has_deadline):
    calls = []
    path = tmp_path / "private.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    gateway = Gateway(
        path,
        (target("google", "google", "gemini-3.5-flash-lite"),),
        HMAC_KEY,
        lambda _ref: "fixture-secret-value",
        fixture_transport(calls),
        clock=lambda: GATEWAY_NOW,
    )
    queue = DurableQueue(gateway, KEY)
    request = task("real-queue-lifecycle")
    if has_deadline:
        request["deadline"] = stamp(GATEWAY_NOW + timedelta(minutes=5))
    assert queue.submit(request)["state"] == "queued"
    with sqlite3.connect(gateway.db) as con:
        payload = con.execute("SELECT payload FROM queue_jobs").fetchone()[0]
    normalized = queue._decrypt(payload, request["request_key"], "payload")
    assert ("deadline" in normalized) is has_deadline
    assert gateway.validate_task(normalized) == normalized
    done = queue.tick("fixture-worker")
    assert done["state"] == "completed" and done["attempt_budget_used"] == 1
    assert len(calls) == 1 and "answer" not in done
    assert queue.result(request["request_key"])["answer"] == "fixture answer"
    assert queue.status(request["request_key"])["state"] == "completed"
    assert queue.submit(request)["state"] == "completed"
    assert queue.tick("fixture-worker") is None and len(calls) == 1
    stored = path.read_bytes()
    assert request["input"].encode() not in stored and b"fixture answer" not in stored


@pytest.mark.parametrize("unsafe", ["db", "parent", "key", "sidecar", "symlink"])
def test_storage_preflight_rejection_preserves_existing_database(tmp_path, unsafe):
    path = tmp_path / "existing.sqlite"
    path.write_bytes(b"fixture existing database bytes unchanged")
    path.chmod(0o600)
    key = tmp_path / "queue.key"
    key.write_bytes(KEY)
    key.chmod(0o600)
    if unsafe == "db":
        path.chmod(0o644)
    elif unsafe == "parent":
        tmp_path.chmod(0o755)
    elif unsafe == "key":
        key.chmod(0o644)
    elif unsafe == "sidecar":
        sidecar = tmp_path / "existing.sqlite-wal"
        sidecar.write_bytes(b"fixture sidecar")
        sidecar.chmod(0o644)
    else:
        link = tmp_path / "link.sqlite"
        link.symlink_to(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(GatewayError) as error:
        prepare_queue_storage(link if unsafe == "symlink" else path, key)
    assert error.value.code == "queue_security"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert path.stat().st_mode & 0o777 == (0o644 if unsafe == "db" else 0o600)


def test_storage_preflight_creates_only_safe_new_database_without_reading_key(
    tmp_path, monkeypatch
):
    path = tmp_path / "new.sqlite"
    key = tmp_path / "queue.key"
    key.write_bytes(b"fixture key contents validated only by DurableQueue")
    key.chmod(0o600)

    def no_key_read(*_args, **_kwargs):
        raise AssertionError("preflight must not read key contents")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", no_key_read)
        patch.setattr(os, "read", no_key_read)
        prepare_queue_storage(path, key)
    assert path.read_bytes() == b"" and path.stat().st_mode & 0o777 == 0o600
    assert key.read_bytes() == b"fixture key contents validated only by DurableQueue"


def test_storage_preflight_rejection_does_not_create_database(tmp_path):
    path = tmp_path / "new.sqlite"
    key = tmp_path / "queue.key"
    key.write_bytes(KEY)
    key.chmod(0o644)
    with pytest.raises(GatewayError):
        prepare_queue_storage(path, key)
    assert not path.exists()
