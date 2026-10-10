"""Offline real-policy discovery API and CLI integration."""

import io
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest
from test_discovery import NOW, attestation, model, snapshot
from test_gateway import HMAC_KEY

from quota_broker import cli, discovery_sources
from quota_broker.discovery import DiscoveryError
from quota_broker.gateway import Gateway
from quota_broker.gateway_server import make_gateway_server

CLIENT = "offline-client-fixture-token-32-characters"
ADMIN = "offline-admin-fixture-token-32-characters"


def forbidden(*_args, **_kwargs):
    raise AssertionError("discovery must not execute providers or resolve credentials")


@pytest.fixture
def api(tmp_path):
    gateway = Gateway(
        tmp_path / "gateway.sqlite",
        (),
        HMAC_KEY,
        forbidden,
        forbidden,
        clock=lambda: NOW + timedelta(seconds=4),
    )
    server = make_gateway_server(gateway, CLIENT, port=0, admin_token=ADMIN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(api, path, body=None, *, token=CLIENT, declared_length=None):
    headers = {"Authorization": "Bearer " + token}
    content = json.dumps(body).encode() if body is not None else None
    if content is not None:
        headers["Content-Type"] = "application/json"
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    req = urllib.request.Request(api[1] + path, content, headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_client_coverage_admin_refresh_attest_and_auth_are_separate(api):
    assert request(api, "/v1/coverage", token=ADMIN)[0] == 401
    assert request(api, "/v1/admin/discovery/refresh", snapshot())[0] == 401
    status, refreshed = request(api, "/v1/admin/discovery/refresh", snapshot(), token=ADMIN)
    assert status == 200 and refreshed["applied"]
    assert request(api, "/v1/admin/discovery/attest", attestation(), token=ADMIN)[0] == 200
    status, page = request(api, "/v1/coverage?provider=nvidia&model=fixture-model&limit=1")
    assert status == 200 and page["summary"]["total"] == 1
    assert page["records"][0]["account_availability"] == "allowed"
    assert page["records"][0]["live_result"] == "unverified"
    assert request(api, "/v1/admin/discovery/fetch", {}, token=ADMIN)[0] == 404
    for path in (
        "/v1/coverage?limit=1001",
        "/v1/coverage?provider=nvidia&provider=groq",
        "/v1/coverage?unknown=x",
        "/v1/coverage/candidates?model=x",
    ):
        status, failure = request(api, path)
        assert status == 400 and failure["error"] == "invalid_request"
    status, failure = request(
        api, "/v1/admin/discovery/refresh", snapshot(raw_message="sensitive-fixture"), token=ADMIN
    )
    assert status == 400 and "sensitive-fixture" not in json.dumps(failure)


def test_large_refresh_bounded_request_and_full_candidate_pages(api):
    payload = snapshot([model(f"public-model-{number:04}") for number in range(2001)])
    assert 65_536 < len(json.dumps(payload).encode()) < 5 * 1024 * 1024
    assert request(api, "/v1/admin/discovery/refresh", payload, token=ADMIN)[0] == 200
    status, first = request(api, "/v1/coverage?provider=nvidia&limit=1000")
    assert status == 200 and len(first["records"]) == 1000
    assert first["summary"]["total"] == 2004  # Includes three historical registry seeds.
    status, bundle = request(api, "/v1/coverage/candidates?provider=nvidia")
    assert status == 200 and len(bundle["target_candidates"]) == 2004
    assert all(
        row["disabled"] and not row["activation_allowed"] for row in bundle["target_candidates"]
    )
    assert request(api, "/v1/tasks", payload)[0] == 400
    assert (
        request(
            api, "/v1/admin/discovery/refresh", {}, token=ADMIN, declared_length=5 * 1024 * 1024 + 1
        )[0]
        == 400
    )


@pytest.fixture
def local(tmp_path, monkeypatch):
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"targets": []}))
    db = tmp_path / "discovery.sqlite"
    monkeypatch.setattr(cli, "utcnow", lambda: NOW + timedelta(seconds=4))
    monkeypatch.setattr(cli, "Gateway", forbidden)
    return ["quota-broker", "gateway", "--json", "--config", str(config), "--db", str(db)]


def invoke(monkeypatch, capsys, argv, raw=None):
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(raw) if raw is not None else ""))
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_local_cli_refresh_attest_coverage_and_candidate_bundle(local, monkeypatch, capsys):
    assert invoke(monkeypatch, capsys, local + ["discovery-refresh"], snapshot())["applied"]
    proof = attestation(live_at=NOW, live_result="passed")
    assert invoke(monkeypatch, capsys, local + ["discovery-attest"], proof)["evidence_eligible"]
    page = invoke(
        monkeypatch,
        capsys,
        local + ["coverage", "--provider", "nvidia", "--model", "fixture-model"],
    )
    assert page["summary"]["total"] == 1 and page["records"][0]["evidence_eligible"]
    bundle = invoke(monkeypatch, capsys, local + ["discovery-candidates", "--provider", "nvidia"])
    assert len(bundle["target_candidates"]) == 4 and not bundle["activation_allowed"]


def test_public_cli_requires_explicit_fixed_provider_and_modalities(local, monkeypatch, capsys):
    calls = []

    def fetch(provider, *, output_modalities, clock):
        calls.append((provider, output_modalities, clock()))
        return snapshot()

    monkeypatch.setattr(discovery_sources, "fetch_public_snapshot", fetch)
    assert invoke(
        monkeypatch,
        capsys,
        local + ["discovery-public", "--provider", "nvidia", "--output-modalities", "all"],
    )["applied"]
    assert calls == [("nvidia", "all", NOW + timedelta(seconds=4))]
    for args in (
        ["discovery-public", "--provider", "nvidia"],
        ["discovery-public", "--provider", "groq", "--output-modalities", "all"],
    ):
        with pytest.raises(SystemExit):
            invoke(monkeypatch, capsys, local + args)
    with pytest.raises(DiscoveryError):
        invoke(
            monkeypatch,
            capsys,
            [
                "quota-broker",
                "gateway",
                "discovery-public",
                "--provider",
                "nvidia",
                "--output-modalities",
                "text",
            ],
        )
    assert len(calls) == 1


def test_http_cli_uses_admin_and_client_routes(api, tmp_path, monkeypatch, capsys):
    credential = tmp_path / "offline-token"
    credential.write_text(ADMIN)
    base = ["quota-broker", "gateway", "--json", "--url", api[1], "--token-file", str(credential)]
    assert invoke(monkeypatch, capsys, base + ["discovery-refresh"], snapshot())["applied"]
    assert (
        invoke(monkeypatch, capsys, base + ["discovery-attest"], attestation())[
            "account_availability"
        ]
        == "allowed"
    )
    credential.write_text(CLIENT)
    assert (
        invoke(
            monkeypatch,
            capsys,
            base + ["coverage", "--provider", "nvidia", "--model", "fixture-model"],
        )["summary"]["total"]
        == 1
    )
    assert not invoke(monkeypatch, capsys, base + ["discovery-candidates", "--provider", "nvidia"])[
        "activation_allowed"
    ]
