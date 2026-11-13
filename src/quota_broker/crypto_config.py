"""Pinned trust, standard Ed25519 JWS, and durable public configuration versions.

The independently distributed profile is the trust anchor. A network manifest
cannot introduce a root, change the audience, or reset the local version floor.
"""

import base64
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class ConfigError(ValueError):
    pass


def unique_json(raw: bytes | str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError("invalid configuration")
            result[key] = value
        return result

    if len(raw) > 65536:
        raise ConfigError("configuration too large")
    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ConfigError("invalid JSON")),
    )


def unbase64(value: object, maximum: int = 65536) -> bytes:
    if not isinstance(value, str) or len(value) > maximum or "=" in value:
        raise ConfigError("invalid encoding")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(result).rstrip(b"=").decode() != value:
            raise ValueError
        return result
    except ValueError:
        raise ConfigError("invalid encoding") from None


def private_file(path: Path, *, create: bool = False) -> None:
    """Owner-controlled directory is required; reject links and permissive files."""
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ConfigError("private state file required")


def native_module() -> Any:
    try:
        return importlib.import_module("quota_broker_ohttp")
    except ImportError:
        raise ConfigError("install the encrypted SDK extra and platform wheel") from None


@dataclass(frozen=True)
class TrustProfile:
    audience: str
    roots: dict[str, str]
    epoch: int
    minimum_version: int = 1

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.audience)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path != "/quota/api/v1"
                or any(ord(c) <= 32 for c in self.audience)
                or not isinstance(self.roots, dict)
                or not 1 <= len(self.roots) <= 4
                or type(self.epoch) is not int
                or self.epoch < 1
                or type(self.minimum_version) is not int
                or self.minimum_version < 1
            ):
                raise ValueError
            for key, value in self.roots.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 64:
                    raise ValueError
                Ed25519PublicKey.from_public_bytes(unbase64(value, 64))
        except (ValueError, TypeError, AttributeError):
            raise ConfigError("invalid trust profile") from None

    @classmethod
    def load(cls, path: str | Path) -> "TrustProfile":
        try:
            with Path(path).open("rb") as stream:
                data = unique_json(stream.read(65537))
            return cls(**data)
        except (OSError, ValueError, TypeError, RecursionError):
            raise ConfigError("trust profile unavailable") from None


@dataclass(frozen=True)
class VerifiedConfig:
    config: bytes
    version: int
    expires_at: int
    cache_until: int


def _jws(token: object, keys: dict[str, str]) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > 32768:
        raise ConfigError("invalid signature")
    try:
        jwt = importlib.import_module("jwt")
    except ImportError:
        raise ConfigError("install the encrypted SDK extra") from None
    try:
        header_raw, payload_raw, _sig = token.split(".")
        header, payload = unique_json(unbase64(header_raw)), unique_json(unbase64(payload_raw))
        if (
            not isinstance(header, dict)
            or set(header) != {"alg", "kid", "typ"}
            or header["alg"] != "EdDSA"
            or header["typ"] != "JWT"
            or not isinstance(header["kid"], str)
            or header["kid"] not in keys
            or not isinstance(payload, dict)
        ):
            raise ConfigError("invalid signature")
        key = Ed25519PublicKey.from_public_bytes(unbase64(keys[header["kid"]]))
        # JWS verifies the original payload bytes. Claims are checked below using
        # the explicit trusted clock; no key locator or algorithm negotiation.
        jwt.api_jws.decode_complete(token, key=key, algorithms=["EdDSA"])
        return payload
    except (jwt.exceptions.PyJWTError, ValueError, TypeError, KeyError, RecursionError):
        # Library exceptions may embed untrusted header/payload text.
        raise ConfigError("signature verification failed") from None


def _window(claims: dict[str, Any], now: int, maximum: int) -> None:
    start, end = claims.get("iat"), claims.get("exp")
    if (
        type(start) is not int
        or type(end) is not int
        or not start <= now < end
        or not 0 < end - start <= maximum
    ):
        raise ConfigError("configuration outside validity window")


class ConfigVerifier:
    def __init__(
        self, profile: TrustProfile, state: str | Path, *, clock: Callable[[], float] = time.time
    ):
        self.profile, self.state, self.clock = profile, Path(state), clock

    def verify(self, envelope: object) -> VerifiedConfig:
        try:
            if not isinstance(envelope, dict) or set(envelope) != {"delegation", "manifest"}:
                raise ConfigError("invalid configuration")
            now = int(self.clock())
            delegation = _jws(envelope["delegation"], self.profile.roots)
            if (
                set(delegation)
                != {"iss", "aud", "sub", "epoch", "purpose", "iat", "exp", "public_key"}
                or delegation["iss"] != "quota-root"
                or delegation["aud"] != self.profile.audience
                or type(delegation["epoch"]) is not int
                or delegation["epoch"] != self.profile.epoch
                or delegation["purpose"] != "quota-config-signing"
                or not isinstance(delegation["sub"], str)
            ):
                raise ConfigError("invalid delegation")
            _window(delegation, now, 30 * 86400)
            manifest = _jws(envelope["manifest"], {delegation["sub"]: delegation["public_key"]})
            if (
                set(manifest)
                != {"iss", "aud", "epoch", "version", "purpose", "profile", "iat", "exp", "config"}
                or manifest["iss"] != delegation["sub"]
                or manifest["aud"] != self.profile.audience
                or type(manifest["epoch"]) is not int
                or manifest["epoch"] != self.profile.epoch
                or manifest["purpose"] != "quota-ohttp"
                or manifest["profile"] != "ohttp-v1"
                or type(manifest["version"]) is not int
                or manifest["version"] < self.profile.minimum_version
                or manifest["iat"] < delegation["iat"]
                or manifest["exp"] > delegation["exp"]
            ):
                raise ConfigError("invalid manifest")
            _window(manifest, now, 900)
            config = unbase64(manifest["config"], 256)
            native_module().ClientRequest(config)  # Enforce the one fixed suite.
            fingerprint = hashlib.sha256(envelope["manifest"].encode()).hexdigest()
            anchor = hashlib.sha256(
                json.dumps(
                    {
                        "audience": self.profile.audience,
                        "roots": self.profile.roots,
                        "epoch": self.profile.epoch,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            private_file(self.state, create=True)
            with closing(sqlite3.connect(self.state, timeout=15)) as con, con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS trust (id INTEGER PRIMARY KEY CHECK(id=1), anchor TEXT, version INTEGER, digest TEXT, seen INTEGER)"
                )
                row = con.execute(
                    "SELECT anchor,version,digest,seen FROM trust WHERE id=1"
                ).fetchone()
                if row and (
                    row[0] != anchor
                    or manifest["version"] < row[1]
                    or (manifest["version"] == row[1] and fingerprint != row[2])
                    or now < row[3]
                ):
                    raise ConfigError("configuration rollback or equivocation")
                con.execute(
                    "INSERT OR REPLACE INTO trust VALUES(1,?,?,?,?)",
                    (anchor, manifest["version"], fingerprint, now),
                )
            return VerifiedConfig(
                config, manifest["version"], manifest["exp"], min(now + 600, manifest["exp"])
            )
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError):
            raise ConfigError("configuration verification failed") from None
