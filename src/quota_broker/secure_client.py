"""SDK-only transport: verified configuration before secrets, one exchange per call."""

import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .client import ClientError, _NoRedirect
from .crypto_config import (
    ConfigError,
    ConfigVerifier,
    TrustProfile,
    VerifiedConfig,
    native_module,
    unique_json,
)

MAX_RESPONSE = 32 * 1024 * 1024 + 65536


def exchange(
    url: str, data: bytes | None, headers: dict[str, str], timeout: float, context: ssl.SSLContext
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": f"api-quota-broker/{__version__}", **headers},
        method="POST" if data is not None else "GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        wire = response.read(MAX_RESPONSE + 1)
        if len(wire) > MAX_RESPONSE:
            raise ValueError("response exceeds limit")
        return response.status, {k.lower(): v for k, v in response.headers.items()}, wire


class SecureTransport:
    def __init__(self, audience: str, profile: object, state: object, context: object):
        try:
            if profile is None:
                profile = os.environ.get("QUOTA_TRUST_PROFILE")
            if isinstance(profile, (str, Path)):
                profile = TrustProfile.load(profile)
            if not isinstance(profile, TrustProfile) or profile.audience != audience:
                raise ConfigError("matching independent trust profile required")
            native_module()  # Missing native support fails before even public network I/O.
            if context is None:
                context = ssl.create_default_context()
            if (
                not isinstance(context, ssl.SSLContext)
                or not context.check_hostname
                or context.verify_mode != ssl.CERT_REQUIRED
            ):
                raise ConfigError("verified TLS required")
            if state is None:
                directory = Path.home() / ".cache" / "quota-broker"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                state = directory / (hashlib.sha256(audience.encode()).hexdigest() + ".sqlite")
            if not isinstance(state, (str, Path)):
                raise ConfigError("trust state path required")
            self.verifier = ConfigVerifier(profile, state)
        except (ValueError, OSError):
            raise ClientError(
                "encrypted SDK trust configuration unavailable",
                detail={"error": "trust_unavailable", "action": "configure_trust"},
            ) from None
        self.audience, self.context = audience, context
        self.cache: VerifiedConfig | None = None
        self.envelope: object = None
        self.lock = threading.Lock()

    def _config(self, timeout: float) -> VerifiedConfig:
        with self.lock:
            try:
                if self.cache is not None and time.time() < self.cache.cache_until:
                    # Verify the clock/version again; do not extend the original cache window.
                    self.verifier.verify(self.envelope)
                    return self.cache
                status, headers, raw = exchange(
                    self.audience + "/crypto-config",
                    None,
                    {"Accept": "application/json"},
                    timeout,
                    self.context,
                )
                if (
                    status != 200
                    or headers.get("content-type", "").split(";", 1)[0] != "application/json"
                ):
                    raise ConfigError("configuration unavailable")
                self.envelope = unique_json(raw)
                self.cache = self.verifier.verify(self.envelope)
                return self.cache
            except (ValueError, OSError, HTTPException):
                raise ClientError(
                    "encrypted SDK configuration verification failed",
                    detail={"error": "trust_unavailable", "action": "check_trusted_configuration"},
                ) from None

    def request(self, url: str, data: dict | None, token: str, timeout: float) -> object:
        config = self._config(timeout)
        native = native_module()
        parsed = urlsplit(url)
        if not url.startswith(self.audience + "/") or parsed.query or parsed.fragment:
            raise ClientError("invalid encrypted operation")
        body = b"" if data is None else json.dumps(data, allow_nan=False).encode()
        if len(body) > 16 * 1024 * 1024:
            raise ClientError("request exceeds limit", detail={"error": "invalid_request"})
        headers = [
            ("authorization", "Bearer " + token),
            ("content-type", "application/json"),
            ("x-quota-created", str(int(time.time()))),
            ("x-quota-request", uuid.uuid4().hex),
        ]
        binary = native.encode_request(
            "GET" if data is None else "POST", parsed.netloc, parsed.path, headers, body
        )
        wire, response_context = native.ClientRequest(config.config).encapsulate(binary)
        try:
            status, outer_headers, response = exchange(
                self.audience + "/encrypted",
                wire,
                {"Content-Type": "message/ohttp-req", "Accept": "message/ohttp-res"},
                timeout,
                self.context,
            )
            if status != 200 or outer_headers.get("content-type") != "message/ohttp-res":
                raise ValueError
            status, fields, clear = native.decode_response(response_context.decapsulate(response))
            if fields != [("content-type", "application/json")]:
                raise ValueError
            # Business responses have the existing bounded JSON contract, not the
            # smaller configuration limit. Reject pathological nesting before parsing.
            from .gateway_server import _json_depth

            _json_depth(clear)
            result = json.loads(clear)
        except (ValueError, OSError, HTTPException, RecursionError):
            raise ClientError(
                "encrypted exchange outcome unknown",
                detail={"error": "transport_unknown", "action": "query_original_job"},
            ) from None
        if status >= 400:
            if not isinstance(result, dict):
                raise ClientError(
                    "invalid encrypted response", detail={"error": "transport_unknown"}
                )
            raise ClientError(f"broker HTTP {status}", detail=result)
        return result
