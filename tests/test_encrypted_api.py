"""Real TLS and OHTTP, synthetic credentials and a counted cloud fixture only."""

import base64
import json
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import jwt
import pytest
import quota_broker_ohttp as native
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
from test_execution_policy import configure
from test_gateway import task
from test_gateway_access import LEGACY, TOKENS, running

from quota_broker.access import AccessPolicyFile
from quota_broker.client import ClientError, ProjectClient
from quota_broker.crypto_config import TrustProfile
from quota_broker.encrypted_transport import EncryptedEndpoint
from quota_broker.gateway_server import make_gateway_server


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@contextmanager
def encrypted_fixture(tmp_path, *, backend=None, serialize=False):
    with running(tmp_path) as (_base, gateway, queue, calls, policy):
        endpoint = EncryptedEndpoint(
            tmp_path / "profile.json",
            tmp_path / "manifest.json",
            tmp_path / "recipients.json",
            tmp_path / "publisher.sqlite",
            tmp_path / "replay.sqlite",
        )
        local = None
        if backend is not None:
            from quota_broker.access import ProjectAccess
            from quota_broker.local_jobs import LocalJobs

            local = LocalJobs(queue, ProjectAccess(gateway, AccessPolicyFile(policy)), backend)
        server = make_gateway_server(
            gateway,
            LEGACY,
            port=0,
            queue=queue,
            access_policy=AccessPolicyFile(policy),
            private_api=True,
            encrypted_endpoint=endpoint,
            local_jobs=local,
            serialize_work=serialize,
        )
        aud = f"https://127.0.0.1:{server.server_port}/quota/api/v1"
        now = int(datetime.now(UTC).timestamp())
        root, signer = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
        profile = TrustProfile(aud, {"root": b64(root.public_key().public_bytes_raw())}, 1)
        (tmp_path / "profile.json").write_text(
            json.dumps({"audience": aud, "roots": profile.roots, "epoch": 1})
        )
        delegation = jwt.encode(
            {
                "iss": "quota-root",
                "sub": "signer",
                "aud": aud,
                "epoch": 1,
                "purpose": "quota-config-signing",
                "iat": now - 10,
                "exp": now + 86400,
                "public_key": b64(signer.public_key().public_bytes_raw()),
            },
            root,
            algorithm="EdDSA",
            headers={"kid": "root"},
        )
        manifest = jwt.encode(
            {
                "iss": "signer",
                "aud": aud,
                "epoch": 1,
                "version": 1,
                "purpose": "quota-ohttp",
                "profile": "ohttp-v1",
                "iat": now - 5,
                "exp": now + 895,
                "config": b64(native.Server(b"k" * 32, 3).config()),
            },
            signer,
            algorithm="EdDSA",
            headers={"kid": "signer"},
        )
        (tmp_path / "manifest.json").write_text(
            json.dumps({"delegation": delegation, "manifest": manifest})
        )
        keys = tmp_path / "recipients.json"
        keys.write_text(
            json.dumps(
                {
                    "version": 1,
                    "keys": [
                        {
                            "key_id": 3,
                            "seed": b64(b"k" * 32),
                            "not_before": now - 10,
                            "encrypt_until": now + 86390,
                        }
                    ],
                }
            )
        )
        keys.chmod(0o600)
        tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(tls_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(tls_key, hashes.SHA256())
        )
        cert_path, tls_path = tmp_path / "tls.crt", tmp_path / "tls.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        tls_path.write_bytes(
            tls_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        tls_path.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, tls_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        client_tls = ssl.create_default_context(cafile=str(cert_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield aud, profile, client_tls, gateway, queue, calls, policy, endpoint
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def client_for(tmp_path, fixture, who="a"):
    aud, profile, tls, *_ = fixture
    return ProjectClient(
        aud,
        TOKENS[who],
        trust_profile=profile,
        trust_state=tmp_path / f"client-{who}.sqlite",
        ssl_context=tls,
    )


def test_secure_client_requires_trust_before_any_token_io(tmp_path):
    with pytest.raises(ClientError):
        ProjectClient("https://momonong.me/quota", TOKENS["a"])
    with pytest.raises(ClientError):
        ProjectClient("https://momonong.me/quota", TOKENS["a"], security_profile="plaintext")


def test_encrypted_status_during_worker_and_busy_mutation_remains_encrypted(tmp_path):
    from test_local_jobs import Backend

    backend = Backend()
    with encrypted_fixture(tmp_path, backend=backend, serialize=True) as fixture:
        configure(fixture[6])
        client = client_for(tmp_path, fixture)
        client.submit(task("accepted"))
        gate = fixture[3]._http_work_gate
        with gate:
            assert client.catalog()
            assert client.status("accepted")["state"] == "queued"
            with pytest.raises(ClientError) as error:
                client.submit(task("busy"))
            assert error.value.detail["error"] == "work_busy"
        assert backend.posts == []
        assert client.status("accepted")["state"] == "queued"


def test_tls_encrypted_cloud_job_and_new_client_recovery(tmp_path, monkeypatch):
    import quota_broker.secure_client as transport

    observed = []
    original = transport.exchange

    def capture(url, data, headers, timeout, context):
        reply = original(url, data, headers, timeout, context)
        observed.append((url, data, headers, reply))
        return reply

    monkeypatch.setattr(transport, "exchange", capture)
    with encrypted_fixture(tmp_path) as fixture:
        client = client_for(tmp_path, fixture)
        assert client.catalog()
        assert client.explain(task("private-key"))["selected_target_id"] is not None
        assert client.submit(task("private-key"))["state"] == "queued"
        fixture[4].tick("fixture-worker")
        result = client_for(tmp_path, fixture).result("private-key")
        assert result["state"] == "completed"
        assert result["result"]["text"] == "fixture answer"
        assert len(fixture[5]) == 1
        for url, data, headers, reply in observed:
            assert "Authorization" not in headers
            assert url.endswith(("/crypto-config", "/encrypted"))
            for secret in (TOKENS["a"], "fixture input", "private-key", "fixture answer"):
                assert secret.encode() not in (data or b"")
                assert secret.encode() not in reply[2]


def test_sensitive_errors_and_cross_project_results_stay_encrypted(tmp_path):
    with encrypted_fixture(tmp_path) as fixture:
        configure(fixture[6])
        a = client_for(tmp_path, fixture)
        with pytest.raises(ClientError) as error:
            a.submit({**task("secret"), "execution_policy": "cloud_only"})
        assert error.value.detail["error"] == "forbidden"
        with pytest.raises(ClientError) as error:
            client_for(tmp_path, fixture, "b").status("secret")
        assert error.value.detail["error"] == "not_found"
        assert fixture[5] == []


def test_same_encrypted_sdk_local_submit_result_and_isolation(tmp_path):
    from test_local_jobs import Backend

    from quota_broker.access import ProjectAccess
    from quota_broker.local_jobs import LocalJobs

    backend = Backend()
    with encrypted_fixture(tmp_path, backend=backend) as fixture:
        configure(fixture[6])
        client = client_for(tmp_path, fixture)
        assert any(row.get("execution_location") == "local" for row in client.catalog())
        assert client.explain(task("my-local"))["execution_policy"] == "local_only"
        assert client.submit(task("my-local"))["state"] == "queued"
        local = LocalJobs(
            fixture[4], ProjectAccess(fixture[3], AccessPolicyFile(fixture[6])), backend
        )
        local.tick()
        next(iter(backend.jobs.values())).update(
            state="succeeded", result_state="available", ended=fixture[3].clock().timestamp()
        )
        local.tick()
        answer = client_for(tmp_path, fixture).wait("my-local", wait_timeout=1, poll_interval=0.01)
        assert answer["result"]["text"] == "private local answer"
        assert answer["execution_location"] == "local"
        with pytest.raises(ClientError) as error:
            client_for(tmp_path, fixture, "b").result("my-local")
        assert error.value.detail["error"] == "not_found"
        assert fixture[5] == []
        assert len(backend.posts) == 1


def test_untrusted_outer_response_means_unknown_without_second_post(tmp_path, monkeypatch):
    import quota_broker.secure_client as transport

    original = transport.exchange
    sends = []

    def altered(url, data, headers, timeout, context):
        reply = original(url, data, headers, timeout, context)
        if data is not None:
            sends.append(data)
            return 200, {"content-type": "application/json"}, b'{"state":"completed"}'
        return reply

    with encrypted_fixture(tmp_path) as fixture:
        client = client_for(tmp_path, fixture)
        monkeypatch.setattr(transport, "exchange", altered)
        with pytest.raises(ClientError) as error:
            client.submit(task("uncertain"))
        assert error.value.detail["error"] == "transport_unknown"
        assert len(sends) == 1
        monkeypatch.setattr(transport, "exchange", original)
        assert client.status("uncertain")["state"] == "queued"


@pytest.mark.parametrize(
    "path,authority",
    [
        ("/v1/admin/targets/any", None),
        ("/quota/api/v1/encrypted", None),
        ("/quota/api/v1/catalog", "evil.invalid"),
        ("https://evil.invalid", None),
    ],
)
def test_inner_route_cannot_turn_broker_into_proxy(tmp_path, path, authority):
    from urllib.parse import urlsplit

    from quota_broker.secure_client import exchange

    with encrypted_fixture(tmp_path) as fixture:
        aud, _profile, tls, *_ = fixture
        req = native.encode_request(
            "GET",
            authority or urlsplit(aud).netloc,
            path if path.startswith("/") else "/" + path,
            [
                ("authorization", "Bearer " + TOKENS["a"]),
                ("x-quota-created", str(int(datetime.now(UTC).timestamp()))),
                ("x-quota-request", "a" * 32),
            ],
            b"",
        )
        wire, context = native.ClientRequest(native.Server(b"k" * 32, 3).config()).encapsulate(req)
        status, _headers, body = exchange(
            aud + "/encrypted", wire, {"Content-Type": "message/ohttp-req"}, 3, tls
        )
        assert status == 200
        inner_status, _, _ = native.decode_response(context.decapsulate(body))
        assert inner_status == 400
        assert fixture[5] == []
