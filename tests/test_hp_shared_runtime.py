"""Fixed private runtime wiring with synthetic credentials and offline providers."""

import base64
import importlib.util
import json
from pathlib import Path

import pytest
from test_gateway import HMAC_KEY
from test_shared_credit_admission import setup_credit


def module():
    spec = importlib.util.spec_from_file_location(
        "hp_shared_runtime", Path(__file__).parents[1] / "deploy/hp_shared_runtime.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def credentials():
    return {
        "schema": 1,
        "instance_id": "hp-shared-20261111",
        "digest_key": base64.b64encode(HMAC_KEY).decode(),
        "queue_key": base64.b64encode(b"Q" * 32).decode(),
        "legacy_token": "fixture-reserved-legacy-token-more-than32",
        "provider_keys": {
            "OPENAI_API_KEY": "fixture-openai-key",
            "OPENROUTER_API_KEY": "fixture-openrouter-key",
        },
    }


def test_resolver_is_bound_to_loaded_credentials_not_future_files():
    runtime = module()
    source = credentials()
    resolver, _digest, _queue, _legacy = runtime.credential_values(source, "hp-shared-20261111")
    source["provider_keys"]["OPENAI_API_KEY"] = "changed-after-load"
    assert resolver("OPENAI_API_KEY") == "fixture-openai-key"
    with pytest.raises(ValueError):
        resolver("OTHER_SECRET")
    with pytest.raises(ValueError):
        runtime.credential_values(credentials(), "other-instance")


def test_runtime_never_creates_replacement_ledger_on_restart(tmp_path):
    runtime = module()
    tmp_path.chmod(0o700)
    record = {
        "schema": 1,
        "instance_id": "hp-shared-20261111",
        "historical_gap": True,
        "history_start": "2026-11-11T00:00:00+00:00",
    }
    marker = tmp_path / "instance.json"
    marker.write_text(json.dumps(record))
    marker.chmod(0o600)
    with pytest.raises(ValueError, match="ledger_missing"):
        runtime.check_state(tmp_path, record["instance_id"])
    assert not (tmp_path / "ledger.sqlite3").exists()


def test_listener_policy_rejects_non_loopback_or_shared_ports():
    runtime = module()
    for settings in (
        ("0.0.0.0", 18587, 18588),
        ("127.0.0.1", 18587, 18587),
        ("localhost", 18587, 18588),
    ):
        with pytest.raises(ValueError):
            runtime.listener_settings(*settings)
    assert runtime.listener_settings("127.0.0.1", 18587, 18588) == (18587, 18588)


def test_observation_reimport_cannot_reset_completed_or_unknown_holds(tmp_path):
    runtime = module()
    gateway, item, _calls, clock, _access = setup_credit(tmp_path, unknown=True)
    from test_shared_credit_admission import body

    gateway.run(body("once"))
    with gateway.broker._tx() as con:
        original = gateway.routing.observations(con, "openai:account:fixture-account", clock[0])[0]
    raw = {
        key: original[key]
        for key in (
            "metric",
            "window",
            "remaining",
            "limit",
            "as_of",
            "valid_until",
            "reset_at",
            "provenance",
            "source",
            "confidence",
        )
    }
    runtime.import_observations(gateway, [{"target_id": item.id, "observation": raw}])
    with gateway.broker._tx() as con:
        after = gateway._quota_observations(con, item, clock[0])[0]
    assert after["effective_remaining"] == 100 and after["local_observation_hold"] == 100


def test_persistent_ledger_rejects_changed_instance_keys(tmp_path):
    runtime = module()
    _resolve, digest, queue, _legacy = runtime.credential_values(credentials(), runtime.INSTANCE)
    fingerprint = runtime.credential_fingerprint(digest, queue, runtime.INSTANCE)
    tmp_path.chmod(0o700)
    marker = tmp_path / "instance.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "instance_id": runtime.INSTANCE,
                "historical_gap": True,
                "history_start": "2026-11-11T00:00:00+00:00",
                "credential_fingerprint": fingerprint,
            }
        )
    )
    marker.chmod(0o600)
    ledger = tmp_path / "ledger.sqlite3"
    ledger.touch(mode=0o600)
    assert runtime.check_state(tmp_path, runtime.INSTANCE, fingerprint) == ledger
    with pytest.raises(ValueError, match="state_credential_binding"):
        runtime.check_state(
            tmp_path,
            runtime.INSTANCE,
            runtime.credential_fingerprint(digest, b"X" * 32, runtime.INSTANCE),
        )


