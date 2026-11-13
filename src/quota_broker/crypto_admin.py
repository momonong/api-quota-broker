"""Explicit offline key/delegation and online manifest operations; never a daemon.

Run keygen/delegate on the offline administrator device. Only the delegated
signer and recipient material belong on their respective trusted services.
"""

import argparse
import base64
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto_config import (
    ConfigVerifier,
    TrustProfile,
    _jws,
    native_module,
    private_file,
    unbase64,
    unique_json,
)
from .encrypted_transport import KeyRing


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _replace(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        private_file(path)
    fd, name = tempfile.mkstemp(prefix=".quota-crypto-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _key(path: Path) -> Ed25519PrivateKey:
    private_file(path)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Ed25519 key required")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    generate = sub.add_parser("keygen")
    generate.add_argument("--private", type=Path, required=True)
    generate.add_argument("--public", type=Path, required=True)
    delegate = sub.add_parser("delegate")
    for name in ("root-key", "signer-public", "profile", "out"):
        delegate.add_argument("--" + name, type=Path, required=True)
    delegate.add_argument("--root-id", required=True)
    delegate.add_argument("--signer-id", required=True)
    delegate.add_argument("--days", type=int, default=30)
    recipient = sub.add_parser("recipient")
    recipient.add_argument("--out", type=Path, required=True)
    recipient.add_argument("--key-id", type=int, required=True)
    recipient.add_argument("--public", type=Path, required=True)
    publish = sub.add_parser("publish")
    for name in ("signer-key", "delegation", "profile", "recipient-config", "out", "state"):
        publish.add_argument("--" + name, type=Path, required=True)
    publish.add_argument("--version", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        jwt = importlib.import_module("jwt")
        now = int(time.time())
        if args.operation == "keygen":
            if args.private.exists() or args.public.exists() or args.private == args.public:
                raise ValueError("output already exists")
            key = Ed25519PrivateKey.generate()
            _new(
                args.private,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            _new(args.public, (_b64(key.public_key().public_bytes_raw()) + "\n").encode())
        elif args.operation == "delegate":
            profile = TrustProfile.load(args.profile)
            key = _key(args.root_key)
            if (
                not 1 <= args.days <= 30
                or not 1 <= len(args.signer_id) <= 64
                or profile.roots.get(args.root_id) != _b64(key.public_key().public_bytes_raw())
            ):
                raise ValueError("invalid root or delegation")
            claims = {
                "iss": "quota-root",
                "aud": profile.audience,
                "sub": args.signer_id,
                "epoch": profile.epoch,
                "purpose": "quota-config-signing",
                "iat": now,
                "exp": now + args.days * 86400,
                "public_key": args.signer_public.read_text().strip(),
            }
            token = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": args.root_id})
            _new(args.out, token.encode())
        elif args.operation == "recipient":
            if not 0 <= args.key_id <= 255:
                raise ValueError("invalid recipient id")
            retained = []
            if args.out.exists():
                previous = KeyRing.load(args.out)
                for item in previous.keys:
                    until = min(item.encrypt_until, now)
                    if until + 1200 > now:
                        if item.key_id == args.key_id or until <= item.not_before:
                            raise ValueError("recipient overlap or id collision")
                        retained.append(
                            {
                                "key_id": item.key_id,
                                "seed": _b64(item.seed),
                                "not_before": item.not_before,
                                "encrypt_until": until,
                            }
                        )
            if len(retained) >= 3:
                raise ValueError("recipient rotation too frequent")
            retained.append(
                {
                    "key_id": args.key_id,
                    "seed": _b64(os.urandom(32)),
                    "not_before": now,
                    "encrypt_until": now + 86400,
                }
            )
            _replace(args.out, json.dumps({"version": 1, "keys": retained}).encode())
            config = KeyRing.load(args.out).config()
            _replace(
                args.public,
                json.dumps(
                    {"config": _b64(config), "not_before": now, "encrypt_until": now + 86400}
                ).encode(),
            )
        else:
            profile = TrustProfile.load(args.profile)
            delegation = args.delegation.read_text().strip()
            claims = _jws(delegation, profile.roots)
            key = _key(args.signer_key)
            if claims["public_key"] != _b64(key.public_key().public_bytes_raw()):
                raise ValueError("signer differs from delegation")
            public = unique_json(args.recipient_config.read_bytes())
            if (
                set(public) != {"config", "not_before", "encrypt_until"}
                or type(public["not_before"]) is not int
                or type(public["encrypt_until"]) is not int
                or not public["not_before"] <= now < public["encrypt_until"]
                or not 0 < public["encrypt_until"] - public["not_before"] <= 86400
            ):
                raise ValueError("invalid public recipient configuration")
            config = unbase64(public["config"], 256)
            native_module().ClientRequest(config)
            manifest = {
                "iss": claims["sub"],
                "aud": profile.audience,
                "epoch": profile.epoch,
                "version": args.version,
                "purpose": "quota-ohttp",
                "profile": "ohttp-v1",
                "iat": now,
                "exp": min(now + 900, claims["exp"], public["encrypt_until"]),
                "config": _b64(config),
            }
            envelope = {
                "delegation": delegation,
                "manifest": jwt.encode(
                    manifest, key, algorithm="EdDSA", headers={"kid": claims["sub"]}
                ),
            }
            ConfigVerifier(profile, args.state).verify(envelope)
            _replace(args.out, json.dumps(envelope).encode())
        print(json.dumps({"operation": args.operation, "status": "written"}))
        return 0
    except (ValueError, TypeError, KeyError, OSError, ImportError):
        print('{"error":"crypto_admin_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
