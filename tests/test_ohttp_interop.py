"""Independent HPKE interop, anchored to the public RFC 9458 Appendix A vector.

Only this test uses PyHPKE and implements the RFC response formula. Production
uses the pinned upstream OHTTP library; the published keys below protect nothing.
"""

import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pyhpke import AEADId, CipherSuite, KDFId, KEMId
from test_ohttp_native import native, request_bytes


def suite():
    return CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES128_GCM)


def response_material(s, ctx, enc, nonce):
    prk = s.kdf.extract(enc + nonce, ctx.export(b"message/bhttp response", 16))
    return s.kdf.expand(prk, b"key", 16), s.kdf.expand(prk, b"nonce", 12)


def test_public_rfc9458_vector_anchors_independent_oracle():
    # https://www.rfc-editor.org/rfc/rfc9458.html#appendix-A
    s = suite()
    public_test_private_key = bytes.fromhex(
        "3c168975674b2fa8e465970b79c8dcf09f1c741626480bd4c6162fc5b6a98e1a"
    )
    wire = bytes.fromhex(
        "010020000100014b28f881333e7c164ffc499ad9796f877f4e1051ee6d31bad1"
        "9dec96c208b4726374e469135906992e1268c594d2a10c695d858c40a026e796"
        "5e7d86b83dd440b2c0185204b4d63525"
    )
    ctx = s.create_recipient_context(
        wire[7:39],
        s.kem.deserialize_private_key(public_test_private_key),
        b"message/bhttp request\x00" + wire[:7],
    )
    plain = ctx.open(wire[39:])
    assert plain.hex() == "00034745540568747470730b6578616d706c652e636f6d012f"
    assert native().decode_request(plain) == ("GET", "example.com", "/", [], b"")
    response = bytes.fromhex(
        "c789e7151fcba46158ca84b04464910d86f9013e404feea014e7be4a441f234f857fbd"
    )
    key, nonce = response_material(s, ctx, wire[7:39], response[:16])
    assert key.hex() == "5d0172a080e428b16d298c4ea0db620d"
    assert nonce.hex() == "f6bf1aeb88d6df87007fa263"
    assert native().decode_response(AESGCM(key).decrypt(nonce, response[16:], b"")) == (
        200,
        [],
        b"",
    )


def test_native_client_interoperates_with_independent_hpke_and_aead():
    m, s, seed = native(), suite(), secrets.token_bytes(32)
    server = m.Server(seed, 7)
    wire, receiver = m.ClientRequest(server.config()).encapsulate(request_bytes(m))
    pair = s.kem.derive_key_pair(seed)
    ctx = s.create_recipient_context(
        wire[7:39], pair.private_key, b"message/bhttp request\x00" + wire[:7]
    )
    assert ctx.open(wire[39:]) == request_bytes(m)
    response_nonce = secrets.token_bytes(16)
    key, nonce = response_material(s, ctx, wire[7:39], response_nonce)
    wire_response = response_nonce + AESGCM(key).encrypt(nonce, b"\x01\x40\xc8", b"")
    assert m.decode_response(receiver.decapsulate(wire_response)) == (200, [], b"")


def test_native_server_interoperates_with_independent_hpke_and_aead():
    m, s, seed = native(), suite(), secrets.token_bytes(32)
    server = m.Server(seed, 7)
    pair = s.kem.derive_key_pair(seed)
    header = bytes.fromhex("07002000010001")
    enc, ctx = s.create_sender_context(pair.public_key, b"message/bhttp request\x00" + header)
    plain, sender = server.decapsulate(header + enc + ctx.seal(request_bytes(m)))
    assert plain == request_bytes(m)
    wire_response = sender.encapsulate(b"\x01\x40\xc8")
    key, nonce = response_material(s, ctx, enc, wire_response[:16])
    assert m.decode_response(AESGCM(key).decrypt(nonce, wire_response[16:], b"")) == (200, [], b"")