def test_release_tree_rejects_writable_files_symlinks_and_hardlinks(tmp_path):
    runtime = module()
    import os

    tree = tmp_path / "release"
    tree.mkdir(mode=0o755)
    child = tree / "file.py"
    child.write_text("fixture = 1\n")
    child.chmod(0o644)
    runtime.verify_tree(tree, owner=os.geteuid())
    child.chmod(0o666)
    with pytest.raises(ValueError, match="release_permissions"):
        runtime.verify_tree(tree, owner=os.geteuid())
    child.chmod(0o644)
    linked = tree / "alias"
    linked.symlink_to(child)
    with pytest.raises(ValueError, match="release_permissions"):
        runtime.verify_tree(tree, owner=os.geteuid())
    linked.unlink()
    os.link(child, linked)
    with pytest.raises(ValueError, match="release_permissions"):
        runtime.verify_tree(tree, owner=os.geteuid())


def test_inventory_snapshot_preserves_validity_and_rejects_unexpected_scope(tmp_path):
    runtime = module()
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    raw = {
        "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
        "verified_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "project": "api-quota-broker",
        "config": "dev",
    }
    inventory = runtime.secret_inventory(raw, tmp_path)
    assert inventory.verified_at == now
    assert inventory.expires_at == now + timedelta(minutes=5)
    with pytest.raises(ValueError):
        runtime.secret_inventory({**raw, "config": "prd"}, tmp_path)


