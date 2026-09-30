import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import nvidia_smoke_once as smoke


def fake_access(monkeypatch, calls):
    monkeypatch.setattr(smoke, "checked_metadata", lambda binary: binary == "/bin/doppler")

    def create(binary):
        calls.append(("token", binary))
        return "dp.st.dev." + "a" * 40

    def resolver(token, project, config):
        calls.append(("resolver", project, config))
        assert token == "dp.st.dev." + "a" * 40

        def read(name):
            calls.append(("read", name))
            return "fixture-provider-key"

        return read

    monkeypatch.setattr(smoke, "create_service_token", create)
    monkeypatch.setattr(smoke, "doppler_resolver_from_token", resolver)


def test_smoke_success_one_dispatch_and_secret_free_receipt(tmp_path, monkeypatch):
    calls = []
    fake_access(monkeypatch, calls)

    def transport(key, prompt, maximum):
        calls.append(("provider", prompt, maximum))
        assert key == "fixture-provider-key"
        return 200, {
            "choices": [{"message": {"content": "OK."}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    receipt = tmp_path / "private" / "receipt.json"
    result = smoke.run_once("/bin/doppler", receipt, transport)
    assert result == {
        "http_status": 200,
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        "state": "completed",
        "text_present": True,
        "answer_is_ok": True,
    }
    assert calls == [
        ("token", "/bin/doppler"),
        ("resolver", smoke.PROJECT, smoke.CONFIG),
        ("read", smoke.SECRET_NAME),
        ("provider", "Reply with OK.", 16),
    ]
    assert receipt.parent.stat().st_mode & 0o077 == 0
    assert receipt.stat().st_mode & 0o077 == 0
    saved = receipt.read_text()
    for secret in ("dp.st.dev.", "fixture-provider-key", "Reply with OK.", "OK."):
        assert secret not in saved
    assert json.loads(saved)["state"] == "completed"
    with pytest.raises(RuntimeError, match="never replay"):
        smoke.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("second POST"))
    assert len(calls) == 4  # Existing receipt blocks even a second Doppler token.


def test_smoke_uncertain_send_stays_blocked(tmp_path, monkeypatch):
    calls = []
    fake_access(monkeypatch, calls)

    def timeout(*_):
        calls.append(("provider",))
        raise TimeoutError("maybe sent")

    receipt = tmp_path / "private" / "receipt.json"
    result = smoke.run_once("/bin/doppler", receipt, timeout)
    assert result == {"state": "unknown", "transport_error": "TimeoutError"}
    assert json.loads(receipt.read_text())["state"] == "unknown"
    with pytest.raises(RuntimeError, match="never replay"):
        smoke.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("second POST"))
    assert calls.count(("provider",)) == 1
    assert calls.count(("token", "/bin/doppler")) == 1


def test_smoke_pending_keeps_request_id_without_polling(tmp_path, monkeypatch):
    calls = []
    fake_access(monkeypatch, calls)
    receipt = tmp_path / "private" / "receipt.json"
    result = smoke.run_once(
        "/bin/doppler",
        receipt,
        lambda *_: (202, {"requestId": "request-123", "detail": "do not print"}),
    )
    assert result == {
        "http_status": 202,
        "usage": None,
        "state": "pending_unknown",
        "request_id": "request-123",
    }
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "pending_unknown"
    assert saved["request_id"] == "request-123"
    assert "do not print" not in receipt.read_text()
    with pytest.raises(RuntimeError, match="never replay"):
        smoke.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("second POST"))
    assert calls.count(("token", "/bin/doppler")) == 1


def test_smoke_provider_error_suppresses_response_body(tmp_path, monkeypatch):
    calls = []
    fake_access(monkeypatch, calls)
    receipt = tmp_path / "private" / "receipt.json"
    result = smoke.run_once(
        "/bin/doppler",
        receipt,
        lambda *_: (402, {"error": "private provider detail"}),
    )
    assert result == {"http_status": 402, "usage": None, "state": "http_error"}
    assert "private provider detail" not in receipt.read_text()


def test_smoke_existing_receipt_blocks_before_doppler(tmp_path, monkeypatch):
    receipt = tmp_path / "private" / "receipt.json"
    receipt.parent.mkdir()
    receipt.write_text('{"state":"dispatching"}')
    monkeypatch.setattr(
        smoke, "checked_metadata", lambda *_: pytest.fail("metadata queried after receipt")
    )
    with pytest.raises(RuntimeError, match="never replay"):
        smoke.run_once("/bin/doppler", receipt)


def test_smoke_failed_token_creation_preserves_receipt(tmp_path, monkeypatch):
    calls = []
    fake_access(monkeypatch, calls)
    monkeypatch.setattr(
        smoke,
        "create_service_token",
        lambda *_: (_ for _ in ()).throw(RuntimeError("uncertain token creation")),
    )
    receipt = tmp_path / "private" / "receipt.json"
    with pytest.raises(RuntimeError, match="uncertain token creation"):
        smoke.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("provider called"))
    assert json.loads(receipt.read_text())["state"] == "preflight_failed"
    with pytest.raises(RuntimeError, match="never replay"):
        smoke.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("provider called"))
