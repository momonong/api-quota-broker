"""Private key entry: fake Doppler only; no real secret or provider request."""

import http.client
import json
import re
import subprocess
import threading
from urllib.parse import urlencode

import pytest

from quota_broker.key_admin import (
    AdminError,
    DopplerCLIWriter,
    KeyState,
    MetadataStore,
    make_key_admin_server,
)


class FakeDoppler:
    def __init__(self):
        self.projects = {"api-provider-nvidia"}
        self.configs = {"api-provider-nvidia": {"dev"}}
        self.names = {"api-provider-nvidia": {"NVIDIA_API_KEY"}}
        self.calls = []
        self.fail_set = False

    def __call__(self, command, value):
        self.calls.append((command, value))
        verb = next(name for name in ("projects", "configs", "secrets") if name in command)
        args = command[command.index(verb) :]
        if verb == "projects":
            rows = [{"id": project} for project in sorted(self.projects)]
            return subprocess.CompletedProcess(command, 0, json.dumps(rows).encode(), b"")
        if verb == "configs":
            project = args[args.index("--project") + 1]
            rows = [{"name": name} for name in self.configs[project]]
            return subprocess.CompletedProcess(command, 0, json.dumps(rows).encode(), b"")
        project = args[args.index("--project") + 1]
        if "set" in args:
            if self.fail_set:
                return subprocess.CompletedProcess(command, 1, b"", b"bad")
            self.names[project].add(args[2])
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(
            command, 0, json.dumps(sorted(self.names[project])).encode(), b""
        )


def test_shared_scope_groq_stdin_and_replacement(tmp_path):
    binary = tmp_path / "doppler"
    binary.write_text("fixture")
    fake = FakeDoppler()
    writer = DopplerCLIWriter(str(binary), tmp_path, fake)
    assert writer.state("nvidia") == KeyState(True, True)
    assert writer.state("groq") == KeyState(False, True)
    writer.save("groq", "fixture-key-one")
    assert writer.state("groq") == KeyState(True, True)
    writer.save("groq", "fixture-key-two")
    assert "NVIDIA_API_KEY" in fake.names["api-provider-nvidia"]
    writes = [(command, value) for command, value in fake.calls if "set" in command]
    assert [value for _, value in writes] == [b"fixture-key-one", b"fixture-key-two"]
    assert all(
        "GROQ_API_KEY" in command and "api-provider-nvidia" in command for command, _ in writes
    )
    assert not any("create" in command for command, _ in fake.calls)
    assert all("fixture-key" not in " ".join(command) for command, _ in writes)
    assert not any(value for command, value in fake.calls if "set" not in command)


def test_missing_shared_scope_rejects_both_providers_without_create(tmp_path):
    binary = tmp_path / "doppler"
    binary.write_text("fixture")
    fake = FakeDoppler()
    fake.projects.clear()
    writer = DopplerCLIWriter(str(binary), tmp_path, fake)
    for provider in ("nvidia", "groq"):
        with pytest.raises(AdminError, match="shared_scope_missing"):
            writer.save(provider, "fixture-key")
    assert not any("create" in command or "set" in command for command, _ in fake.calls)


def test_failed_write_does_not_claim_configured(tmp_path):
    binary = tmp_path / "doppler"
    binary.write_text("fixture")
    fake = FakeDoppler()
    fake.fail_set = True
    writer = DopplerCLIWriter(str(binary), tmp_path, fake)
    with pytest.raises(AdminError, match="doppler_write_failed"):
        writer.save("nvidia", "fixture-key")
    assert writer.state("nvidia") == KeyState(True, True)  # old name is still present
    with pytest.raises(AdminError, match="invalid_provider"):
        writer.save("other", "fixture-key")


class MemoryWriter:
    def __init__(self):
        self.saved = []
        self.fail = False

    def state(self, provider):
        return KeyState(any(name == provider for name, _ in self.saved), True)

    def save(self, provider, value):
        if self.fail:
            raise AdminError("doppler_write_failed")
        self.saved.append((provider, value))