def runtime_fixture(tmp_path):
    """An explicitly initialized instance, never copied from live host data."""
    from datetime import timedelta

    from test_gateway import NOW

    runtime = module()
    release, state, run = [tmp_path / name for name in ("release", "state", "run")]
    for path in (release, state, run):
        path.mkdir(mode=0o700)
    config = release / "config"
    config.mkdir(mode=0o700)
    observed = (NOW - timedelta(seconds=1)).isoformat()
    expires = (NOW + timedelta(hours=1)).isoformat()
    manifest = {
        "schema_version": 1,
        "providers": [
            {
                "id": "openai",
                "adapter": "openai_responses",
                "origin": "https://api.openai.com",
                "endpoint": "https://api.openai.com/v1/responses",
            }
        ],
        "models": [
            {
                "provider": "openai",
                "model": "gpt-6-luna",
                "capability": "text_generation",
                "capabilities": ["text_generation"],
                "context_tokens": 100000,
                "max_output_tokens": 1000,
                "features": ["text_generation", "text"],
            }
        ],
    }
    dimension = {"metric": "credits_usd_micros", "window": "budget"}
    gateway = {
        "targets": [
            {
                "id": "openai",
                "provider": "openai",
                "model": "gpt-6-luna",
                "account_id": "fixture-account",
                "enabled": True,
                "free_eligible": True,
                "billing_enabled": False,
                "verified_at": observed,
                "expires_at": expires,
                "source": "fixture owner evidence",
                "concurrency_limit": 1,
                "max_output_tokens": 64,
                "secret_ref": "OPENAI_API_KEY",
                "local_safety_caps": [
                    {
                        "bucket": "fixture-minute",
                        "metric": "requests",
                        "limit": 5,
                        "window": "rolling_minute",
                        "timezone": "UTC",
                    },
                    {
                        "bucket": "fixture-day",
                        "metric": "requests",
                        "limit": 20,
                        "window": "day",
                        "timezone": "UTC",
                    },
                ],
                "provider_quota_facts": [],
                "required_capacity": [dimension],
                "resource_estimates": [
                    {
                        "metric": "credits_usd_micros",
                        "amount": 100,
                        "max_input_bytes": 4096,
                        "max_output_tokens": 64,
                        "source": "trusted_operator",
                        "verified_at": observed,
                        "expires_at": expires,
                    }
                ],
                "capacity": {
                    "kind": "one_time_gift",
                    "refresh_seconds": None,
                    "as_of": observed,
                    "source": "fixture owner evidence",
                    "scope": "fixture-account",
                    "expires_at": expires,
                    "credit_expires_at": expires,
                },
            }
        ]
    }
    observation = {
        **dimension,
        "remaining": 200,
        "limit": 200,
        "as_of": observed,
        "valid_until": expires,
        "reset_at": None,
        "provenance": "observed",
        "source": "trusted_operator",
        "confidence": "administrator_verified",
    }
    inventory = {
        "names": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
        "verified_at": observed,
        "expires_at": expires,
        "project": "api-quota-broker",
        "config": "dev",
    }
    for name, document in [
        ("registry", manifest),
        ("gateway", gateway),
        ("observations", [{"target_id": "openai", "observation": observation}]),
        ("secret-inventory", inventory),
    ]:
        path = config / (name + ".json")
        path.write_text(json.dumps(document))
        path.chmod(0o600)
    from test_gateway_access import policy_file

    policy_file(config)
    credential = tmp_path / "runtime.json"
    credential.write_text(json.dumps(credentials()))
    credential.chmod(0o600)
    _resolve, digest, key, _legacy = runtime.credential_values(credentials(), runtime.INSTANCE)
    marker = {
        "schema": 1,
        "instance_id": runtime.INSTANCE,
        "historical_gap": True,
        "history_start": NOW.isoformat(),
        "credential_fingerprint": runtime.credential_fingerprint(digest, key, runtime.INSTANCE),
    }
    (state / "instance.json").write_text(json.dumps(marker))
    (state / "instance.json").chmod(0o600)
    (state / "ledger.sqlite3").touch(mode=0o600)
    return release, state, run, credential


