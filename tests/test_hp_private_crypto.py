"""Real local keys, no production credential, network or systemd mutation."""

import importlib.util
import json
from pathlib import Path

import pytest

from quota_broker.crypto_admin import main as crypto
from quota_broker.crypto_config import ConfigVerifier, TrustProfile
from quota_broker.encrypted_transport import KeyRing


def helper():
    path = Path(__file__).parents[1] / "deploy/hp_private_crypto.py"
    spec = importlib.util.spec_from_file_location("private_crypto", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_keys(tmp_path):
    for name in ("root", "signer"):
        assert (
            crypto(
                [
                    "keygen",
                    "--private",
                    str(tmp_path / (name + ".pem")),
                    "--public",
                    str(tmp_path / (name + ".pub")),
                ]
            )
            == 0
        )
    profile = {
        "audience": "https://momonong.me/quota/api/v1",
        "epoch": 1,
        "roots": {"hp-root-1": (tmp_path / "root.pub").read_text().strip()},
    }
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    return helper()


def test_host_maintained_delegation_refresh_and_monotonic_publish(tmp_path, capsys):
    module = setup_keys(tmp_path)
    module.delegate(tmp_path, tmp_path / "root.pem")
    module.publish(tmp_path, tmp_path / "signer.pem")
    verifier = ConfigVerifier(
        TrustProfile.load(tmp_path / "profile.json"), tmp_path / "consumer.db"
    )
    envelope = lambda: json.loads((tmp_path / "manifest.json").read_text())
    assert verifier.verify(envelope()).version == 1
    initial_keys = (tmp_path / "recipients.json").read_bytes()
    module.publish(tmp_path, tmp_path / "signer.pem")
    assert verifier.verify(envelope()).version == 2
    assert (tmp_path / "recipients.json").read_bytes() == initial_keys
    module.delegate(tmp_path, tmp_path / "root.pem")
    module.publish(tmp_path, tmp_path / "signer.pem")
    assert verifier.verify(envelope()).version == 3
    for value in ("PRIVATE KEY", json.loads(initial_keys)["keys"][0]["seed"]):
        assert value not in capsys.readouterr().out


def test_recipient_rotation_keeps_bounded_overlap_without_root_key(tmp_path):
    module = setup_keys(tmp_path)
    module.delegate(tmp_path, tmp_path / "root.pem")
    module.publish(tmp_path, tmp_path / "signer.pem")
    path = tmp_path / "recipients.json"
    keys = json.loads(path.read_text())
    keys["keys"][0]["not_before"] -= 120
    keys["keys"][0]["encrypt_until"] = int(module.time.time()) + 300
    path.write_text(json.dumps(keys))
    old = keys["keys"][0]["key_id"]
    (tmp_path / "root.pem").unlink()
    module.publish(tmp_path, tmp_path / "signer.pem")
    ring = KeyRing.load(path)
    assert len(ring.keys) == 2
    assert ring.keys[-1].key_id != old
    assert ring.keys[0].encrypt_until <= module.time.time()
    assert ring.keys[-1].encrypt_until - ring.keys[-1].not_before == 86400


def test_missing_publisher_state_never_resets_version(tmp_path):
    module = setup_keys(tmp_path)
    module.delegate(tmp_path, tmp_path / "root.pem")
    module.publish(tmp_path, tmp_path / "signer.pem")
    old = (tmp_path / "manifest.json").read_bytes()
    (tmp_path / "publisher.sqlite").unlink()
    with pytest.raises(ValueError, match="publisher_state_missing"):
        module.publish(tmp_path, tmp_path / "signer.pem")
    assert (tmp_path / "manifest.json").read_bytes() == old


def test_recipient_secrets_only_persist_encrypted_and_restore_after_runtime_loss(tmp_path):
    from cryptography.fernet import Fernet

    module = setup_keys(tmp_path)
    module.delegate(tmp_path, tmp_path / "root.pem")
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    storage_key = Fernet.generate_key()
    module.publish_encrypted(tmp_path, runtime, tmp_path / "signer.pem", storage_key)
    clear = (runtime / "recipients.json").read_bytes()
    ciphertext = (tmp_path / "recipients.fernet").read_bytes()
    assert json.loads(clear)["keys"][0]["seed"].encode() not in ciphertext
    assert not (tmp_path / "recipients.json").exists()
    (runtime / "recipients.json").unlink()
    module.publish_encrypted(tmp_path, runtime, tmp_path / "signer.pem", storage_key)
    assert (runtime / "recipients.json").read_bytes() == clear
    old = (tmp_path / "manifest.json").read_bytes()
    (tmp_path / "recipients.fernet").unlink()
    with pytest.raises(ValueError, match="recipient_storage_missing"):
        module.publish_encrypted(tmp_path, runtime, tmp_path / "signer.pem", storage_key)
    assert (tmp_path / "manifest.json").read_bytes() == old
