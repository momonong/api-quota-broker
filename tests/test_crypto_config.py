import base64
import json
from dataclasses import replace

import jwt
import pytest
import quota_broker_ohttp as native
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quota_broker.crypto_config import ConfigError, ConfigVerifier, TrustProfile

NOW = 1800000000
AUD = "https://momonong.me/quota/api/v1"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def setup_config(tmp_path):
    root, signer = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    profile = TrustProfile(AUD, {"root-1": b64(root.public_key().public_bytes_raw())}, epoch=1)
    delegation = {
        "iss": "quota-root",
        "aud": AUD,
        "sub": "signer-1",
        "epoch": 1,
        "purpose": "quota-config-signing",
        "iat": NOW - 10,
        "exp": NOW + 86400,
        "public_key": b64(signer.public_key().public_bytes_raw()),
    }
    manifest = {
        "iss": "signer-1",
        "aud": AUD,
        "epoch": 1,
        "version": 1,
        "purpose": "quota-ohttp",
        "profile": "ohttp-v1",
        "iat": NOW - 5,
        "exp": NOW + 895,
        "config": b64(native.Server(b"s" * 32, 1).config()),
    }

    def signed(d=None, m=None, root_key=None, signer_key=None, header=None):
        return {
            "delegation": jwt.encode(
                d or delegation,
                root_key or root,
                algorithm="EdDSA",
                headers={"kid": "root-1", "typ": "JWT"},
            ),
            "manifest": jwt.encode(
                m or manifest,
                signer_key or signer,
                algorithm="EdDSA",
                headers=header or {"kid": "signer-1", "typ": "JWT"},
            ),
        }

    return profile, delegation, manifest, signed


def test_verified_config_and_persistent_monotonic_version(tmp_path):
    profile, _d, m, signed = setup_config(tmp_path)
    state = tmp_path / "trust.sqlite"
    first = ConfigVerifier(profile, state, clock=lambda: NOW).verify(signed())
    assert first.config == native.Server(b"s" * 32, 1).config()
    assert first.cache_until <= NOW + 600
    assert ConfigVerifier(profile, state, clock=lambda: NOW).verify(signed()).version == 1
    ConfigVerifier(profile, state, clock=lambda: NOW).verify(signed(m={**m, "version": 2}))
    with pytest.raises(ConfigError):
        ConfigVerifier(profile, state, clock=lambda: NOW).verify(signed())
    # The state is public metadata, without private material or content.
    assert b"BEGIN PRIVATE KEY" not in state.read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("aud", "https://evil.invalid/quota/api/v1"),
        ("purpose", "other"),
        ("profile", "plaintext"),
        ("epoch", 2),
        ("epoch", True),
        ("version", True),
        ("exp", NOW - 1),
        ("exp", NOW + 901),
        ("iat", NOW + 1),
        ("config", "untrusted"),
    ],
)
def test_bad_manifest_does_not_poison_version(tmp_path, field, value):
    profile, _d, m, signed = setup_config(tmp_path)
    verifier = ConfigVerifier(profile, tmp_path / "state", clock=lambda: NOW)
    with pytest.raises(ConfigError):
        verifier.verify(signed(m={**m, "version": 999, field: value}))
    assert verifier.verify(signed()).version == 1


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "other"},
        {"exp": NOW + 30 * 86400 + 1},
        {"exp": NOW - 1},
        {"aud": "https://evil.invalid"},
        {"epoch": 2},
    ],
)
def test_delegation_limits_are_not_extended_by_online_signer(tmp_path, change):
    profile, _d, _m, signed = setup_config(tmp_path)
    verifier = ConfigVerifier(profile, tmp_path / "state", clock=lambda: NOW)
    with pytest.raises(ConfigError):
        verifier.verify(signed(d={**_d, **change}))