def runtime_child(release, state, run, credential, ports, calls_path, unknown):
    import socket

    from test_gateway import NOW
    from test_shared_provider_contracts import responses_body

    import quota_broker.gateway as gateway_module

    real_gateway, real_connect = gateway_module.Gateway, socket.socket.connect

    def restricted_connect(sock, destination):
        if not isinstance(destination, tuple) or destination[0] != "127.0.0.1":
            raise AssertionError("offline runtime test attempted external network")
        return real_connect(sock, destination)

    socket.socket.connect = restricted_connect

    def provider(*_args):
        with calls_path.open("a") as stream:
            stream.write("fixture-call\n")
        if unknown == "delayed":
            import time

            deadline = time.monotonic() + 10
            while not calls_path.with_suffix(".release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("offline fixture drain was not released")
                time.sleep(0.02)
        if unknown is True:
            raise TimeoutError("fixture timeout after dispatch")
        return 200, {}, json.dumps(responses_body()).encode()

    def fixture_gateway(*args, **kwargs):
        return real_gateway(
            *args,
            **kwargs,
            clock=lambda: NOW,
            family_transport=provider,
            transport=lambda *_a: pytest.fail("unexpected legacy transport"),
        )

    gateway_module.Gateway = fixture_gateway
    if unknown == "legacy_factory":
        import quota_broker.gateway_server as servers

        factory = servers.make_gateway_server

        def legacy_factory(*args, **kwargs):
            assert not {"local_jobs", "encrypted_endpoint"} & set(kwargs)
            return factory(*args, **kwargs)

        servers.make_gateway_server = legacy_factory
    module().run_runtime(release, state, run, credential, ports=ports)


@pytest.mark.parametrize("unknown", [False, True, "legacy_factory"])
def test_real_runtime_http_lifecycle_and_restart_without_provider_replay(tmp_path, unknown):
    import multiprocessing
    import socket
    import time
    from contextlib import ExitStack, contextmanager

    from test_gateway_access import request
    from test_shared_credit_admission import body

    release, state, run, credential = runtime_fixture(tmp_path)
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        ports = tuple(listener.getsockname()[1] for listener in sockets)
    calls_path = tmp_path / "fixture-calls"
    metadata, private = [f"http://127.0.0.1:{port}" for port in ports]

    @contextmanager
    def started():
        process = multiprocessing.get_context("fork").Process(
            target=runtime_child, args=(release, state, run, credential, ports, calls_path, unknown)
        )
        process.start()
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                assert process.is_alive(), f"runtime exited {process.exitcode}"
                try:
                    if request(metadata, "web", "GET", "/v1/catalog")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.03)
            else:
                pytest.fail("offline runtime did not become ready")
            yield
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=8)
            if process.is_alive():
                process.kill()
                process.join()
                pytest.fail("runtime did not shut down cleanly")
            assert process.exitcode == 0

    with started():
        assert request(private, "not-a-token", "POST", "/quota/api/v1/jobs", body("once"))[0] == 401
        assert request(metadata, "web", "POST", "/v1/tasks", body("once"))[0] == 403
        assert request(private, "web", "POST", "/quota/api/v1/jobs", body("once"))[0] == 403
        status, submitted = request(private, "a", "POST", "/quota/api/v1/jobs", body("once"))
        assert status == 200, submitted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job = request(private, "a", "GET", "/quota/api/v1/jobs/once")
            if job["state"] in {"completed", "unknown"}:
                break
            time.sleep(0.03)
        assert job["state"] == ("unknown" if unknown is True else "completed")
        assert request(private, "b", "GET", "/quota/api/v1/jobs/once/result")[0] == 404
        status, result = request(private, "a", "GET", "/quota/api/v1/jobs/once/result")
        assert status == 200, result
        if unknown is not True:
            assert result["result"]["text"] == "42"
        status, usage = request(metadata, "web", "GET", "/v1/usage")
        assert status == 200 and usage
        status, diagnostics = request(metadata, "web", "GET", "/v1/diagnostics")
        assert status == 200
        assert diagnostics["targets"][0]["credentials"]["state"] == "present"
    with started():
        assert request(private, "a", "GET", "/quota/api/v1/jobs/once")[1]["state"] == job["state"]
        # Same project/key remains bound to its terminal outcome, never another dispatch.
        assert request(private, "a", "POST", "/quota/api/v1/jobs", body("once"))[0] == 200
    assert calls_path.read_text().splitlines() == ["fixture-call"]


def test_sigterm_waits_for_dispatched_result_to_settle(tmp_path):
    import multiprocessing
    import socket
    import time

    from test_gateway_access import request
    from test_shared_credit_admission import body

    release, state, run, credential = runtime_fixture(tmp_path)
    sockets = [socket.socket(), socket.socket()]
    for listener in sockets:
        listener.bind(("127.0.0.1", 0))
    ports = tuple(listener.getsockname()[1] for listener in sockets)
    for listener in sockets:
        listener.close()
    calls_path = tmp_path / "fixture-calls"
    metadata, private = [f"http://127.0.0.1:{port}" for port in ports]
    process = multiprocessing.get_context("fork").Process(
        target=runtime_child,
        args=(release, state, run, credential, ports, calls_path, "delayed"),
    )
    process.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            assert process.is_alive()
            try:
                if request(metadata, "web", "GET", "/v1/catalog")[0] == 200:
                    break
            except OSError:
                time.sleep(0.03)
        else:
            pytest.fail("offline runtime not ready")
        assert request(private, "a", "POST", "/quota/api/v1/jobs", body("inflight"))[0] == 200
        deadline = time.monotonic() + 5
        while not calls_path.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert calls_path.exists()
        process.terminate()
        process.join(timeout=1.8)
        assert process.is_alive(), "runtime exited before its dispatched result settled"
        calls_path.with_suffix(".release").touch(mode=0o600)
        process.join(timeout=8)
        assert process.exitcode == 0
        import sqlite3

        with sqlite3.connect(state / "ledger.sqlite3") as con:
            assert con.execute("SELECT state FROM queue_jobs").fetchall() == [("completed",)]
        assert calls_path.read_text().splitlines() == ["fixture-call"]
    finally:
        calls_path.with_suffix(".release").touch(mode=0o600)
        if process.is_alive():
            process.terminate()
        process.join(timeout=8)
        if process.is_alive():
            process.kill()
            process.join()


