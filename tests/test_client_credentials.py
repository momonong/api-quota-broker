"""Default client endpoint, actual namespace DAC, and volatile export lifecycle."""

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from quota_broker import cli
from quota_broker.client_credentials import ClientCredentialError


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18085",
        "http://localhost:18084",
        "http://[::1]:18084",
        "http://user@127.0.0.1:18084",
        "http://127.0.0.1:18084/",
        "http://127.0.0.1:18084/tasks",
        "http://127.0.0.1:18084?x=1",
        "http://127.0.0.1:18084#fragment",
    ],
)
def test_default_never_reads_or_sends_client_bearer_to_alternative_loopback(url, monkeypatch):
    def forbidden():
        pytest.fail("credential must not be read before fixed endpoint admission")

    monkeypatch.setattr(cli, "read_runtime_client", forbidden)
    args = SimpleNamespace(
        config=None,
        db=None,
        registry_file=None,
        action="catalog",
        token_stdin=False,
        token_file=None,
        url=url,
    )
    with pytest.raises(ClientCredentialError, match="client_runtime_endpoint_not_allowed"):
        cli.gateway_cli(args)


def test_missing_default_is_clear_and_never_uses_stdin_or_http(monkeypatch, capsys):
    def missing():
        raise ClientCredentialError("client_credential_missing_or_unit_inactive")

    def forbidden(*args, **kwargs):
        pytest.fail("no anonymous/sudo/HTTP fallback")

    monkeypatch.setattr(cli, "read_runtime_client", missing)
    monkeypatch.setattr(cli, "_json_http", forbidden)
    with (
        patch.object(sys, "argv", ["quota-broker", "gateway", "catalog"]),
        pytest.raises(SystemExit),
    ):
        cli.main()
    assert (
        json.loads(capsys.readouterr().err)["error"] == "client_credential_missing_or_unit_inactive"
    )


def test_default_run_keeps_prompt_stdin_separate_from_client_credential(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "read_runtime_client", lambda: "public-fixture-client-at-least-32-bytes"
    )

    def fake(url, body, headers, timeout):
        assert body["input"] == "Public prompt only"
        assert body["max_output_tokens"] is None
        return {"state": "public_fixture", "response_truncated": True, "finish_reason": "length"}

    monkeypatch.setattr(cli, "_json_http", fake)
    with (
        patch.object(
            sys,
            "argv",
            [
                "quota-broker",
                "gateway",
                "run",
                "--request-key",
                "new-unique",
                "--capability",
                "text_generation",
            ],
        ),
        patch.object(sys, "stdin", io.StringIO("Public prompt only")),
    ):
        cli.main()
    assert "truncated=True" in capsys.readouterr().out


