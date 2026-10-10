"""Offline dependency-injected fixtures; no real credential or GET execution."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "provider_metadata_fetch", SCRIPTS / "provider_metadata_fetch.py"
)
assert SPEC is not None and SPEC.loader is not None
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)
NOW = datetime(2026, 10, 1, tzinfo=UTC)
ACCOUNT = "a" * 32


class BatchFixture:
    def __init__(self, *, google_pages=2, cf_pages=2, duration=0):
        self.google_pages = google_pages
        self.cf_pages = cf_pages
        self.duration = duration
        self.elapsed = 0.0
        self.creations = 0
        self.resolutions = []
        self.calls = []
        self.google_page = 0

    def credentials(self):
        self.creations += 1
        return self.resolve, 0.0

    def resolve(self, name):
        self.resolutions.append(name)
        return ACCOUNT if name == "CLOUDFLARE_ACCOUNT_ID" else "fixture-value-123"

    def get(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        self.elapsed += self.duration
        if "googleapis.com" in url:
            self.google_page += 1
            page = self.google_page
            raw = {
                "models": [
                    {
                        "name": f"models/google-{page}",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            }
            if page < self.google_pages:
                raw["nextPageToken"] = f"opaque/{page}+token"
        elif "cloudflare.com" in url:
            page = int(url.split("?page=")[1].split("&")[0])
            raw = {
                "success": True,
                "result": [
                    {"name": f"@cf/fixture/model-{page}", "task": {"name": "Text Generation"}}
                ],
                "result_info": {
                    "page": page,
                    "total_pages": self.cf_pages,
                    "total_count": self.cf_pages,
                },
            }
        elif url == fetch.plan.KEY_URL:
            raw = {
                "data": {
                    "label": "discard-private-label",
                    "limit": 10,
                    "limit_remaining": 8,
                    "usage": 2,
                    "free_model_daily_requests": {"used": 1, "limit": 50, "remaining": 49},
                }
            }
        elif "openrouter.ai" in url:
            raw = {
                "data": [
                    {
                        "id": "fixture/openrouter",
                        "architecture": {
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                        },
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }
        else:
            raw = {"data": [{"id": "fixture-model"}]}
        return json.dumps(raw).encode()

    def run(self, **kwargs):
        return fetch.run_batch(
            credentials=self.credentials,
            get=kwargs.pop("get", self.get),
            monotonic=lambda: self.elapsed,
            clock=lambda: NOW,
            **kwargs,
        )


def test_default_dry_run_has_no_credential_process_file_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("execution attempted")

    monkeypatch.setattr(fetch, "credential_factory", forbidden)
    monkeypatch.setattr(fetch, "metadata_get", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert fetch.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == fetch.plan.build_plan()
    assert result["execution_authorized"] is False


def test_fixed_batch_full_exhaustion_and_normalized_outputs():
    fixture = BatchFixture()
    result = fixture.run()
    assert result["state"] == "completed" and result["request_count"] == 9
    assert fixture.creations == 1 and len(set(fixture.resolutions)) == len(fixture.resolutions) == 6
    assert (
        "NVIDIA_API_KEY" not in fixture.resolutions
        and "OCRSPACE_API_KEY" not in fixture.resolutions
    )
    assert all(call[2] == 20 for call in fixture.calls)
    snapshots = {s["provider"]: s for s in result["snapshots"]}
    assert snapshots["google"]["complete"] and len(snapshots["google"]["models"]) == 2
    assert snapshots["cloudflare"]["complete"] and len(snapshots["cloudflare"]["models"]) == 2
    assert len(snapshots) == 7
    assert "pageToken=opaque%2F1%2Btoken" in fixture.calls[4][0]
    assert fixture.calls[3][1] == {"x-goog-api-key": "fixture-value-123"}
    assert fixture.calls[0][1] == {} and fixture.calls[-2][1] == {}
    public = json.dumps(result)
    for private in (ACCOUNT, "fixture-value-123", "opaque", "discard-private-label"):
        assert private not in public
    account = result["account_metadata"][0]
    assert account["free_model_daily_requests"]["remaining"] == 49
    assert account["credits"]["limit_remaining"] == "8"
    assert account["account_availability"] == "unknown" and account["live_result"] == "unknown"


@pytest.mark.parametrize("pages,complete", [(5, True), (6, False)])
def test_five_page_cap_is_not_exhaustion(pages, complete):
    fixture = BatchFixture(google_pages=pages, cf_pages=pages)
    result = fixture.run()
    assert result["state"] == "completed" and result["request_count"] == 15
    snapshots = {s["provider"]: s for s in result["snapshots"]}
    for provider in ("google", "cloudflare"):
        assert snapshots[provider]["complete"] is complete
        assert len(snapshots[provider]["models"]) == 5


def test_insufficient_token_lifetime_prevents_all_gets():
    fixture = BatchFixture()
    fixture.elapsed = 30.001
    result = fixture.run()
    assert result["state"] == "stopped" and result["reason"] == "expiry"
    assert result["request_count"] == 0 and fixture.calls == []
    assert fixture.creations == 1


def test_wall_budget_stops_serial_batch_without_renewal_or_retry():
    fixture = BatchFixture(google_pages=6, cf_pages=6, duration=20)
    result = fixture.run()
    assert result["state"] == "stopped" and result["reason"] == "deadline"
    assert fixture.elapsed == 240 and result["request_count"] == 12
    assert fixture.creations == 1 and all(call[2] == 20 for call in fixture.calls)


def test_remaining_budget_is_used_for_last_request():
    fixture = BatchFixture(duration=19)
    fixture.elapsed = 25
    result = fixture.run()
    assert result["state"] == "completed"
    assert fixture.elapsed < 270
    fixture = BatchFixture(google_pages=6, cf_pages=6, duration=19)
    fixture.elapsed = 25
    result = fixture.run()
    assert result["reason"] == "deadline"
    assert fixture.calls[-1][2] == 12
    assert fixture.elapsed > 265  # Fixture deliberately violates final timeout.


def test_secret_or_transport_error_never_reflects_raw_message_or_retries():
    fixture = BatchFixture()

    def failed(*args):
        raise OSError("secret-key private-account-id untrusted-provider-error")

    result = fixture.run(get=failed)
    assert result["state"] == "stopped" and result["phase"] == "transport"
    assert result["request_count"] == 1
    assert "secret-key" not in json.dumps(result)
    assert fixture.creations == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"key":"fixture"}',
        b'{"data":[],"data":[]}',
        b'{"value":NaN}',
        b'{"note":"Bearer fixture"}',
        b"[1]",
        b"{" * 1500,
        b"x" * (5 * 1024 * 1024 + 1),
    ],
)
def test_raw_reply_safety_and_bounds(raw):
    with pytest.raises(fetch.FetchError):
        fetch.parse_reply(raw)


@pytest.mark.parametrize("kind", ["token", "cf_count", "cf_schema", "duplicate"])
def test_untrusted_pagination_stops_without_partial_complete(kind):
    fixture = BatchFixture()
    underlying = fixture.get

    def changed(url, headers, timeout):
        raw = json.loads(underlying(url, headers, timeout))
        if "googleapis.com" in url and kind == "token":
            raw["nextPageToken"] = "same-token"
        if "googleapis.com" in url and kind == "duplicate":
            raw["models"][0]["name"] = "models/repeated-model"
        if "cloudflare.com" in url and kind == "cf_count":
            raw["result_info"]["total_count"] = 99
        if "cloudflare.com" in url and kind == "cf_schema":
            raw.pop("result_info")
        return json.dumps(raw).encode()

    result = fixture.run(get=changed)
    assert result["state"] == "stopped" and result["phase"] == "normalize"
    assert result["reason"] in {"pagination", "duplicate"}
    assert not any(
        s["provider"] == ("google" if kind in {"token", "duplicate"} else "cloudflare")
        for s in result["snapshots"]
    )


def clear_proxies(monkeypatch):
    for name in tuple(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            monkeypatch.delenv(name)


def test_transport_exact_bounds_default_identity_and_no_redirect_retry(monkeypatch):
    clear_proxies(monkeypatch)
    calls = []

    def curl(config, timeout, **kwargs):
        calls.append((config.decode(), timeout, kwargs))
        timings = {name: 0 for name in fetch.parse_curl_result.__globals__["TIMINGS"]}
        metrics = {**timings, "http_code": "200", "exitcode": 0}
        return 0, b'HTTP/2 200\r\n\r\n{"data":[]}' + fetch.parse_curl_result.__globals__[
            "MARKER"
        ] + json.dumps(metrics).encode()

    monkeypatch.setattr(fetch, "run_curl", curl)
    assert fetch.metadata_get(fetch.plan.SOURCES["nvidia"], {}, 20) == b'{"data":[]}'
    config, timeout, kwargs = calls[0]
    assert timeout == 20 and kwargs == {
        "output_bound": 5 * 1024 * 1024 + 16_384,
        "deadline_grace": 0,
    }
    assert 'retry = "0"' in config and 'max-redirs = "0"' in config
    for forbidden in ("location", "user-agent", "proxy =", "resolve =", "insecure", "POST"):
        assert forbidden not in config
    for url in (
        "https://example.test/models",
        fetch.plan.KEY_URL + "?key=fixture",
        "http://integrate.api.nvidia.com/v1/models",
    ):
        with pytest.raises(fetch.FetchError):
            fetch.metadata_get(url, {}, 20)
    assert len(calls) == 1
    monkeypatch.setenv("HTTPS_PROXY", "https://private-proxy.invalid")
    with pytest.raises(fetch.FetchError, match="metadata batch stopped"):
        fetch.metadata_get(fetch.plan.SOURCES["nvidia"], {}, 20)
    assert len(calls) == 1


def test_credential_factory_uses_existing_single_readonly_creation_and_memory_resolver(
    monkeypatch, capsys
):
    import verify_doppler_executor_read as executor

    from quota_broker import nvidia

    clear_proxies(monkeypatch)
    calls = []
    marker = "fixture-service-token-memory-only"
    monkeypatch.setattr(executor, "cli", lambda: "/fixture/doppler")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout=marker.encode())
        ),
    )
    resolved = []
    monkeypatch.setattr(
        nvidia,
        "doppler_resolver_from_token",
        lambda token, project, config: (
            resolved.append((token, project, config)) or (lambda name: "fixture")
        ),
    )
    resolver, issued_before = fetch.credential_factory()
    assert callable(resolver) and issued_before <= fetch.time.monotonic()
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == "/fixture/doppler"
    assert command[command.index("--access") + 1] == "read"
    assert command[command.index("--max-age") + 1] == "5m"
    assert command[command.index("--attempts") + 1] == "1"
    assert "--no-read-env" in command and "--plain" in command
    assert kwargs == {"capture_output": True, "timeout": 15, "check": False}
    assert resolved == [(marker, "api-quota-broker", "dev")]
    assert capsys.readouterr().out == ""


def test_stdin_normalization_is_offline_safe_and_execute_requires_explicit_flag(
    monkeypatch, capsys
):
    def forbidden():
        raise AssertionError("auth attempted")

    monkeypatch.setattr(fetch, "credential_factory", forbidden)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b'{"data":[{"id":"fixture-model","description":"private ignored description"}]}'
            )
        ),
    )
    assert fetch.main(["--normalize-stdin", "nvidia"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["models"][0]["model"] == "fixture-model"
    assert "description" not in json.dumps(result)
    with pytest.raises(SystemExit):
        fetch.main(["--url", "https://secret.invalid"])
    assert "secret.invalid" not in capsys.readouterr().out


def test_safe_model_like_account_identifier_reflection_stops_without_output():
    fixture = BatchFixture()
    result = fixture.run(
        get=lambda *args: json.dumps({"data": [{"id": "prefix-" + ACCOUNT}]}).encode()
    )
    assert result["state"] == "stopped"
    assert (result["phase"], result["reason"]) == ("normalize", "reflection")
    assert result["request_count"] == 1
    assert result["snapshots"] == result["account_metadata"] == result["summaries"] == []
    assert ACCOUNT not in json.dumps(result)


def test_json_quoted_key_reflection_is_checked_semantically_after_decoding():
    fixture = BatchFixture()
    secret = "fixture-value-123"
    # No provider-controlled literal secret is present in the JSON bytes. The
    # decoded, otherwise valid model identity contains the cached key instead.
    escaped = "".join("\\u" + format(ord(char), "04x") for char in secret)
    raw = ('{"data":[{"id":"safe-prefix-' + escaped + '"}]}').encode()
    assert secret.encode() not in raw
    result = fixture.run(get=lambda *args: raw)
    assert result["reason"] == "reflection"
    assert secret not in json.dumps(result)
    assert result["snapshots"] == []


def test_normalized_key_metadata_reflection_is_checked_before_append():
    fixture = BatchFixture()
    fixture.resolve = lambda name: ACCOUNT if name == "CLOUDFLARE_ACCOUNT_ID" else "12345678"
    underlying = fixture.get

    def changed(url, headers, timeout):
        if url == fetch.plan.KEY_URL:
            return b'{"data":{"limit":"12345678"}}'
        return underlying(url, headers, timeout)

    result = fixture.run(get=changed)
    assert result["reason"] == "reflection"
    assert result["request_count"] == 9
    assert result["account_metadata"] == []
    assert "12345678" not in json.dumps(result)


def test_parser_model_id_diagnostic_preserved_for_openrouter_19_row_fixture():
    fixture = BatchFixture()
    underlying = fixture.get

    def changed(url, headers, timeout):
        if "openrouter.ai/api/v1/models" in url:
            rows = [{"id": f"fixture/model-{index}"} for index in range(19)]
            rows[18]["id"] = "https://private-provider.invalid/model"
            return json.dumps({"data": rows}).encode()
        return underlying(url, headers, timeout)

    result = fixture.run(get=changed)
    assert (result["phase"], result["reason"]) == ("parse_models", "model_id_url")
    assert result["state"] == "stopped" and result["request_count"] == 8
    assert not any(snapshot["provider"] == "openrouter" for snapshot in result["snapshots"])
    assert "private-provider.invalid" not in json.dumps(result)


def test_unallowlisted_error_diagnostics_never_reflect(monkeypatch):
    fixture = BatchFixture()
    error = fetch.DiscoveryError("invalid_request", "untrusted secret message")
    error.phase = "untrusted-secret-phase"
    error.reason = "untrusted-secret-reason"

    def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(fetch, "parse_models", failed)
    result = fixture.run()
    assert (result["phase"], result["reason"]) == ("normalize", "schema")
    assert "untrusted" not in json.dumps(result)


def test_stdin_parser_diagnostic_preserved_without_raw_message(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(b'{"data":[{"id":"https://private-provider.invalid/model"}]}')
        ),
    )
    assert fetch.main(["--normalize-stdin", "openrouter"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "error": "invalid_request",
        "phase": "parse_models",
        "reason": "model_id_url",
    }


@pytest.mark.parametrize(
    "phase,reason",
    [
        ("parse_models", "model_id_" + reason)
        for reason in (
            "type",
            "empty",
            "length",
            "characters",
            "url",
            "secret_pattern",
            "provider_format",
        )
    ]
    + [
        ("validate_evidence", "model_id_" + reason)
        for reason in ("type", "empty", "length", "characters", "url", "secret_pattern")
    ],
)
def test_new_fixed_model_id_diagnostics_survive_injected_fixture(monkeypatch, phase, reason):
    from quota_broker.discovery_parsers import ParserError
    from quota_broker.discovery_sources import EvidenceError

    private = "untrusted provider-controlled freeform secret text"
    if phase == "parse_models":
        error = ParserError(reason, private)
        # Even a provider-controlled exception message must never be reflected.
        error.args = (private,)
        boundary = "parse_models"
    else:
        error = EvidenceError(private, reason)
        boundary = "validate_snapshot"

    def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(fetch, boundary, failed)
    result = BatchFixture().run()
    assert result["state"] == "stopped"
    assert (result["phase"], result["reason"]) == (phase, reason)
    assert result["request_count"] == 1
    assert result["snapshots"] == []
    assert private not in json.dumps(result)
