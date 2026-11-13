import json

from quota_broker.crypto_admin import main
from quota_broker.crypto_config import ConfigVerifier, TrustProfile


def test_operator_workflow_only_uses_explicit_private_files(tmp_path, capsys):
    root, signer = tmp_path / "root.pem", tmp_path / "signer.pem"
    root_pub, signer_pub = tmp_path / "root.pub", tmp_path / "signer.pub"
    for key, public in ((root, root_pub), (signer, signer_pub)):
        assert main(["keygen", "--private", str(key), "--public", str(public)]) == 0
        assert key.stat().st_mode & 0o777 == 0o600
    assert main(["keygen", "--private", str(root), "--public", str(root_pub)]) != 0
    profile = tmp_path / "profile.json"
    audience = "https://momonong.me/quota/api/v1"
    profile.write_text(
        json.dumps(
            {"audience": audience, "epoch": 1, "roots": {"root": root_pub.read_text().strip()}}
        )
    )
    delegation, recipients, manifest = (
        tmp_path / name for name in ("delegation.jws", "recipients.json", "manifest.json")
    )
    assert (
        main(
            [
                "delegate",
                "--root-key",
                str(root),
                "--root-id",
                "root",
                "--signer-public",
                str(signer_pub),
                "--signer-id",
                "online",
                "--profile",
                str(profile),
                "--out",
                str(delegation),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "recipient",
                "--out",
                str(recipients),
                "--key-id",
                "1",
                "--public",
                str(tmp_path / "recipient-public.json"),
            ]
        )
        == 0
    )
    args = [
        "publish",
        "--signer-key",
        str(signer),
        "--delegation",
        str(delegation),
        "--profile",
        str(profile),
        "--recipient-config",
        str(tmp_path / "recipient-public.json"),
        "--out",
        str(manifest),
        "--state",
        str(tmp_path / "publisher.db"),
        "--version",
        "1",
    ]
    assert main(args) == 0
    verifier = ConfigVerifier(TrustProfile.load(profile), tmp_path / "client.db")
    assert verifier.verify(json.loads(manifest.read_text())).version == 1
    args[-1] = "2"
    assert main(args) == 0
    assert verifier.verify(json.loads(manifest.read_text())).version == 2
    output = capsys.readouterr().out + capsys.readouterr().err
    assert "PRIVATE KEY" not in output
    assert json.loads(recipients.read_text())["keys"][0]["seed"] not in output
