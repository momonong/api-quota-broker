"""OHTTP recipient lifecycle and bounded, persistent transport replay protection."""

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crypto_config import (
    ConfigError,
    ConfigVerifier,
    TrustProfile,
    native_module,
    private_file,
    unbase64,
    unique_json,
)


class TransportError(ValueError):
    pass


@dataclass(frozen=True)
class RecipientKey:
    key_id: int
    seed: bytes = field(repr=False)
    not_before: int
    encrypt_until: int

    def __post_init__(self) -> None:
        if (
            type(self.key_id) is not int
            or not 0 <= self.key_id <= 255
            or not isinstance(self.seed, bytes)
            or len(self.seed) != 32
            or type(self.not_before) is not int
            or type(self.encrypt_until) is not int
            or not 0 < self.encrypt_until - self.not_before <= 86400
        ):
            raise TransportError("invalid recipient key")


class KeyRing:
    def __init__(self, keys: list[RecipientKey], *, clock: Callable[[], float] = time.time):
        if not 1 <= len(keys) <= 3 or len({k.key_id for k in keys}) != len(keys):
            raise TransportError("invalid recipient keyring")
        self.keys, self.clock = tuple(keys), clock

    @classmethod
    def load(cls, path: str | Path, *, clock: Callable[[], float] = time.time) -> "KeyRing":
        try:
            path = Path(path)
            private_file(path)
            with path.open("rb") as stream:
                data = unique_json(stream.read(65537))
            if set(data) != {"version", "keys"} or data["version"] != 1:
                raise ValueError
            keys = []
            for item in data["keys"]:
                if set(item) != {"key_id", "seed", "not_before", "encrypt_until"}:
                    raise ValueError
                keys.append(RecipientKey(**{**item, "seed": unbase64(item["seed"], 64)}))
            return cls(keys, clock=clock)
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            raise TransportError("recipient keys unavailable") from None

    def config(self) -> bytes:
        current = [k for k in self.keys if k.not_before <= self.clock() < k.encrypt_until]
        if not current:
            raise TransportError("recipient keys unavailable")
        key = max(current, key=lambda k: k.not_before)
        return bytes(native_module().Server(key.seed, key.key_id).config())

    def decapsulate(self, wire: bytes) -> tuple[bytes, Any]:
        if not wire or len(wire) > 64 * 1024 * 1024:
            raise TransportError("invalid encrypted request")
        for key in self.keys:
            if key.key_id == wire[0] and key.not_before <= self.clock() < key.encrypt_until + 1200:
                try:
                    # Contexts stay on this request thread; no shared mutable HPKE state.
                    decoded, response = (
                        native_module().Server(key.seed, key.key_id).decapsulate(wire)
                    )
                    return bytes(decoded), response
                except ValueError:
                    break
        raise TransportError("invalid encrypted request")


class ReplayStore:
    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time, capacity: int = 100000
    ):
        if type(capacity) is not int or not 1 <= capacity <= 1000000:
            raise TransportError("invalid replay capacity")
        self.path, self.clock, self.capacity = Path(path), clock, capacity

    def accept(self, wire: bytes, request_id: str, created: int) -> None:
        now = int(self.clock())
        if (
            type(created) is not int
            or abs(now - created) > 120
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", request_id)
        ):
            raise TransportError("invalid transport freshness")
        try:
            private_file(self.path, create=True)
            with closing(sqlite3.connect(self.path, timeout=15)) as con, con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS replay (digest TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS replay_clock (id INTEGER PRIMARY KEY CHECK(id=1), seen INTEGER NOT NULL)"
                )
                row = con.execute("SELECT seen FROM replay_clock WHERE id=1").fetchone()
                if row and now < row[0]:
                    raise TransportError("replay clock rollback")
                con.execute("DELETE FROM replay WHERE expires < ?", (now,))
                if con.execute("SELECT count(*) FROM replay").fetchone()[0] >= self.capacity:
                    raise TransportError("replay capacity exhausted")
                con.execute(
                    "INSERT INTO replay VALUES(?,?,?)",
                    (hashlib.sha256(wire).hexdigest(), request_id, created + 120),
                )
                con.execute("INSERT OR REPLACE INTO replay_clock VALUES(1,?)", (now,))
        except (OSError, sqlite3.Error, ConfigError):
            raise TransportError("transport replay unavailable or duplicate") from None


class EncryptedEndpoint:
    """Reload operator-managed files; never generate or renew trust automatically."""

    def __init__(
        self,
        profile: str | Path,
        manifest: str | Path,
        recipients: str | Path,
        state: str | Path,
        replay: str | Path,
    ):
        self.profile_file, self.manifest_file = Path(profile), Path(manifest)
        self.recipients_file, self.state_file = Path(recipients), Path(state)
        self.replay = ReplayStore(replay)

    @classmethod
    def load(cls, path: str | Path) -> "EncryptedEndpoint":
        with Path(path).open("rb") as stream:
            data = unique_json(stream.read(65537))
        if (
            set(data) != {"profile", "manifest", "recipients", "state", "replay"}
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in data.values())
            or len(set(data.values())) != 5
        ):
            raise TransportError("invalid encrypted service configuration")
        endpoint = cls(**data)
        endpoint.public_config()
        return endpoint

    @property
    def profile(self) -> TrustProfile:
        return TrustProfile.load(self.profile_file)

    def public_config(self) -> dict[str, Any]:
        try:
            with self.manifest_file.open("rb") as stream:
                envelope = unique_json(stream.read(65537))
            verified = ConfigVerifier(self.profile, self.state_file).verify(envelope)
            if verified.config != KeyRing.load(self.recipients_file).config():
                raise TransportError("configuration and recipients differ")
            return dict(envelope)
        except (OSError, ValueError, TypeError, RecursionError):
            raise TransportError("encrypted service unavailable") from None

    def decapsulate(self, wire: bytes) -> tuple[bytes, Any]:
        return KeyRing.load(self.recipients_file).decapsulate(wire)
