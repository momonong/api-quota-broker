"""Fictional verifier files and real loopback requests; no private host state."""

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from uuid import uuid4

import pytest
from test_dashboard_server import public_request, request

from quota_broker.dashboard import Dashboard
from quota_broker.dashboard_demo import create_demo

PASSWORD = "  合成網站密碼 phrase  "


def document(password=PASSWORD):
    salt = "FixtureSalt12345"
    digest = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=32768, r=8, p=1, dklen=64, maxmem=67108864
    ).hex()
    return {
        "schema_version": 1,
        "kind": "scrypt",
        "verifier": f"scrypt:32768:8:1${salt}${digest}",
        "revision": str(uuid4()),
    }


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o640)


def reader(path):
    from quota_broker.dashboard_auth import BrowserPasswordFile

    return BrowserPasswordFile(path, fixture_owner=(os.getuid(), os.getgid()))


@contextmanager
def running(tmp_path, public=False):
    from quota_broker.dashboard_server import make_dashboard_server

    path = tmp_path / "browser-auth.json"
    write(path, document())
    board = Dashboard(create_demo(tmp_path))
    server = make_dashboard_server(
        board,
        password_file=reader(path),
        port=0,
        public_base_url="https://momonong.me/quota/" if public else None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, path, board
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def login(server, password=PASSWORD):
    return request(server, "/api/login", method="POST", payload={"token": password})


def test_unicode_spaces_wrong_password_and_logout(tmp_path):
    with running(tmp_path) as (server, _, _):
        assert login(server, PASSWORD.strip())[0] == 401
        status, headers, _ = login(server)
        assert status == 200
        cookie = headers["Set-Cookie"].split(";")[0]
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 200
        assert request(server, "/api/logout", method="POST", payload={}, cookie=cookie)[0] == 200
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401


def test_password_mode_public_prefix_and_cookie(tmp_path):
    with running(tmp_path, public=True) as (server, _, _):
        assert public_request(server, "/quota/api/snapshot")[0] == 401
        status, headers, _ = public_request(
            server, "/quota/api/login", method="POST", payload={"token": PASSWORD}
        )
        assert status == 200
        cookie = headers["Set-Cookie"]
        assert (
            cookie.startswith("__Secure-aqb_quota=")
            and "Path=/quota/" in cookie
            and "; Secure;" in cookie
        )
        assert public_request(server, "/quota/api/snapshot", cookie=cookie.split(";")[0])[0] == 200
        assert public_request(server, "/api/snapshot", cookie=cookie.split(";")[0])[0] == 404


@pytest.mark.parametrize("value", [None, 123, "", "x" * 1025, "a\nb", "a\rb", "a\0b", "\ud800"])
def test_invalid_password_never_reaches_kdf(tmp_path, monkeypatch, value):
    with running(tmp_path) as (server, _, _):
        monkeypatch.setattr(
            hashlib, "scrypt", lambda *a, **k: pytest.fail("invalid input reached KDF")
        )
        assert login(server, value)[0] == 401


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "symlink",
        "hardlink",
        "writable",
        "public",
        "parent",
        "invalid",
        "duplicate",
        "cost",
        "uuid",
        "boolean",
        "fifo",
        "oversize",
        "deep",
    ],
)
def test_file_damage_revokes_existing_session_and_denies_login(tmp_path, damage):
    with running(tmp_path) as (server, path, _):
        cookie = login(server)[1]["Set-Cookie"].split(";")[0]
        value = json.loads(path.read_text())
        if damage == "missing":
            path.unlink()
        elif damage == "fifo":
            path.unlink()
            os.mkfifo(path, 0o640)
        elif damage == "oversize":
            path.write_bytes(b" " * 8193)
        elif damage == "deep":
            path.write_bytes(b"[" * 4000 + b"0" + b"]" * 4000)
        elif damage == "symlink":
            target = path.with_suffix(".target")
            path.rename(target)
            path.symlink_to(target)
        elif damage == "hardlink":
            os.link(path, path.with_suffix(".link"))
        elif damage in {"writable", "public"}:
            path.chmod(0o660 if damage == "writable" else 0o644)
        elif damage == "parent":
            path.parent.chmod(0o777)
        elif damage == "invalid":
            path.write_text("{invalid")
        elif damage == "duplicate":
            path.write_text(json.dumps(value)[:-1] + ',"kind":"scrypt"}')
        else:
            if damage == "cost":
                value["verifier"] = value["verifier"].replace("32768", "131072")
            elif damage == "uuid":
                value["revision"] = value["revision"].upper()
            else:
                value["schema_version"] = True
            write(path, value)
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401
        assert login(server)[0] == 401


