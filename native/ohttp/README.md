# OHTTP Python bindings

This internal package wraps `ohttp 0.8.0` and `bhttp 0.8.0` through `PyO3 0.30.1`.
The upstream implementation owns all HPKE, AEAD and binary HTTP processing.
It is based on [RFC 9458](https://www.rfc-editor.org/rfc/rfc9458.html) and
[RFC 9292](https://www.rfc-editor.org/rfc/rfc9292.html). The three direct upstream
dependencies declare MIT OR Apache-2.0 licensing; this wrapper does not replace
their notices or grant a new license for the parent project.

The fixed service profile accepts one X25519/HKDF-SHA256/AES-128-GCM configuration.
It exposes buffered GET/POST requests and final responses, with no streaming,
informational messages or trailers. The 64 MiB native ceiling is an outer bound;
the Python ingress must enforce the deployment's smaller body and concurrency
limits. Header syntax is constrained, but exact origin, route, authorization and
replay enforcement belong to the Broker integration.

Contexts are single-use, including failed operations. Rust/PyO3 checks Send/Sync
and exclusive mutable borrows, so Python can safely collect exception-retained
contexts on another thread. Server contexts are constructed per request from
protected key material; callers do not need to share them. The seed is
input keying material for the upstream KEM derivation, not a raw X25519 private key.
No method exports private keys or includes input in exception messages.

Build with Rust 1.90.0 and the pinned build backend:

```sh
uv run --no-project --python 3.12 --with maturin==1.16.0 maturin build \
  --release --locked --manifest-path native/ohttp/Cargo.toml --out dist \
  --interpreter .venv/bin/python
```

Install the prebuilt wheel on the consumer; Rust is needed only by the builder.
The verified artifact is `cp312-abi3-manylinux_2_34_x86_64` on Python 3.12.3.
Other OS/architecture combinations and Python versions have not been validated.
Do not publish a source-only dependency as a compiler-free SDK delivery.

Development acceptance on 2026-11-13:

- Twelve native boundary tests passed, including cross-thread contexts, after
  observed missing-feature and unsendable-thread failures.
- Three independent conformance tests passed using PyHPKE/cryptography, anchored
  to the public RFC 9458 Appendix A request and response; both native client and
  native server interoperate with that independent HPKE implementation.
- The wheel installed into a separate Python environment and completed an
  encrypted request/response without a compiler or repository import path.
- OSV query of the 99 locked upstream Rust packages plus PyJWT, PyHPKE and
  cryptography returned no known advisories for those versions on that date.
  This is dependency evidence, not an independent cryptographic audit.

Signed discovery, application authentication, durable replay protection and key
rotation are separate parts of the full design. Passing these primitive boundary
tests does not establish production API readiness.

`THIRD_PARTY_LICENSES.txt` preserves dependency notices and is included in the
wheel metadata. The lock includes platform/build dependencies; their presence
does not mean every dependency is linked into this Linux binary.
