"""Two fixed SSH capabilities, tested without host mutation or real credentials."""

import base64
import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "access_bootstrap", Path(__file__).parents[1] / "deploy/hp_shared_access_bootstrap.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def public_key(byte):
    kind = b"ssh-ed25519"
    raw = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


def payload():
    helper = b"# reviewed root helper fixture\n"
    return {
        "schema": "hp-quota-shared-access-v1",
        "operation_id": "acc93705-d44c-47b2-95a1-e9b69a48ef4c",
        "helper": {
            "size": len(helper),
            "sha256": hashlib.sha256(helper).hexdigest(),
            "content_base64": base64.b64encode(helper).decode(),
        },
        "deploy_public_key": public_key(1),
        "client_public_key": public_key(2),
        "runtime_launcher_sha256": gate.RUNTIME_PIN,
        "unit_sha256": gate.UNIT_PIN,
    }


def validate(value):
    return gate.validate_payload(
        value, gate.digest(gate.canonical(value)), value["helper"]["sha256"]
    )


def test_exact_fixed_payload_and_public_keys_only():
    value = payload()
    assert validate(value)["helper"] == b"# reviewed root helper fixture\n"
    for key in (
        public_key(1) + " comment",
        "command=evil " + public_key(1),
        "ssh-rsa AAAA",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ):
        changed = payload()
        changed["deploy_public_key"] = key
        with pytest.raises(gate.Refused):
            validate(changed)
    for field, replacement in (
        ("runtime_launcher_sha256", "0" * 64),
        ("unit_sha256", "0" * 64),
        ("client_public_key", value["deploy_public_key"]),
    ):
        changed = payload()
        changed[field] = replacement
        with pytest.raises(gate.Refused):
            validate(changed)
    value["command"] = "arbitrary"
    with pytest.raises(gate.Refused):
        validate(value)


def test_private_client_cannot_get_sessions_or_other_forwarding():
    text = gate.SSH_TEXT
    assert text.endswith("Match all\n")
    deploy, client = text.split("Match User quota-broker-client\n")
    assert "DisableForwarding yes" in deploy and "AllowTcpForwarding no" in deploy
    assert "AllowTcpForwarding local" in client
    assert "PermitOpen 127.0.0.1:18588" in client
    assert "PermitListen none" in client
    assert "AllowStreamLocalForwarding no" in client
    assert "MaxSessions 0" in client and "PermitTTY no" in client
    assert gate.SUDOERS_TEXT.strip().endswith('quota-shared-maintenance-root ""')
    assert "-I -B" not in gate.SUDOERS_TEXT


def test_candidate_config_preserves_include_order_without_writing_live_file():
    original = b"# main\nInclude /etc/ssh/sshd_config.d/*.conf\nPort 22\n"
    included = [
        Path("/etc/ssh/sshd_config.d/10-existing.conf"),
        Path("/etc/ssh/sshd_config.d/99-existing.conf"),
    ]
    candidate = gate.candidate_main(original, included, "/proc/self/fd/42")
    assert candidate.index(b"10-existing") < candidate.index(b"/proc/self/fd/42")
    assert candidate.index(b"/proc/self/fd/42") < candidate.index(b"99-existing")
    assert b"Port 22" in candidate
    with pytest.raises(gate.Refused):
        gate.candidate_main(b"Port 22\n", included, "/proc/self/fd/42")


def test_effective_configuration_requires_exact_user_capabilities():
    for user in (gate.DEPLOY, gate.CLIENT):
        fields = gate.expected_effective(user)
        gate.check_effective(user, fields)
        broken = dict(fields)
        broken["allowtcpforwarding"] = "yes"
        with pytest.raises(gate.Refused):
            gate.check_effective(user, broken)
    broken = gate.expected_effective(gate.CLIENT)
    broken["maxsessions"] = "1"
    with pytest.raises(gate.Refused):
        gate.check_effective(gate.CLIENT, broken)


def test_read_token_must_match_existing_read_plan_principal():
    token = "R" * 48
    policy = {
        "version": 1,
        "principals": [
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "project_id": None,
                "scopes": ["read", "plan"],
            }
        ],
    }
    gate.check_read_token(token, policy)
    policy["principals"][0]["scopes"].append("execute")
    with pytest.raises(gate.Refused):
        gate.check_read_token(token, policy)


def test_exclusive_claim_is_never_replayed(tmp_path):
    receipt = tmp_path / "receipt"
    gate.write_new(receipt, b'{"status":"claimed"}')
    with pytest.raises(FileExistsError):
        gate.write_new(receipt, b"replacement")
    assert receipt.read_bytes() == b'{"status":"claimed"}'


