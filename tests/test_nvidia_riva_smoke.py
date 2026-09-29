import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import nvidia_riva_translate_once as probe


def test_fixed_translation_transport_has_official_route_and_bounded_request(monkeypatch):
    seen = []
    stages = []

    class Response:
        status = 200

        def __init__(self):
            self.headers = {"NVCF-REQID": "safe-request-1"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit):
            assert limit == probe.MAX_RESPONSE_BYTES + 1
            return json.dumps(
                {
                    "choices": [{"message": {"content": "你好。"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    status, response = probe.riva_transport("fixture-key", lambda *values: stages.append(values))
    assert status == 200
    assert response["choices"][0]["message"]["content"] == "你好。"
    assert stages == [(200, "safe-request-1")]
    assert len(seen) == 1
    request, timeout = seen[0]
    assert timeout == 60
    assert request.full_url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert request.get_method() == "POST"
    payload = json.loads(request.data)
    assert payload == {
        "model": "nvidia/riva-translate-4b-instruct-v2",
        "messages": [
            {"role": "system", "content": "en-zh-cn"},
            {"role": "user", "content": "Hello."},
        ],
        "temperature": 0,
        "max_tokens": 16,
        "stream": False,
    }
    assert "chat_template_kwargs" not in payload


def test_new_receipt_is_independent_and_timeout_never_replays(tmp_path, monkeypatch):
    old = tmp_path / "nvidia-smoke-once.json"
    old.write_text('{"state":"unknown","transport_error":"TimeoutError"}')
    new = tmp_path / "nvidia-riva-translate-once.json"
    monkeypatch.setattr(probe, "checked_metadata", lambda *_: True)
    monkeypatch.setattr(probe, "create_service_token", lambda *_: "fixture-token")
    monkeypatch.setattr(
        probe, "doppler_resolver_from_token", lambda *_: lambda _name: "fixture-key"
    )
    calls = []

    def timeout(_key, on_headers):
        calls.append(1)
        on_headers(200, "request-2")
        raise TimeoutError()

    result = probe.run_once("/bin/doppler", new, timeout)
    assert result == {
        "state": "unknown",
        "transport_error": "TimeoutError",
        "transport_stage": "reading_body",
        "http_status": 200,
        "request_id": "request-2",
    }
    saved = json.loads(new.read_text())
    assert saved["state"] == "unknown"
    assert saved["model"] == probe.MODEL
    assert saved["transport_stage"] == "reading_body"
    assert "fixture-key" not in new.read_text()
    assert "fixture-token" not in new.read_text()
    assert old.read_text() == '{"state":"unknown","transport_error":"TimeoutError"}'
    with pytest.raises(RuntimeError, match="never replay"):
        probe.run_once("/bin/doppler", new, timeout)
    assert calls == [1]


def test_translation_success_keeps_answer_out_of_receipt(tmp_path, monkeypatch):
    receipt = tmp_path / "translation.json"
    monkeypatch.setattr(probe, "checked_metadata", lambda *_: True)
    monkeypatch.setattr(probe, "create_service_token", lambda *_: "fixture-token")
    monkeypatch.setattr(
        probe, "doppler_resolver_from_token", lambda *_: lambda _name: "fixture-key"
    )

    def translate(_key, on_headers):
        on_headers(200, None)
        return 200, {
            "choices": [{"message": {"content": "你好。"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        }

    result = probe.run_once("/bin/doppler", receipt, translate)
    assert result == {
        "state": "completed",
        "http_status": 200,
        "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        "translation": "你好。",
        "text_truncated": False,
    }
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "completed"
    assert saved["model"] == probe.MODEL
    assert saved["usage"] == {"prompt_tokens": 8, "completion_tokens": 4}
    assert "你好。" not in receipt.read_text()


def test_preflight_failure_keeps_correct_model_and_no_dispatch(tmp_path, monkeypatch):
    receipt = tmp_path / "preflight.json"
    monkeypatch.setattr(probe, "checked_metadata", lambda *_: True)

    def fail_token(_binary):
        raise RuntimeError("uncertain create")

    monkeypatch.setattr(probe, "create_service_token", fail_token)
    with pytest.raises(RuntimeError, match="uncertain create"):
        probe.run_once("/bin/doppler", receipt, lambda *_: pytest.fail("provider dispatch"))
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "preflight_failed"
    assert saved["model"] == probe.MODEL
