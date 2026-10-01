import base64
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from quota_broker.gateway import Gateway as RealGateway
from quota_broker.gateway import validate_task

script_path = Path(__file__).resolve().parents[1] / "scripts" / "v1_smoke_once.py"
spec = importlib.util.spec_from_file_location("v1_smoke_once", script_path)
assert spec is not None and spec.loader is not None
v1_smoke_once = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v1_smoke_once)


def test_plan_lists_seven_fixed_routes_without_creating_token(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["v1_smoke_once.py"])
    monkeypatch.setattr(v1_smoke_once, "service_token", lambda _: 1 / 0)
    assert v1_smoke_once.main() == 0
    output = capsys.readouterr().out
    assert output.count("https://") == 7
    assert "plan_only: no token" in output
    assert "GEMINI_API_KEY" in output
    targets = v1_smoke_once.runtime_targets(
        Path(__file__).resolve().parents[1] / "gateway.example.json"
    )
    cloudflare = next(target for target in targets if target.provider == "cloudflare")
    assert cloudflare.capacity.kind == "short_renewable"
    assert cloudflare.capacity.refresh_seconds == 86_400
    assert all(
        target.capacity.kind == "unknown" for target in targets if target.provider != "cloudflare"
    )


def test_openrouter_extra_zero_price_is_allowed_but_any_charge_blocks():
    item = {
        "pricing": {"prompt": "0", "completion": "0", "request": "0"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    assert v1_smoke_once.zero_priced_text_model(item)
    assert not v1_smoke_once.zero_priced_text_model(
        {**item, "pricing": {**item["pricing"], "request": "0.001"}}
    )
    assert not v1_smoke_once.zero_priced_text_model({**item, "pricing": {"prompt": "0"}})


def test_synthetic_ocr_png_is_accepted_and_has_no_user_content():
    encoded = v1_smoke_once.synthetic_ocr_png()
    image = base64.b64decode(encoded)
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) < 36_000
    assert (
        validate_task(
            {
                "request_key": "fixture-ocr",
                "capability": "ocr",
                "input": encoded,
                "max_output_tokens": 1,
                "provider": "ocrspace",
            }
        )["capability"]
        == "ocr"
    )


def test_prior_receipts_block_repeat_dispatch_but_allow_pre_send_retry(tmp_path):
    prior = tmp_path / "v1-smoke-prior.db"
    with sqlite3.connect(prior) as con:
        con.execute("CREATE TABLE gateway_tasks(provider TEXT, dispatched_at TEXT)")
        con.execute("CREATE TABLE gateway_attempts(provider TEXT, dispatched_at TEXT)")
        con.execute(
            "INSERT INTO gateway_attempts VALUES(?, ?)",
            ("nvidia", "2026-10-02T00:00:00Z"),
        )
        con.execute("INSERT INTO gateway_attempts VALUES(?, NULL)", ("google",))
    with pytest.raises(RuntimeError, match="already dispatched"):
        v1_smoke_once.check_prior_receipts(("nvidia",), [prior])
    v1_smoke_once.check_prior_receipts(("google", "cloudflare"), [prior])
    with pytest.raises(RuntimeError, match="requires prior"):
        v1_smoke_once.check_prior_receipts(("google",), [])
    with pytest.raises(RuntimeError, match="unavailable"):
        v1_smoke_once.check_prior_receipts(("google",), [tmp_path / "absent.db"])


def test_fixture_live_claim_precedes_token_and_independent_failures_continue(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "v1-smoke-fixture.db"
    monkeypatch.setattr(sys, "argv", ["v1_smoke_once.py", "--live", "--db", str(db)])
    monkeypatch.setattr(v1_smoke_once, "confirm_openrouter_free", lambda: None)
    monkeypatch.setattr(v1_smoke_once, "cli", lambda: "fixture-cli")
    monkeypatch.setattr(v1_smoke_once, "metadata_names", lambda _: v1_smoke_once.SECRETS)
    token_calls = []

    def token(_):
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT name FROM sqlite_master WHERE name='gateway_tasks'"
            ).fetchone()
        token_calls.append(1)
        return "fixture-token-never-printed"

    monkeypatch.setattr(v1_smoke_once, "service_token", token)

    def resolver(*_):
        def resolve(ref):
            if ref == "GEMINI_API_KEY":
                raise ValueError("fixture pre-send failure")
            return "fixture-account" if ref == "CLOUDFLARE_ACCOUNT_ID" else "fixture-secret"

        return resolve

    monkeypatch.setattr(v1_smoke_once, "doppler_resolver_from_token", resolver)
    provider_calls = []

    def transport(url, headers, payload, timeout):
        provider_calls.append(url)
        if "nvidia.com" in url:
            return 503, {}, b"{}"
        if "googleapis" in url:
            body = {
                "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
            }
        elif "cloudflare" in url:
            body = {
                "result": {"response": "OK"},
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "neurons": 2},
            }
        elif "ocr.space" in url:
            body = {
                "OCRExitCode": 1,
                "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "OK"}],
            }
        else:
            body = {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }
        return 200, {}, json.dumps(body).encode()

    monkeypatch.setattr(
        v1_smoke_once,
        "Gateway",
        lambda db, targets, key, resolver: RealGateway(
            db, targets, key, resolver, transport=transport
        ),
    )
    assert v1_smoke_once.main() == 2  # NVIDIA unknown; Google pre-send failure; five others run.
    output = capsys.readouterr().out
    assert len(provider_calls) == 6 and len(token_calls) == 1
    assert db.stat().st_mode & 0o777 == 0o600
    assert '"expected_text_ok": true' in output
    assert "fixture-token" not in output and "fixture-secret" not in output
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM gateway_tasks").fetchone()[0] == 7
        assert (
            con.execute(
                "SELECT state FROM gateway_tasks WHERE request_key='v1-smoke-nvidia'"
            ).fetchone()[0]
            == "unknown"
        )
        assert con.execute(
            "SELECT state,dispatched_at FROM gateway_attempts WHERE provider='google'"
        ).fetchone() == ("pre_send_failed", None)
    try:
        v1_smoke_once.main()
    except RuntimeError as exc:
        assert "new independent" in str(exc)
    else:
        raise AssertionError("same smoke receipt database must refuse re-run")
    assert len(token_calls) == 1