def test_wrong_root_and_remote_key_locator_rejected(tmp_path):
    profile, _d, _m, signed = setup_config(tmp_path)
    verifier = ConfigVerifier(profile, tmp_path / "state", clock=lambda: NOW)
    with pytest.raises(ConfigError):
        verifier.verify(signed(root_key=Ed25519PrivateKey.generate()))
    with pytest.raises(ConfigError):
        verifier.verify(signed(header={"kid": "signer-1", "jku": "https://evil.invalid"}))


def test_equivocation_clock_rollback_and_corrupt_state_fail_closed(tmp_path):
    profile, _d, m, signed = setup_config(tmp_path)
    path = tmp_path / "state"
    ConfigVerifier(profile, path, clock=lambda: NOW).verify(signed())
    with pytest.raises(ConfigError):
        ConfigVerifier(profile, path, clock=lambda: NOW).verify(signed(m={**m, "exp": NOW + 800}))
    with pytest.raises(ConfigError):
        ConfigVerifier(profile, path, clock=lambda: NOW - 1).verify(signed())
    path.write_bytes(b"not sqlite")
    with pytest.raises(ConfigError):
        ConfigVerifier(profile, path, clock=lambda: NOW).verify(signed())


def test_trust_profile_is_independently_loaded_and_strict(tmp_path):
    profile, _, _, _ = setup_config(tmp_path)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"audience": AUD, "roots": profile.roots, "epoch": 1}))
    assert TrustProfile.load(path) == profile
    path.write_text('{"audience":"x","audience":"y"}')
    with pytest.raises(ConfigError):
        TrustProfile.load(path)
    with pytest.raises(ConfigError):
        replace(profile, audience="http://momonong.me/quota/api/v1")


def test_state_cannot_be_reused_for_a_different_trust_epoch(tmp_path):
    profile, _, _, signed = setup_config(tmp_path)
    path = tmp_path / "state"
    ConfigVerifier(profile, path, clock=lambda: NOW).verify(signed())
    with pytest.raises(ConfigError):
        ConfigVerifier(replace(profile, epoch=2), path, clock=lambda: NOW).verify(signed())


def test_root_authorized_signer_rotation_preserves_version_floor(tmp_path):
    profile, delegation, manifest, signed = setup_config(tmp_path)
    verifier = ConfigVerifier(profile, tmp_path / "state", clock=lambda: NOW)
    verifier.verify(signed())
    next_signer = Ed25519PrivateKey.generate()
    rotated = signed(
        d={
            **delegation,
            "sub": "signer-2",
            "public_key": b64(next_signer.public_key().public_bytes_raw()),
        },
        m={**manifest, "iss": "signer-2", "version": 2},
        signer_key=next_signer,
        header={"kid": "signer-2", "typ": "JWT"},
    )
    assert verifier.verify(rotated).version == 2
    with pytest.raises(ConfigError):
        verifier.verify(signed())


def test_compromised_signer_high_version_recovers_only_via_independent_epoch(tmp_path):
    profile, delegation, manifest, signed = setup_config(tmp_path)
    old_state = tmp_path / "old-state"
    verifier = ConfigVerifier(profile, old_state, clock=lambda: NOW)
    verifier.verify(signed(m={**manifest, "version": 999999}))
    with pytest.raises(ConfigError):
        verifier.verify(signed())
    new_root = Ed25519PrivateKey.generate()
    new_profile = replace(
        profile, epoch=2, roots={"root-1": b64(new_root.public_key().public_bytes_raw())}
    )
    independently_issued = signed(
        d={**delegation, "epoch": 2}, m={**manifest, "epoch": 2}, root_key=new_root
    )
    with pytest.raises(ConfigError):
        verifier.verify(independently_issued)
    with pytest.raises(ConfigError):
        ConfigVerifier(new_profile, old_state, clock=lambda: NOW).verify(independently_issued)
    assert (
        ConfigVerifier(new_profile, tmp_path / "new-state", clock=lambda: NOW)
        .verify(independently_issued)
        .version
        == 1
    )