NAMESPACE_TEST = r"""
import importlib.util,json,os,shutil,tempfile
from pathlib import Path
from quota_broker import client_credentials as client
spec=importlib.util.spec_from_file_location("exporter",Path.cwd()/"deploy/asus/export_client_credential.py")
export=importlib.util.module_from_spec(spec);spec.loader.exec_module(export)
root=Path(tempfile.mkdtemp(prefix="aqb-client-public-fixture-"));root.chmod(0o711)
directory=root/"client";directory.mkdir(mode=0o711);directory.chmod(0o711)
source=root/"public-dummy-credential";source.write_text("public-fixture-client-value-at-least-32");source.chmod(0o400)
export.DIRECTORY=directory;export.CREDENTIAL=source
client.RUNTIME_DIRECTORY=directory
client.unit_ready=lambda:True  # Component DAC fixture; no actual systemd unit is launched.
def child(uid,expect,raw_read=False):
    pid=os.fork()
    if pid==0:
        try:
            os.setgid(uid);os.setuid(uid)
            if raw_read:
                try:(directory/"client_token").read_bytes()
                except PermissionError:os._exit(0)
                os._exit(10)
            try:value=client.read_runtime_client()
            except client.ClientCredentialError as error:
                os._exit(0 if str(error)==expect else 11)
            os._exit(0 if expect=="ready" and value==source_value else 12)
        except BaseException:os._exit(13)
    _,status=os.waitpid(pid,0);assert status==0,(uid,expect,status)
try:
    source_value="public-fixture-client-value-at-least-32"
    export.publish();info=(directory/"client_token").stat()
    assert info.st_uid==info.st_gid==1000 and info.st_mode&0o777==0o600
    child(1000,"ready");child(1001,"client_credential_wrong_user");child(1001,"",raw_read=True)
    client.unit_ready=lambda:False
    child(1000,"client_credential_unit_inactive")
    client.unit_ready=lambda:True
    meta=directory/"metadata.json";data=json.loads(meta.read_text());data["expires_at"]="2000-01-01T00:00:00+00:00";meta.write_text(json.dumps(data))
    child(1000,"client_credential_expired")
    export.cleanup();child(1000,"client_credential_missing_or_unit_inactive")
    # Source key rotation is a PUBLIC fixture only; no new real key is created.
    source_value="public-fixture-refreshed-value-at-least-32";source.write_text(source_value)
    export.publish();child(1000,"ready")
    os.link(directory/"client_token",root/"linked-public-fixture")
    try:export.cleanup()
    except ValueError:pass
    else:raise AssertionError("multi-link must be preserved and refused")
    assert (directory/"client_token").exists();(root/"linked-public-fixture").unlink()
    export.cleanup()
    (directory/"client_token").symlink_to(source)
    try:export.publish()
    except ValueError:pass
    else:raise AssertionError("foreign symlink must be preserved and refused")
    assert (directory/"client_token").is_symlink();(directory/"client_token").unlink()
    # Reboot-like fresh volatile directory preserves neither bearer nor metadata.
    directory.rmdir();directory.mkdir(mode=0o711);directory.chmod(0o711)
    export.publish();child(1000,"ready");export.cleanup()
    print(json.dumps({"status":"passed","namespace_kernel_DAC":True,"morris_UID_read":True,
      "other_UID_raw_read_denied":True,"expired_refused":True,"stop_cleanup":True,
      "restart_refresh":True,"reboot_like_rebuild":True,"foreign_symlink_and_multilink_preserved":True,
      "real_credentials":0,"systemd_units_started":0}))
finally:shutil.rmtree(root)
"""


def test_real_namespace_permissions_and_export_lifecycle():
    if sys.platform != "linux":
        pytest.skip("Linux namespace acceptance only")
    supported = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "/usr/bin/true"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if supported.returncode:
        pytest.skip("UID namespace unavailable; real ASUS UID acceptance remains required")
    result = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-auto",
            "--map-root-user",
            sys.executable,
            "-c",
            NAMESPACE_TEST,
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)
    assert report["other_UID_raw_read_denied"] and report["restart_refresh"]


def test_fixed_unit_preserves_foreign_files_and_refreshes_with_broker():
    source = Path("deploy/asus/api-quota-broker-client.service").read_text()
    assert "After=api-quota-broker.service" in source
    assert "Requires=api-quota-broker.service" in source
    assert "PartOf=api-quota-broker.service" in source
    assert "RemainAfterExit=yes" in source and "RuntimeDirectoryPreserve=yes" in source
    assert "LoadCredentialEncrypted=client_token:" in source
    assert "admin_token" not in source and "doppler" not in source
    assert "ExecStopPost=" in source and "PrivateNetwork=yes" in source
    assert (
        "Wants=api-quota-broker-client.service"
        in Path("deploy/asus/api-quota-broker-v1.service").read_text()
    )


def test_tmpfs_gate_requires_run_mount_not_unrelated_tmpfs():
    import importlib.util

    path = Path("deploy/asus/export_client_credential.py")
    spec = importlib.util.spec_from_file_location("test_client_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module.run_is_tmpfs(
        "1 0 8:1 / /run rw - ext4 /dev/a rw\n2 0 0:5 / /dev/shm rw - tmpfs tmpfs rw"
    )
    assert module.run_is_tmpfs("1 0 0:4 / /run rw - tmpfs tmpfs rw")


def test_unit_state_gate_does_not_treat_stale_ready_files_as_live(monkeypatch):
    from quota_broker import client_credentials as credentials

    monkeypatch.setattr(credentials.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(credentials, "unit_ready", lambda: False)

    def forbidden(*args, **kwargs):
        pytest.fail("inactive unit must refuse before credential file read")

    monkeypatch.setattr(credentials.os, "open", forbidden)
    with pytest.raises(ClientCredentialError, match="client_credential_unit_inactive"):
        credentials.read_runtime_client()
