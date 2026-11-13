"""Real RFC 9458/bHTTP boundary; no fake encryption or production keys."""

import importlib
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest


def native():
    try:
        return importlib.import_module("quota_broker_ohttp")
    except ImportError:
        pytest.fail("standard OHTTP wheel is required; no plaintext fallback")


def request_bytes(module):
    return module.encode_request(
        "POST",
        "example.com",
        "/quota/api/v1/jobs",
        [
            ("authorization", "Bearer synthetic-test-value"),
            ("date", "Fri, 13 Nov 2026 00:00:00 GMT"),
        ],
        b'{"input":"synthetic confidential input"}',
    )


def test_standard_request_and_response_round_trip():
    m = native()
    server = m.Server(secrets.token_bytes(32), 7)
    plain = request_bytes(m)
    wire, client_response = m.ClientRequest(server.config()).encapsulate(plain)
    assert b"synthetic" not in wire
    assert b"/quota/api/v1/jobs" not in wire
    decoded, server_response = server.decapsulate(wire)
    assert decoded == plain
    assert m.decode_request(decoded) == (
        "POST",
        "example.com",
        "/quota/api/v1/jobs",
        [
            ("authorization", "Bearer synthetic-test-value"),
            ("date", "Fri, 13 Nov 2026 00:00:00 GMT"),
        ],
        b'{"input":"synthetic confidential input"}',
    )
    reply = m.encode_response(201, [("content-type", "application/json")], b'{"answer":"private"}')
    sealed = server_response.encapsulate(reply)
    assert b"private" not in sealed
    assert m.decode_response(client_response.decapsulate(sealed)) == (
        201,
        [("content-type", "application/json")],
        b'{"answer":"private"}',
    )


def test_response_is_bound_to_its_original_request():
    m = native()
    server = m.Server(secrets.token_bytes(32), 7)
    first, response1 = m.ClientRequest(server.config()).encapsulate(request_bytes(m))
    _second, response2 = m.ClientRequest(server.config()).encapsulate(request_bytes(m))
    _decoded, sender = server.decapsulate(first)
    reply = sender.encapsulate(m.encode_response(200, [], b"private"))
    with pytest.raises(ValueError):
        response2.decapsulate(reply)
    assert m.decode_response(response1.decapsulate(reply))[2] == b"private"


@pytest.mark.parametrize("damage", ["last_byte", "truncate", "wrong_key"])
def test_request_tampering_and_wrong_key_fail_closed(damage):
    m = native()
    server = m.Server(secrets.token_bytes(32), 7)
    wire, _response = m.ClientRequest(server.config()).encapsulate(request_bytes(m))
    if damage == "last_byte":
        wire = wire[:-1] + bytes([wire[-1] ^ 1])
    elif damage == "truncate":
        wire = wire[:-1]
    else:
        server = m.Server(secrets.token_bytes(32), 7)
    with pytest.raises(ValueError):
        server.decapsulate(wire)


def test_response_contexts_are_consumed_even_after_failure():
    m = native()
    server = m.Server(secrets.token_bytes(32), 7)
    client = m.ClientRequest(server.config())
    wire, receiver = client.encapsulate(request_bytes(m))
    with pytest.raises(ValueError):
        client.encapsulate(request_bytes(m))
    _decoded, sender = server.decapsulate(wire)
    response = sender.encapsulate(m.encode_response(200, [], b"private"))
    with pytest.raises(ValueError):
        sender.encapsulate(b"second")
    with pytest.raises(ValueError):
        receiver.decapsulate(response[:-1])
    with pytest.raises(ValueError):
        receiver.decapsulate(response)


def test_binary_http_rejects_wrong_message_kind_and_nonzero_trailing_data():
    m = native()
    request = request_bytes(m)
    response = m.encode_response(200, [], b"private")
    with pytest.raises(ValueError):
        m.decode_response(request)
    with pytest.raises(ValueError):
        m.decode_request(response)
    with pytest.raises(ValueError):
        m.decode_request(request + b"smuggled")


def test_fixed_suite_rejects_malformed_config_and_seed():
    m = native()
    with pytest.raises(ValueError):
        m.ClientRequest(b"invalid")
    with pytest.raises(ValueError):
        m.Server(b"short", 7)


def test_binary_http_does_not_allocate_attacker_claimed_length():
    m = native()
    with pytest.raises(ValueError):
        m.decode_request(b"\x00" + b"\xff" * 8)


@pytest.mark.parametrize("header", [("authorization", "a\r\nb"), (":authority", "elsewhere")])
def test_binary_http_does_not_admit_header_injection(header):
    m = native()
    with pytest.raises(ValueError):
        m.encode_request("GET", "example.com", "/quota/api/v1/catalog", [header], b"")


def test_contexts_can_be_finalized_or_used_on_another_python_thread():
    # Exceptions retain frames; cyclic GC may run on a different HTTP thread.
    # The native classes must have compiler-checked Send/Sync, not unsendable Drop.
    m = native()
    server = m.Server(b"s" * 32, 7)
    request = m.ClientRequest(server.config())
    with ThreadPoolExecutor(max_workers=1) as pool:
        wire, client_response = pool.submit(request.encapsulate, b"threaded fixture").result()
        clear, response = server.decapsulate(wire)
        assert clear == b"threaded fixture"
        encrypted = pool.submit(response.encapsulate, b"threaded answer").result()
        assert client_response.decapsulate(encrypted) == b"threaded answer"