def test_http_login_csrf_origin_save_and_no_secret_echo(tmp_path):
    writer = MemoryWriter()
    metadata = MetadataStore(tmp_path / "metadata.json")
    token = "a" * 48
    server = make_key_admin_server(writer, metadata, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_port}"

    def request(method, path, fields=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = urlencode(fields).encode() if fields is not None else None
        base = {"Host": host}
        if body is not None:
            base["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body, base | (headers or {}))
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    try:
        assert request("GET", "/admin")[0] == 303
        assert request("GET", "/admin/login", headers={"Host": "attacker.invalid"})[0] == 403
        assert (
            request("POST", "/admin/login", {"token": token}, {"Origin": "http://evil"})[0] == 403
        )
        assert (
            request("POST", "/admin/login", {"token": "wrong"}, {"Origin": "http://" + host})[0]
            == 401
        )
        status, headers, _ = request(
            "POST", "/admin/login", {"token": token}, {"Origin": "http://" + host}
        )
        assert status == 303
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, page = request("GET", "/admin", headers={"Cookie": cookie})
        assert status == 200 and headers["Cache-Control"] == "no-store"
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        fields = {
            "csrf": csrf,
            "provider": "groq",
            "key": "fixture-secret-key",
            "key_name": "personal",
            "key_id": "id-1",
            "expiry": "2027-09-29",
        }
        base = {"Cookie": cookie, "Origin": "http://" + host}
        assert (
            request("POST", "/admin/save", fields, {"Cookie": cookie, "Origin": "http://evil"})[0]
            == 403
        )
        assert request("POST", "/admin/save", fields | {"csrf": "wrong"}, base)[0] == 403
        assert writer.saved == []
        assert (
            request("POST", "/admin/save", fields | {"key_name": "fixture-secret-key"}, base)[0]
            == 400
        )
        assert request("POST", "/admin/save", fields | {"provider": "other"}, base)[0] == 400
        assert writer.saved == []
        assert request("POST", "/admin/save", fields, base)[0] == 303
        assert writer.saved == [("groq", "fixture-secret-key")]
        status, _, page = request("GET", "/admin", headers={"Cookie": cookie})
        assert status == 200 and "configured" in page and "personal" in page
        assert "fixture-secret-key" not in page
        assert "fixture-secret-key" not in metadata.path.read_text()
        assert metadata.path.stat().st_mode & 0o777 == 0o600
        writer.fail = True
        assert request("POST", "/admin/save", fields | {"key": "different-key"}, base)[0] == 303
        assert writer.saved == [("groq", "fixture-secret-key")]
        assert "different-key" not in request("GET", "/admin", headers={"Cookie": cookie})[2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_iab_opaque_origin_needs_same_origin_fetch_metadata_and_csrf(tmp_path):
    writer = MemoryWriter()
    server = make_key_admin_server(writer, MetadataStore(tmp_path / "meta.json"), "t" * 48, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    host = f"127.0.0.1:{port}"

    def request(path, fields, headers):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            path,
            urlencode(fields).encode(),
            {"Host": host, "Content-Type": "application/x-www-form-urlencoded"} | headers,
        )
        response = conn.getresponse()
        result = response.status, dict(response.getheaders())
        response.read()
        conn.close()
        return result

    iab = {
        "Origin": "null",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    try:
        login = {"token": "t" * 48}
        assert request("/admin/login", login, {"Origin": "null"})[0] == 403
        assert request("/admin/login", login, iab | {"Sec-Fetch-Site": "cross-site"})[0] == 403
        status, headers = request("/admin/login", login, iab)
        assert status == 303
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/admin", headers={"Host": host, "Cookie": cookie})
        response = conn.getresponse()
        page = response.read().decode()
        assert response.status == 200
        conn.close()
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        fields = {
            "csrf": csrf,
            "provider": "groq",
            "key": "fixture-key",
            "key_name": "",
            "key_id": "",
            "expiry": "",
        }
        assert (
            request(
                "/admin/save", fields, iab | {"Cookie": cookie, "Sec-Fetch-Site": "cross-site"}
            )[0]
            == 403
        )
        assert request("/admin/save", fields | {"csrf": "bad"}, iab | {"Cookie": cookie})[0] == 403
        assert writer.saved == []
        assert request("/admin/save", fields, iab | {"Cookie": cookie})[0] == 303
        assert writer.saved == [("groq", "fixture-key")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
