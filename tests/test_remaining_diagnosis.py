import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bounded_curl as curl
import v1_remaining_once as remaining

from quota_broker.gateway_providers import (
    ProviderError,
    official_request,
    safe_response_diagnostics,
)


def riva_request():
    _, headers, payload = official_request(
        "nvidia",
        "nvidia/riva-translate-4b-instruct-v2",
        "fixture",
        "fixture-secret",
        "Good morning.",
        32,
        "en",
        "zh-cn",
    )
    return headers, payload


def curl_output(status=200, code=0, *, header=b"HTTP/2 200\r\n", body=b"{}", tls=0.2):
    metrics = {
        "http_code": str(status).zfill(3),
        "exitcode": code,
        "time_namelookup": 0.1,
        "time_connect": 0.15,
        "time_appconnect": tls,
        "time_starttransfer": 0.3 if status else 0,
        "time_total": 0.4,
    }
    return (
        header
        + b"content-type: application/json\r\n\r\n"
        + body
        + curl.MARKER
        + json.dumps(metrics).encode()
    )


def test_real_curl_config_parses_without_any_network(monkeypatch):
    configs = []

    def fixture(config, timeout):
        configs.append(config)
        # --version suppresses all transfers while curl still parses its stdin config.
        parsed = subprocess.run(
            ["/usr/bin/curl", "--disable", "--silent", "--config", "-", "--version"],
            input=config,
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert parsed.returncode == 0
        assert "fixture-secret" not in parsed.stdout.decode()
        # Exercise actual write-out escaping against an empty local file only.
        local_config = config.replace(curl.URL.encode(), b"file:///dev/null").replace(
            b"=https", b"=file"
        )
        parsed = subprocess.run(
            ["/usr/bin/curl", "--disable", "--silent", "--config", "-"],
            input=local_config,
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert parsed.returncode == 0
        _, separator, raw_metrics = parsed.stdout.rpartition(curl.MARKER)
        assert separator and json.loads(raw_metrics)["http_code"] == "000"
        return 0, curl_output(body=b'{"choices":[]}')

    monkeypatch.setattr(curl, "run_curl", fixture)
    status, headers, body, details = curl.nvidia_http(*riva_request())
    assert status == 200 and headers["content-type"] == "application/json"
    assert body == b'{"choices":[]}' and details["time_appconnect_ms"] == 200
    assert b'url = "https://integrate.api.nvidia.com/v1/chat/completions"' in configs[0]
    assert b'retry = "0"' in configs[0]
    assert b"user-agent" not in configs[0] and b"insecure" not in configs[0]
    assert b'write-out = "\\\\nquota-broker-curl-metrics:' in configs[0]


@pytest.mark.parametrize("first", [b"HTTP/1.1 100 Continue\r\n\r\n", b""])
def test_informational_headers_and_marker_inside_body(monkeypatch, first):
    body = b'{"content":"\\nquota-broker-curl-metrics: fake"}'
    monkeypatch.setattr(curl, "run_curl", lambda *_: (0, first + curl_output(body=body)))
    assert curl.nvidia_http(*riva_request())[2] == body


@pytest.mark.parametrize(
    "status,tls,phase",
    [
        (0, 0.2, "after_tls_before_headers"),
        (0, 0, "tls_or_proxy_tunnel"),
        (200, 0.2, "response_body"),
    ],
)
def test_timeout_never_returns_partial_answer(monkeypatch, status, tls, phase):
    output = curl_output(status, 28, body=b"partial secret-like answer", tls=tls)
    if not status:
        output = output[output.index(curl.MARKER) :]
    monkeypatch.setattr(curl, "run_curl", lambda *_: (28, output))
    _, _, body, diagnostics = curl.nvidia_http(*riva_request())
    assert body == b"" and diagnostics["timeout_phase"] == phase


def test_invalid_headers_control_values_and_overlarge_body_are_rejected(monkeypatch):
    headers, payload = riva_request()
    with pytest.raises(ProviderError):
        curl.nvidia_http({"Authorization": "Bearer secret\ntrace = file"}, payload)
    monkeypatch.setattr(curl, "run_curl", lambda *_: (0, curl_output(header=b"HTTP/2 500\r\n")))
    with pytest.raises(ProviderError, match="status_mismatch"):
        curl.nvidia_http(headers, payload)
    monkeypatch.setattr(curl, "run_curl", lambda *_: (0, curl_output(body=b"x" * 65_537)))
    with pytest.raises(ProviderError, match="body_bound"):
        curl.nvidia_http(headers, payload)


@pytest.mark.parametrize("failure", ["output_bound", "curl_process_deadline"])
def test_output_limit_or_deadline_kills_child_and_discards_bytes(monkeypatch, failure):
    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        killed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, **_):
            return 0

    class Selector:
        active = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def register(self, *_):
            pass

        def get_map(self):
            return {1: True} if self.active else {}

        def select(self, _):
            from types import SimpleNamespace

            return [(SimpleNamespace(fd=1), 1)]

    child = Child()
    monkeypatch.setattr(curl.subprocess, "Popen", lambda *_, **__: child)
    monkeypatch.setattr(curl.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(curl.os, "read", lambda *_: b"secret-like bytes" * 6000)
    if failure == "curl_process_deadline":
        instants = iter((0, 100))
        monkeypatch.setattr(curl.time, "monotonic", lambda: next(instants))
    with pytest.raises(ProviderError, match=failure):
        curl.run_curl(b"fixture config", 1)
    assert child.killed


def test_safe_mistral_fields_retain_known_type_without_untrusted_values():
    body = {
        "object": "error",
        "type": "invalid_request_error",
        "code": "fixture-secret",
        "message": "fixture-secret",
        "param": "model",
    }
    details = safe_response_diagnostics(
        "mistral",
        429,
        {
            "Content-Type": "application/json; charset=utf8",
            "Retry-After": "10",
            "Set-Cookie": "fixture-secret",
        },
        json.dumps(body).encode(),
    )
    assert details["error_type"] == "invalid_request_error"
    assert details["retry_after_seconds"] == 10 and details["error_param_is_model"] is True
    assert "fixture-secret" not in json.dumps(details)
    body["type"] = "fixture-secret"
    assert (
        safe_response_diagnostics("mistral", 429, {}, json.dumps(body).encode())["error_type"]
        == "unclassified"
    )


def test_plan_is_offline(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_remaining_once.py"])
    monkeypatch.setattr(remaining, "service_token", lambda _: 1 / 0)
    assert remaining.main() == 0
    assert "maximum 2 independent POSTs" in capsys.readouterr().out


@pytest.mark.parametrize("chat_available", [True, False])
def test_fixture_live_is_bounded_and_keeps_failure_diagnostics(
    tmp_path, monkeypatch, capsys, chat_available
):
    db = tmp_path / "new.sqlite"
    prior = []
    for number, providers in enumerate((("nvidia",), ("mistral",), ("nvidia", "mistral"))):
        path = tmp_path / f"prior{number}.sqlite"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE gateway_attempts(provider TEXT, dispatched_at TEXT)")
            con.executemany(
                "INSERT INTO gateway_attempts VALUES(?, 'fixture')", [(p,) for p in providers]
            )
        prior.append(path)
    old_bytes = [path.read_bytes() for path in prior]
    monkeypatch.setattr(remaining, "DB", db)
    monkeypatch.setattr(remaining, "ALL_PRIOR", tuple(prior))
    monkeypatch.setattr(sys, "argv", ["v1_remaining_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(remaining, "cli", lambda: "fixture")
    monkeypatch.setattr(
        remaining, "metadata_names", lambda _: {"NVIDIA_API_KEY", "MISTRAL_API_KEY"}
    )
    token_calls = []
    monkeypatch.setattr(
        remaining, "service_token", lambda _: token_calls.append(1) or "fixture-token"
    )
    reads = []

    def resolver(name):
        reads.append(name)
        return "fixture-secret"

    monkeypatch.setattr(remaining, "doppler_resolver_from_token", lambda *_: resolver)
    gets, posts = [], []

    class Response:
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "mistral-small-fixture-version",
                            "aliases": ["mistral-small-latest"],
                            "capabilities": {"completion_chat": chat_available},
                        }
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == remaining.MODEL_URL and timeout == 15
            gets.append(1)
            return Response()

    monkeypatch.setattr(remaining.urllib.request, "build_opener", lambda *_: Opener())

    def mistral(url, headers, payload, timeout):
        assert timeout == 30 and payload["max_tokens"] == 32
        posts.append("mistral")
        return (
            429,
            {},
            json.dumps(
                {
                    "object": "error",
                    "type": "invalid_request_error",
                    "message": "fixture-secret",
                    "code": "fixture-secret",
                }
            ).encode(),
        )

    monkeypatch.setattr(remaining, "provider_http", mistral)

    def nvidia(headers, payload, timeout):
        assert timeout == 60 and payload == riva_request()[1]
        posts.append("nvidia")
        return (
            200,
            {},
            json.dumps(
                {
                    "choices": [{"message": {"content": "fixture-answer"}}],
                    "usage": {"prompt_tokens": 22, "completion_tokens": 3},
                }
            ).encode(),
            {"transport_code": "ok"},
        )

    monkeypatch.setattr(remaining, "nvidia_http", nvidia)
    assert remaining.main() == 2  # unknown Mistral or failed gate remains incomplete
    assert len(gets) == 1 and posts == (["mistral", "nvidia"] if chat_available else ["nvidia"])
    assert reads == ["MISTRAL_API_KEY", "NVIDIA_API_KEY"] and token_calls == [1]
    assert db.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(db) as con:
        assert (
            con.execute("SELECT state FROM gateway_tasks WHERE provider='nvidia'").fetchone()[0]
            == "completed"
        )
        if chat_available:
            assert (
                con.execute("SELECT state FROM gateway_tasks WHERE provider='mistral'").fetchone()[
                    0
                ]
                == "unknown"
            )
            details = json.loads(
                con.execute(
                    "SELECT details_json FROM gateway_diagnostics WHERE provider='mistral' AND request_key != 'mistral-model-get'"
                ).fetchone()[0]
            )
            assert details["error_type"] == "invalid_request_error"
    assert [path.read_bytes() for path in prior] == old_bytes
    assert b"fixture-secret" not in db.read_bytes() and b"fixture-answer" not in db.read_bytes()
    output = capsys.readouterr().out
    assert (
        "fixture-secret" not in output
        and "fixture-answer" not in output
        and "fixture-token" not in output
    )
    with pytest.raises(RuntimeError, match="never replay"):
        remaining.main()
    assert len(gets) == 1 and token_calls == [1]