def test_bad_file_revokes_session_even_if_original_bytes_return(tmp_path):
    with running(tmp_path) as (server, path, _):
        cookie = login(server)[1]["Set-Cookie"].split(";")[0]
        original = path.read_bytes()
        path.unlink()
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401
        path.write_bytes(original)
        path.chmod(0o640)
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401


def test_wrong_owner_group_and_symlink_parent_are_rejected(tmp_path):
    from quota_broker.dashboard_auth import BrowserAuthError, BrowserPasswordFile

    path = tmp_path / "browser-auth.json"
    write(path, document())
    for owner in ((os.getuid() + 1, os.getgid()), (os.getuid(), os.getgid() + 1)):
        with pytest.raises(BrowserAuthError):
            BrowserPasswordFile(path, fixture_owner=owner).read()
    link = tmp_path / "linked-parent"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BrowserAuthError):
        reader(link / path.name).read()


def test_revision_change_revokes_get_and_post_then_accepts_new_password(tmp_path):
    with running(tmp_path) as (server, path, _):
        cookie = login(server)[1]["Set-Cookie"].split(";")[0]
        write(path, document("new fictional phrase"))
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401
        assert request(server, "/api/advice", method="POST", payload={}, cookie=cookie)[0] == 401
        assert login(server)[0] == 401
        assert login(server, "new fictional phrase")[0] == 200


def test_rotation_during_kdf_cannot_issue_session_and_kdf_is_serialized(tmp_path, monkeypatch):
    with running(tmp_path) as (server, path, _):
        replacement = document("new fictional phrase")
        entered, release = threading.Event(), threading.Event()
        actual = hashlib.scrypt

        def paused(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return actual(*args, **kwargs)

        monkeypatch.setattr(hashlib, "scrypt", paused)
        result = []
        worker = threading.Thread(target=lambda: result.append(login(server)))
        worker.start()
        try:
            assert entered.wait(2)
            assert login(server)[0] == 429
            write(path, replacement)
        finally:
            release.set()
            worker.join(3)
        assert result[0][0] == 401
        assert "Set-Cookie" not in result[0][1]


def test_rotation_while_snapshot_runs_does_not_return_protected_data(tmp_path):
    with running(tmp_path) as (server, path, board):
        cookie = login(server)[1]["Set-Cookie"].split(";")[0]
        actual = board.snapshot

        def rotated():
            write(path, document())
            return actual()

        board.snapshot = rotated
        assert request(server, "/api/snapshot", cookie=cookie)[0] == 401


def test_login_rate_limit_avoids_sixth_kdf(tmp_path, monkeypatch):
    with running(tmp_path) as (server, _, _):
        for _ in range(5):
            assert login(server, "wrong fictional phrase")[0] == 401
        monkeypatch.setattr(hashlib, "scrypt", lambda *a, **k: pytest.fail("throttled KDF"))
        assert login(server)[0] == 429


def test_cli_browser_modes_are_mutually_exclusive(monkeypatch):
    from quota_broker.cli import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "dashboard-serve",
            "--demo",
            "--dashboard-token-file",
            "/unused",
            "--dashboard-password-file",
            "/unused",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_password_file_mode_reaches_dashboard_without_token_lookup(tmp_path, monkeypatch):
    from quota_broker.cli import main

    captured = {}

    class Server:
        server_port = 0

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def make_server(board, token, **kwargs):
        captured.update(token=token, **kwargs)
        return Server()

    path = str(tmp_path / "canonical.json")
    monkeypatch.setattr("quota_broker.dashboard_server.make_dashboard_server", make_server)
    monkeypatch.setattr(
        "quota_broker.dashboard_server.read_private_token",
        lambda _: pytest.fail("password mode read a token"),
    )
    monkeypatch.setattr(
        "sys.argv", ["quota-broker", "dashboard-serve", "--demo", "--dashboard-password-file", path]
    )
    main()
    assert captured["password_file"] == path and captured["token"] is None