def test_enabled_provider_cannot_borrow_other_provider_credential(tmp_path):
    runtime = module()
    release, state, run, credential = runtime_fixture(tmp_path)
    path = release / "config/gateway.json"
    document = json.loads(path.read_text())
    document["targets"][0]["secret_ref"] = "OPENROUTER_API_KEY"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="enabled_target_requires_capacity"):
        runtime.run_runtime(release, state, run, credential)


@pytest.mark.parametrize(
    "provider,ref",
    [
        ("google", "GEMINI_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("mistral", "MISTRAL_API_KEY"),
        ("nvidia", "NVIDIA_API_KEY"),
        ("ocrspace", "OCRSPACE_API_KEY"),
        ("sambanova", "SAMBANOVA_API_KEY"),
        ("huggingface", "HUGGINGFACE_API_TOKEN"),
        ("cerebras", "CEREBRAS_API_KEY"),
    ],
)
def test_enabled_provider_credentials_use_only_its_fixed_reference(provider, ref):
    from types import SimpleNamespace

    runtime = module()
    target = SimpleNamespace(
        enabled=True,
        provider=provider,
        secret_ref=ref,
        account_id_ref=None,
        required_capacity=(("requests", "day"),),
    )
    names = runtime.enabled_provider_names([target])
    assert names == {ref}
    source = credentials()
    source["provider_keys"] = {ref: "fixture-private-value"}
    resolver, *_ = runtime.credential_values(source, runtime.INSTANCE, required_names=names)
    assert resolver(ref) == "fixture-private-value"
    source["provider_keys"]["OPENAI_API_KEY"] = "fixture-unneeded-value"
    with pytest.raises(ValueError, match="credential_scope"):
        runtime.credential_values(source, runtime.INSTANCE, required_names=names)
    target.secret_ref = "OPENAI_API_KEY"
    with pytest.raises(ValueError, match="enabled_target_requires_capacity"):
        runtime.enabled_provider_names([target])


def test_cloudflare_requires_fixed_account_reference_and_neuron_capacity():
    from types import SimpleNamespace

    runtime = module()
    target = SimpleNamespace(
        enabled=True,
        provider="cloudflare",
        secret_ref="CLOUDFLARE_API_TOKEN",
        account_id_ref="CLOUDFLARE_ACCOUNT_ID",
        required_capacity=(("neurons", "day"),),
    )
    assert runtime.enabled_provider_names([target]) == {
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    }
    target.account_id_ref = "OPENAI_API_KEY"
    with pytest.raises(ValueError):
        runtime.enabled_provider_names([target])
    target.account_id_ref = "CLOUDFLARE_ACCOUNT_ID"
    target.required_capacity = (("requests", "day"),)
    with pytest.raises(ValueError):
        runtime.enabled_provider_names([target])


def test_legacy_two_key_bundle_resolver_hides_disabled_provider():
    runtime = module()
    resolver, *_ = runtime.credential_values(
        credentials(), runtime.INSTANCE, required_names=frozenset({"OPENAI_API_KEY"})
    )
    assert resolver("OPENAI_API_KEY") == "fixture-openai-key"
    with pytest.raises(ValueError, match="credential_reference_not_authorized"):
        resolver("OPENROUTER_API_KEY")
