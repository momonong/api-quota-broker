import http.client
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from quota_broker.core import stamp, utcnow
from quota_broker.nvidia import ExecutionError, NvidiaExecutor
from quota_broker.nvidia_server import make_nvidia_server


def profile():
    now = utcnow()
    return {
        "secret_ref": "NVIDIA_API_KEY",
        "key_id": "fixture-id",
        "key_name": "Fixture",
        "key_expires_kind": "never",
        "key_expires_at": None,
        "scope": "fixture only",
        "verified_at": stamp(now),
        "eligibility_expires_at": stamp(now + timedelta(hours=1)),
        "source": "offline fixture",
        "enabled": True,
        "free_eligible": True,
        "billing_enabled": False,
        "rpm": 2,
        "rpd": 3,
        "input_tpm": 1000,
        "concurrency_limit": 1,
        "max_output_tokens": 32,
    }


def executor(tmp_path, transport=None):
    calls = []

    def fake(secret, prompt, output):
        calls.append((secret, prompt, output))
        if transport:
            return transport(secret, prompt, output)
        return 200, {
            "choices": [{"message": {"content": "fixture answer"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }

    service = NvidiaExecutor(
        tmp_path / "broker.db",
        b"digest-key-" * 4,
        lambda name: "fixture-secret" if name == "NVIDIA_API_KEY" else "",
        fake,
        doppler_project="fixture-project",
        doppler_config="fixture-config",
    )
    service.put_profile(profile())
    return service, calls


def test_success_idempotency_restart_and_no_content_persistence(tmp_path):
    service, calls = executor(tmp_path)
    request = {"request_key": "one", "prompt": "private prompt", "max_output_tokens": 8}
    result = service.execute(request)
    assert result["content"] == "fixture answer"
    assert service.status("one")["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}
    assert service.status("one")["accounted_input_tokens"] == 4
    assert len(calls) == 1
    assert service.execute(request)["state"] == "completed"
    restarted = NvidiaExecutor(
        service.db,
        b"digest-key-" * 4,
        lambda _: "fixture-secret",
        lambda *_: pytest.fail("replayed provider call"),
    )
    assert restarted.execute(request)["state"] == "completed"
    assert restarted.status("one")["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}
    with pytest.raises(ExecutionError, match="different payload"):
        restarted.execute(request | {"prompt": "other"})
    raw = (tmp_path / "broker.db").read_bytes()
    for forbidden in (b"private prompt", b"fixture answer", b"fixture-secret"):
        assert forbidden not in raw
    with sqlite3.connect(service.db) as con:
        assert (
            con.execute("SELECT state FROM reservations WHERE request_key='nvidia:one'").fetchone()[
                0
            ]
            == "completed"
        )


def test_unknown_keeps_capacity_and_no_replay(tmp_path):
    def timeout(*_):
        raise TimeoutError("maybe sent")

    service, calls = executor(tmp_path, timeout)
    with pytest.raises(ExecutionError) as error:
        service.execute({"request_key": "one", "prompt": "secret", "max_output_tokens": 8})
    assert error.value.code == "provider_unknown"
    assert service.status("one")["state"] == "unknown"
    assert service.status("one")["usage"] is None
    assert service.status("one")["accounted_input_tokens"] == 4 * len(b"secret") + 256
    assert len(calls) == 1
    assert (
        service.execute({"request_key": "one", "prompt": "secret", "max_output_tokens": 8})["state"]
        == "unknown"
    )
    assert len(calls) == 1
    with pytest.raises(ExecutionError) as blocked:
        service.execute({"request_key": "two", "prompt": "second", "max_output_tokens": 8})
    assert blocked.value.code == "unavailable"
    assert len(calls) == 1


def test_expiration_unknown_and_secret_failure_fail_closed(tmp_path):
    service, calls = executor(tmp_path)
    bad = profile()
    bad["key_expires_kind"] = "unknown"
    service.put_profile(bad)
    with pytest.raises(ExecutionError) as error:
        service.execute({"request_key": "one", "prompt": "secret", "max_output_tokens": 8})
    assert error.value.code == "unavailable"
    assert not calls
    bad["key_expires_kind"] = "never"
    service.put_profile(bad)
    service.secret_resolver = lambda _: (_ for _ in ()).throw(
        ExecutionError("secret_unavailable", "offline")
    )
    with pytest.raises(ExecutionError) as error:
        service.execute({"request_key": "two", "prompt": "secret", "max_output_tokens": 8})
    assert error.value.code == "secret_unavailable"
    assert not calls


def test_http_roles_origin_csrf_and_profile(tmp_path):
    service, calls = executor(tmp_path)
    server = make_nvidia_server(service, "127.0.0.1", 0, "c" * 40, "a" * 40)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_port}"

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(method, path, body, {"Host": host} | (headers or {}))
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    try:
        payload = json.dumps({"request_key": "http", "prompt": "hello", "max_output_tokens": 8})
        assert (
            request("POST", "/v1/nvidia/text", payload, {"Content-Type": "application/json"})[0]
            == 401
        )
        assert request("GET", "/admin")[0] == 401
        assert (
            request(
                "POST",
                "/admin/login",
                urlencode({"token": "a" * 40}),
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil"},
            )[0]
            == 403
        )
        status, headers, _ = request(
            "POST",
            "/admin/login",
            urlencode({"token": "a" * 40}),
            {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://" + host},
        )
        assert status == 200
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
        status, _, page = request("GET", "/admin", headers={"Cookie": cookie})
        assert status == 200 and "NVIDIA Broker 管理" in page and "secret_ref" in page
        assert "fixture-project/fixture-config" in page
        assert "Google" in page and "Cloudflare" in page and "NVIDIA" in page
        assert "未知（本頁無帳號資料）" in page
        assert "有效（限本地中繼資料）" in page
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        assert (
            request(
                "POST",
                "/admin/test",
                urlencode({"csrf": "bad"}),
                {
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "http://" + host,
                },
            )[0]
            == 403
        )
        assert (
            request(
                "POST",
                "/admin/test",
                urlencode({"csrf": csrf}),
                {
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "http://evil",
                },
            )[0]
            == 403
        )
        assert not calls
        updated = profile()
        updated["key_name"] = "Edited fixture key"
        status, _, page = request(
            "POST",
            "/admin/profile",
            urlencode({"csrf": csrf, "profile": json.dumps(updated)}),
            {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://" + host,
            },
        )
        assert status == 200 and "Edited fixture key" in page
        assert service.profile()["key_name"] == "Edited fixture key"
        status, _, page = request(
            "POST",
            "/admin/test",
            urlencode({"csrf": csrf}),
            {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://" + host,
            },
        )
        assert status == 200 and "連線測試：completed" in page and len(calls) == 1
        status, _, body = request(
            "POST",
            "/v1/nvidia/text",
            payload,
            {"Authorization": "Bearer " + "c" * 40, "Content-Type": "application/json"},
        )
        assert status == 200 and json.loads(body)["content"] == "fixture answer"
        status, _, body = request(
            "GET",
            "/v1/nvidia/requests/http",
            headers={"Authorization": "Bearer " + "c" * 40},
        )
        assert status == 200
        assert json.loads(body)["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}
        status, _, page = request("GET", "/admin", headers={"Cookie": cookie})
        assert status == 200 and "4 / 2" in page and "配額帳本 input" in page
        modern = json.loads(
            (Path(__file__).resolve().parents[1] / "nvidia-profile.example.json").read_text()
        )
        modern["key_expires_kind"] = "never"
        modern["enabled"] = True
        modern["free_eligible"] = True
        modern["verified_at"] = stamp(utcnow())
        modern["eligibility_expires_at"] = stamp(utcnow() + timedelta(hours=1))
        service.put_profile(modern)
        status, _, page = request("GET", "/admin", headers={"Cookie": cookie})
        assert status == 200
        assert "本地安全上限（非官方 quota）" in page
        assert "供應商 remaining 證據" in page
        assert "未知（unknown" in page
        assert service._target(modern).quota_basis == "local_safety_cap"
        assert (
            request(
                "GET", "/v1/nvidia/requests/http", headers={"Authorization": "Bearer " + "a" * 40}
            )[0]
            == 401
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_rate_limit_without_usage_is_unknown_and_cools_down(tmp_path):
    service, calls = executor(tmp_path, lambda *_: (429, {"error": "rate limited"}))
    with pytest.raises(ExecutionError) as error:
        service.execute({"request_key": "limited", "prompt": "hello", "max_output_tokens": 8})
    assert error.value.code == "provider_unknown"
    assert service.status("limited")["state"] == "unknown"
    assert len(calls) == 1
    with sqlite3.connect(service.db) as con:
        assert con.execute(
            "SELECT until_at FROM cooldowns WHERE target_id='nvidia:primary'"
        ).fetchone()


def test_legacy_direct_config_cannot_enable_nvidia(tmp_path):
    from quota_broker.config import ConfigError, load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"targets": [{"provider": "nvidia", "model": "google/gemma-4-31b-it"}]})
    )
    with pytest.raises(ConfigError, match="authenticated executor"):
        load_config(path)


def test_short_prompt_reserves_chat_overhead(tmp_path):
    service, _ = executor(tmp_path, lambda *_: (_ for _ in ()).throw(TimeoutError("sent")))
    with pytest.raises(ExecutionError):
        service.execute({"request_key": "short", "prompt": "a", "max_output_tokens": 8})
    with sqlite3.connect(service.db) as con:
        reserved = con.execute(
            "SELECT amount FROM charges WHERE bucket='nvidia:primary:input_tpm'"
        ).fetchone()[0]
    assert reserved == 4 * len(b"a") + 256


def test_fixed_transport_and_doppler_lookup_without_fallback(tmp_path, monkeypatch):
    import urllib.request

    from quota_broker.nvidia import URL, doppler_resolver, nvidia_transport

    seen = []

    class Response:
        status = 200

        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return json.dumps(self.value).encode()

    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            if request.full_url.startswith("https://api.doppler.com/"):
                return Response({"value": {"raw": "provider fixture key"}})
            return Response(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    token_file = tmp_path / "service-token"
    token_file.write_text("dp.st.dev." + "a" * 40)
    resolver = doppler_resolver(token_file, "approved", "dev")
    assert resolver("NVIDIA_API_KEY") == "provider fixture key"
    assert resolver("NVIDIA_API_KEY") == "provider fixture key"
    assert len(seen) == 2  # No provider-key cache or offline fallback.
    assert all(
        "project=approved&config=dev&name=NVIDIA_API_KEY" in item[0].full_url for item in seen
    )
    with pytest.raises(ExecutionError):
        resolver("../../other")
    assert len(seen) == 2
    status, _ = nvidia_transport("provider fixture key", "hi", 8)
    assert status == 200
    assert seen[-1][0].full_url == URL and seen[-1][0].get_method() == "POST"
    sent = json.loads(seen[-1][0].data)
    assert sent == {
        "model": "google/gemma-4-31b-it",
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 8,
        "stream": False,
    }


def test_profile_status_distinguishes_unknown_expired_and_unverified(tmp_path):
    service, _ = executor(tmp_path)
    assert service.profile_state() == "有效（限本地中繼資料）"
    data = profile()
    data["key_expires_kind"] = "unknown"
    service.put_profile(data)
    assert service.profile_state() == "金鑰到期未知"
    data["key_expires_kind"] = "at"
    data["key_expires_at"] = stamp(utcnow() - timedelta(seconds=1))
    service.put_profile(data)
    assert service.profile_state() == "金鑰已過期"
    data["key_expires_kind"] = "never"
    data["key_expires_at"] = None
    data["verified_at"] = None
    service.put_profile(data)
    assert service.profile_state() == "免費資格待驗證"


def test_concurrent_same_key_sends_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def slow(*_):
        entered.set()
        assert release.wait(3)
        return 200, {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    service, calls = executor(tmp_path, slow)
    request = {"request_key": "concurrent", "prompt": "hello", "max_output_tokens": 8}
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(service.execute(request)))
    worker.start()
    assert entered.wait(3)
    duplicate = service.execute(request)
    assert duplicate["state"] in {"dispatched", "unknown", "completed"}
    assert len(calls) == 1
    release.set()
    worker.join(timeout=3)
    assert outcome[0]["state"] == "completed"
    assert len(calls) == 1


def test_runtime_doppler_adapter_rejects_personal_cli_token():
    from quota_broker.nvidia import doppler_resolver_from_token

    with pytest.raises(ValueError, match="Service Token"):
        doppler_resolver_from_token("dp.ct." + "a" * 40, "api-provider-nvidia", "dev")


def test_nvidia_429_header_metadata_and_cooldown(tmp_path, monkeypatch):
    import urllib.request

    from quota_broker.nvidia import nvidia_transport

    class Response:
        status = 429

        def __init__(self):
            self.headers = {"Retry-After": "120"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return b"not JSON"

    class Opener:
        def open(self, _request, _timeout=None, **_kwargs):
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    status, response = nvidia_transport("fixture key", "hi", 8)
    assert status == 429
    assert response == {"_retry_after_seconds": 120}

    service, calls = executor(tmp_path, lambda *_: (429, {"_retry_after_seconds": 120}))
    before = utcnow()
    with pytest.raises(ExecutionError):
        service.execute({"request_key": "rate-header", "prompt": "hi", "max_output_tokens": 8})
    assert len(calls) == 1
    assert service.status("rate-header")["state"] == "unknown"
    with sqlite3.connect(service.db) as con:
        until = con.execute(
            "SELECT until_at FROM cooldowns WHERE target_id='nvidia:primary'"
        ).fetchone()[0]
    assert 118 <= (datetime.fromisoformat(until) - before).total_seconds() <= 123
