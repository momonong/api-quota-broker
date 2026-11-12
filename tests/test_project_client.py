"""Project client against the actual private HTTP facade; fixture provider only."""

import json

import pytest
from test_family_adapters import GOOGLE
from test_gateway_access import TOKENS, running

from quota_broker.client import ClientError


def test_project_client_submit_status_result_and_cross_project_isolation(tmp_path):
    from quota_broker.client import ProjectClient

    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):

        def transport(request, *_args):
            calls.append(request)
            return 200, {}, json.dumps(GOOGLE).encode()

        gateway.family_transport = transport
        a, b = ProjectClient(base, TOKENS["a"]), ProjectClient(base, TOKENS["b"])
        submitted = a.submit_text("business-1", "hello", max_output_tokens=16)
        assert submitted["request_key"] == "business-1"
        queue.tick("fixture-worker")
        assert a.status("business-1")["state"] == "completed"
        assert a.result("business-1")["result"]["text"] == "answer"
        assert len(calls) == 1
        with pytest.raises(ClientError):
            b.result("business-1")
        a.submit_text("business-1", "hello", max_output_tokens=16)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.68.67:18588",
        "https://momonong.me/quota",
        "http://127.0.0.1:18588/other",
        "http://user:pass@127.0.0.1:18588",
        "http://127.0.0.1:18588?x=y",
    ],
)
def test_client_requires_local_private_tunnel_destination(url):
    from quota_broker.client import ProjectClient

    with pytest.raises(ClientError):
        ProjectClient(url, TOKENS["a"])


def test_client_transport_failure_never_retries_or_relabels_request(monkeypatch):
    from quota_broker.client import ProjectClient

    calls = []

    def fail(url, data, headers, timeout):
        calls.append((url, data))
        raise TimeoutError()

    monkeypatch.setattr("quota_broker.client._json_http", fail)
    client = ProjectClient("http://127.0.0.1:18588", TOKENS["a"])
    with pytest.raises(ClientError) as exc:
        client.submit_text("durable-business-key", "hello", max_output_tokens=16)
    assert exc.value.detail == {
        "error": "transport_unknown",
        "request_key": "durable-business-key",
        "action": "query_original_job",
    }
    assert len(calls) == 1 and calls[0][1]["request_key"] == "durable-business-key"


def test_project_request_keys_match_gateway_contract():
    from quota_broker.client import ProjectClient

    for value in ("~existing", ".existing", "_existing", "-existing"):
        assert ProjectClient._key(value) == value
    with pytest.raises(ClientError):
        ProjectClient._key("invalid:key")


def test_call_free_api_one_call_helper(tmp_path):
    from quota_broker.client import call_free_api

    with running(tmp_path, private=True) as (base, gateway, queue, calls, _policy):

        def transport(request, *_args):
            calls.append(request)
            return 200, {}, json.dumps(GOOGLE).encode()

        gateway.family_transport = transport
        submitted = call_free_api(
            "hello",
            token=TOKENS["a"],
            base_url=base,
            request_key="helper-call-1",
            max_output_tokens=16,
        )
        assert submitted["request_key"] == "helper-call-1"
        queue.tick("fixture-worker")
        assert len(calls) == 1

