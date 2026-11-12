"""Tunnel arguments and cleanup are verified with fake SSH processes."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "quota_project_tunnel_candidate", SCRIPTS / "quota_project_tunnel.py"
)
tunnel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tunnel)


@pytest.mark.parametrize("port", [True, 0, 65536, "18588"])
def test_invalid_port_never_reads_credentials(port, monkeypatch):
    monkeypatch.setattr(
        tunnel.client.credentials, "get", lambda _: pytest.fail("secret fetch forbidden")
    )
    with pytest.raises(ValueError), tunnel.project_tunnel(port):
        pass


def test_tunnel_fixed_forward_identity_and_context_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "_port_available", lambda _: None)
    monkeypatch.setattr(tunnel, "_wait_ready", lambda *_: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(tunnel.client, "RUNTIME", runtime)
    names = []
    monkeypatch.setattr(
        tunnel.client.credentials,
        "get",
        lambda name: (
            names.append(name)
            or "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
        ),
    )
    processes = []

    class Process:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.stopped = False
            self.returncode = None
            processes.append(self)
            self.path = Path(argv[argv.index("-i") + 1])
            assert self.path.exists()
            assert argv[-1] == "quota-broker-client@192.168.68.67"
            assert argv[argv.index("-L") + 1] == "127.0.0.1:18589:127.0.0.1:18588"
            assert "-N" in argv and "ExitOnForwardFailure=yes" in argv
            assert "ProxyCommand=none" in argv and "ForwardAgent=no" in argv

        def poll(self):
            return self.returncode

        def terminate(self):
            self.stopped = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(tunnel.subprocess, "Popen", Process)
    with tunnel.project_tunnel(18589) as opened:
        assert opened is processes[0] and opened.path.exists()
    assert names == ["QUOTA_SHARED_TUNNEL_SSH_PRIVATE_KEY"]
    assert processes[0].stopped and not processes[0].path.exists()


def test_early_ssh_failure_cleans_key_without_exposing_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "_port_available", lambda _: None)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(tunnel.client, "RUNTIME", runtime)
    monkeypatch.setattr(
        tunnel.client.credentials,
        "get",
        lambda _: "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----",
    )

    class Process:
        def __init__(self, *a, **k):
            pass

        def poll(self):
            return 255

        def wait(self, timeout=None):
            return 255

    monkeypatch.setattr(tunnel.subprocess, "Popen", Process)
    with pytest.raises(ValueError, match="tunnel_start_failed"), tunnel.project_tunnel():
        pass
    assert list(runtime.iterdir()) == []


def test_readiness_timeout_is_fixed_error_without_network(monkeypatch):
    clock = iter([0, 13])
    monkeypatch.setattr(tunnel.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="tunnel_start_failed"):
        tunnel._wait_ready(object(), 18588)