@pytest.mark.parametrize("failure", [None, "ssh_validation", "reload_uncertain"])
def test_apply_validates_before_enable_and_quarantines_only_new_access_on_failure(
    tmp_path, monkeypatch, failure
):
    paths = {
        "ENTRY": tmp_path / "libexec/helper.py",
        "FORCED": tmp_path / "libexec/forced",
        "ROOT_ENTRY": tmp_path / "libexec/root",
        "POLICY_DIR": tmp_path / "etc/policy",
        "JOURNAL": tmp_path / "var/journal",
        "LOCK": tmp_path / "run/update.lock",
        "RECEIPT": tmp_path / "var/receipt.json",
        "READ_TOKEN": tmp_path / "etc/creds/read-token",
        "SSH_CONFIG": tmp_path / "etc/ssh/sshd_config",
        "SSH_SNIPPET": tmp_path / "etc/ssh/sshd_config.d/92-quota-shared.conf",
        "SUDOERS": tmp_path / "etc/sudoers.d/92-quota-shared",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(gate, name, path)
    monkeypatch.setattr(gate, "OWNER", os.getuid())
    monkeypatch.setattr(gate, "root_directory", lambda path: None)
    paths["SSH_CONFIG"].write_bytes(b"original existing SSH configuration\n")
    paths["SSH_CONFIG"].chmod(0o644)
    original = paths["SSH_CONFIG"].read_bytes()
    snapshot = {
        "old": {"morris": {"fixture": "unchanged"}, "quota-deploy": {}},
        "main_raw": original,
        "included": [],
        "included_hashes": {},
    }
    monkeypatch.setattr(gate, "preflight", lambda *_: snapshot)
    events, encrypted_input = [], []
    token = "R" * 48

    def command(args, *, input_data=None, pass_fds=(), timeout=30):
        assert paths["RECEIPT"].exists(), "claim must precede any host mutation"
        events.append(args)
        if "encrypt" in args:
            encrypted_input.append(input_data)
            return b"encrypted fixture blob"
        if "decrypt" in args:
            return token.encode()
        if args == [str(paths["ROOT_ENTRY"])]:
            return b'{"status":"ready"}'
        if "reload" in args and failure == "reload_uncertain":
            raise TimeoutError("synthetic host outcome unknown")
        return b""

    monkeypatch.setattr(gate, "command", command)

    def installed(old):
        events.append(["validated-scoped-ssh"])
        assert old == snapshot["old"]
        if failure == "ssh_validation":
            raise gate.Refused("sshd_capability_mismatch")

    monkeypatch.setattr(gate, "installed_ssh", installed)
    value = payload()
    result = gate.apply(value, validate(value), token)
    assert result["status"] == ("complete" if failure is None else "needs_reconcile")
    assert result["provider_calls"] == 0
    assert encrypted_input == [token.encode()]
    assert paths["READ_TOKEN"].read_bytes() == b"encrypted fixture blob"
    assert paths["SSH_CONFIG"].read_bytes() == original
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes()
    reloads = [event for event in events if "reload" in event]
    if failure == "ssh_validation":
        assert reloads == []
    else:
        assert len(reloads) == 1
    if failure is None:
        enabled = [i for i, event in enumerate(events) if "usermod" in str(event) and "" in event]
        assert len(enabled) == 2 and all(
            i > events.index(["validated-scoped-ssh"]) for i in enabled
        )
        assert paths["SSH_SNIPPET"].exists()
    else:
        assert not paths["SSH_SNIPPET"].exists()
        assert not paths["SUDOERS"].exists()
        assert not (paths["POLICY_DIR"] / "keys" / gate.CLIENT).exists()
        assert not (paths["POLICY_DIR"] / "keys" / gate.DEPLOY).exists()
        assert list(paths["SSH_SNIPPET"].parent.glob("*.disabled-*"))
        assert all(not ("usermod" in str(event) and "" in event) for event in events)
        assert result["cleanup_uncertain"] is False


def test_candidate_memfd_remains_readable_after_validator_closes_inherited_fds(monkeypatch):
    import subprocess
    import sys

    original = b"Include /etc/ssh/sshd_config.d/*.conf\nPort 22\n"
    old = {
        "morris": {"fixture": "morris"},
        "quota-deploy": {"fixture": "quota-deploy"},
        "other-existing-service": {"fixture": "unchanged"},
    }
    checked = []

    def command(args, *, input_data=None, pass_fds=(), timeout=30):
        path = args[-1]
        assert path.startswith(f"/proc/{os.getpid()}/fd/")
        script = """import os,pathlib,sys
path=sys.argv[1]
os.closerange(3,256)
raw=pathlib.Path(path).read_text()
if "Include " in raw:
    linked=raw.split("Include ",1)[1].splitlines()[0]
    assert "Match User quota-broker-client" in pathlib.Path(linked).read_text()
else:
    assert raw.rstrip().endswith('quota-shared-maintenance-root ""')
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, path], capture_output=True, check=False
        )
        assert result.returncode == 0
        checked.append(args[0])
        return b""

    def effective(user, *, config=None, pass_fds=()):
        assert config.startswith(f"/proc/{os.getpid()}/fd/")
        return gate.expected_effective(user) if user in {gate.DEPLOY, gate.CLIENT} else old[user]

    monkeypatch.setattr(gate, "command", command)
    monkeypatch.setattr(gate, "effective", effective)
    gate.validate_candidate_ssh(original, [], old)
    assert checked == ["/usr/sbin/sshd", "/usr/sbin/visudo"]
