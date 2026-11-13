"""HP launcher policy overlay and its real encrypted local worker lifecycle."""

import json
import multiprocessing
import socket
import ssl
import time

import pytest
from test_encrypted_api import encrypted_fixture
from test_execution_policy import configure
from test_gateway import task
from test_gateway_access import TOKENS
from test_hp_shared_runtime import module, runtime_fixture
from test_local_jobs import Backend

from quota_broker.client import ClientError, ProjectClient


def test_sensitive_overlay_cannot_replace_existing_project_or_relax_policy():
    runtime = module()
    base = {"version": 1, "principals": [{"project_id": "existing", "token_sha256": "a" * 64}]}
    principal = {
        "project_id": "private",
        "token_sha256": "b" * 64,
        "allowed_execution": ["local_only"],
        "default_execution": "local_only",
        "encryption_required": True,
    }
    result = runtime.sensitive_policy(base, {"version": 1, "principals": [principal]})
    assert result["principals"] == [base["principals"][0], principal]
    assert len(base["principals"]) == 1
    for change in (
        {"project_id": "existing"},
        {"token_sha256": "a" * 64},
        {"allowed_execution": ["local_only", "cloud_only"]},
        {"default_execution": "cloud_only"},
        {"encryption_required": False},
    ):
        with pytest.raises(ValueError):
            runtime.sensitive_policy(base, {"version": 1, "principals": [{**principal, **change}]})


def test_worker_credential_becomes_private_runtime_file_without_printing(tmp_path, capsys):
    runtime = module()
    source, dest = tmp_path / "credential", tmp_path / "worker.token"
    secret = b"synthetic-worker-token-at-least-thirty-two-characters"
    source.write_bytes(secret)
    source.chmod(0o400)
    runtime.copy_worker_credential(source, dest)
    assert dest.read_bytes() == secret
    assert dest.stat().st_mode & 0o777 == 0o600
    assert secret.decode() not in capsys.readouterr().out
    source.chmod(0o644)
    with pytest.raises(ValueError):
        runtime.copy_worker_credential(source, dest)


def runtime_encrypted_child(release, state, run, credential, ports, config, crypto):
    import quota_broker.gateway as gateway_module
    import quota_broker.gateway_server as servers
    from quota_broker.local_jobs import LocalHTTPBackend

    class ImmediateBackend(Backend):
        exact_lookup = True

        def submit(self, key, data):
            result = super().submit(key, data)
            result.update(state="succeeded", result_state="available", ended=time.time())
            return result

    backend = ImmediateBackend()
    backend.token_file = run / "worker.token"
    LocalHTTPBackend.load = lambda _: backend
    real_gateway = gateway_module.Gateway

    def forbidden(*_args):
        raise AssertionError("sensitive work reached a cloud transport")

    gateway_module.Gateway = lambda *a, **kw: real_gateway(
        *a, **kw, transport=forbidden, family_transport=forbidden
    )
    real_server = servers.make_gateway_server

    def tls_private(*a, **kw):
        server = real_server(*a, **kw)
        if kw.get("private_api"):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(crypto / "tls.crt", crypto / "tls.key")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        return server

    servers.make_gateway_server = tls_private
    module().run_runtime(release, state, run, credential, ports=ports, private_config=config)


def test_runtime_encrypted_local_dispatch_and_recreated_client(tmp_path):
    release, state, run, credential = runtime_fixture(tmp_path)
    crypto = tmp_path / "crypto"
    crypto.mkdir(mode=0o700)
    with encrypted_fixture(crypto) as fixture:
        audience = fixture[0]
        configure(fixture[6])
    from urllib.parse import urlsplit

    private_port = urlsplit(audience).port
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        metadata_port = listener.getsockname()[1]
    policy = json.loads((release / "config/access.json").read_text())
    policy["principals"] = [p for p in policy["principals"] if p["project_id"] != "a"]
    (release / "config/access.json").write_text(json.dumps(policy))
    overlay = json.loads((crypto / "access.json").read_text())
    overlay["principals"] = [p for p in overlay["principals"] if p["project_id"] == "a"]
    (crypto / "local-access.json").write_text(json.dumps(overlay))
    (crypto / "local-access.json").chmod(0o600)
    encrypted = {
        name: str(crypto / file)
        for name, file in (
            ("profile", "profile.json"),
            ("manifest", "manifest.json"),
            ("recipients", "recipients.json"),
            ("state", "hp-trust.sqlite"),
            ("replay", "hp-replay.sqlite"),
        )
    }
    (crypto / "encrypted.json").write_text(json.dumps(encrypted))
    (crypto / "encrypted.json").chmod(0o600)
    (crypto / "local.json").write_text("{}")  # replaced by the CPU-only downstream fixture
    config = crypto / "private.json"
    worker_credential = crypto / "worker.credential"
    worker_credential.write_text("synthetic-worker-token-more-than-thirty-two-characters")
    worker_credential.chmod(0o400)
    config.write_text(
        json.dumps(
            {
                "encrypted": str(crypto / "encrypted.json"),
                "local": str(crypto / "local.json"),
                "access": str(crypto / "local-access.json"),
                "worker_credential": str(worker_credential),
            }
        )
    )
    config.chmod(0o600)
    process = multiprocessing.get_context("fork").Process(
        target=runtime_encrypted_child,
        args=(release, state, run, credential, (metadata_port, private_port), config, crypto),
    )
    process.start()

    def client(token=TOKENS["a"]):
        return ProjectClient(
            audience,
            token,
            trust_profile=crypto / "profile.json",
            trust_state=crypto / "consumer.sqlite",
            ssl_context=ssl.create_default_context(cafile=str(crypto / "tls.crt")),
        )

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.is_alive(), "HP runtime stopped before readiness"
            try:
                client().catalog()
                break
            except ClientError:
                time.sleep(0.05)
        else:
            pytest.fail("HP encrypted endpoint not ready")
        c = client()
        result = c.run(
            {**task("hp-local"), "execution_policy": "local_only"},
            wait_timeout=5,
            poll_interval=0.05,
        )
        assert result["result"]["text"] == "private local answer"
        assert client().result("hp-local") == result
        with pytest.raises(ClientError) as error:
            c.submit({**task("cloud-denied"), "execution_policy": "cloud_only"})
        assert error.value.detail["error"] == "forbidden"
        with pytest.raises(ClientError) as error:
            client(TOKENS["b"]).result("hp-local")
        assert error.value.detail["error"] == "not_found"
    finally:
        process.terminate()
        process.join(timeout=8)
        if process.is_alive():
            process.kill()
            process.join()
