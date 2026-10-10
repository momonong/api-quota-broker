"""Explicit local files, safe typed output, and bounded HTTP media; fixtures only."""

import base64
import io
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_families import media, wav
from test_family_gateway import catalog, embedding_task, setup
from test_gateway import HMAC_KEY, NOW, target

from quota_broker import cli
from quota_broker.config import Quota
from quota_broker.families import MediaLimits
from quota_broker.gateway import Gateway
from quota_broker.gateway_server import _bounded_json, _json_depth, make_gateway_server
from quota_broker.queue import DurableQueue

TOKEN = "fixture-media-client-token-over-32-characters"


def defaults(**changes):
    return SimpleNamespace(request_key="fixture-media", capability="vision", **changes)


def test_input_file_size_regular_type_and_explicit_mime(tmp_path):
    raw = base64.b64decode(media()["data"])
    image = tmp_path / "image.bin"
    image.write_bytes(raw)
    assert cli._input_part(str(image), "image/png", len(raw)) == media()
    for mime in (None, "text/plain", "image/unknown"):
        with pytest.raises((ValueError, TypeError)) as error:
            cli._input_part(str(image), mime, len(raw))
        assert str(image) not in str(error.value)
    with pytest.raises(ValueError, match="limit"):
        cli._input_part(str(image), "image/png", len(raw) - 1)
    link = tmp_path / "link"
    link.symlink_to(image)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in (link, fifo, tmp_path):
        with pytest.raises(ValueError):
            cli._input_part(str(path), "image/png", 100)


def test_file_media_preserves_messages_options_and_refuses_conflicting_inputs(
    tmp_path, monkeypatch
):
    image = tmp_path / "fixture.png"
    image.write_bytes(base64.b64decode(media()["data"]))
    original = {
        "input": {
            "messages": [
                {"role": "user", "content": "describe"},
                {"role": "assistant", "content": "history"},
            ]
        },
        "options": {"temperature": 0.2},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(original)))
    body = cli._typed_task(defaults(input_file=str(image), mime_type="image/png"), MediaLimits())
    assert body["input"]["messages"][0]["content"] == [
        {"type": "text", "text": "describe"},
        media(),
    ]
    assert body["input"]["messages"][1]["content"] == "history"
    assert body["options"] == original["options"]
    assert original["input"]["messages"][0]["content"] == "describe"
    assert cli._attach_media({"capability": "ocr"}, media())["input"] == {"document": media()}
    assert cli._attach_media({"capability": "audio_transcription"}, wav())["input"] == {
        "audio": wav()
    }
    for body in (
        {"capability": "embedding"},
        {"capability": "ocr", "input": {"document": media()}},
        {"capability": "vision", "input": "existing"},
        {"capability": "audio_transcription"},
    ):
        with pytest.raises(ValueError):
            cli._attach_media(body, media())


def test_typed_json_is_explicit_bounded_and_cli_identity_cannot_overwrite_it(monkeypatch):
    for content in (
        '{"input":"' + "x" * 100 + '"}',
        "{invalid",
        '{"request_key":"another"}',
        '{"capability":"embedding"}',
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        with pytest.raises(ValueError):
            cli._typed_task(defaults(), MediaLimits(request_bytes=80))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"input":{"messages":[{"role":"user","content":"text"}]},"options":{"temperature":0.1}}'
        ),
    )
    body = cli._typed_task(defaults(), MediaLimits())
    assert body["options"] == {"temperature": 0.1}


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("kind", ["image", "audio"])
def test_output_file_is_private_exclusive_and_stdout_redacts_only_media(tmp_path, nested, kind):
    part = media() if kind == "image" else wav()
    value = {"images": [part]} if kind == "image" else {"audio": part}
    response = {
        "state": "completed",
        "result": {"state": "completed", "result": value} if nested else value,
    }
    output = tmp_path / "approved-output"
    safe = cli._write_media_result(response, str(output), MediaLimits())
    assert output.read_bytes() == base64.b64decode(part["data"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert safe["output_written"] and "data" not in json.dumps(safe)
    assert "data" in json.dumps(response)
    assert "bytes" in json.dumps(safe) and str(output) not in json.dumps(safe)
    with pytest.raises(ValueError, match="exclusively"):
        cli._write_media_result(response, str(output), MediaLimits())
    assert output.read_bytes() == base64.b64decode(part["data"])


def test_media_output_rejects_urls_multiple_images_malformed_and_over_bound(tmp_path):
    path = tmp_path / "never-created"
    for result in (
        {"images": [media(), media()]},
        {"images": [{**media(), "data": "https://fixture.invalid/image.png"}]},
        {"audio": {**wav(), "mime_type": "text/plain"}},
        {"text": "legacy answer"},
    ):
        with pytest.raises((ValueError, TypeError)):
            cli._write_media_result({"result": result}, str(path), MediaLimits())
        assert not path.exists()
    with pytest.raises(ValueError):
        cli._write_media_result(
            {"result": {"images": [media()]}}, str(path), MediaLimits(result_bytes=16)
        )
    assert not path.exists()


def invoke(monkeypatch, capsys, argv, content=""):
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(content))
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_cli_typed_file_and_result_stdout_require_explicit_flags(tmp_path, monkeypatch, capsys):
    credential = tmp_path / "fixture-token"
    credential.write_text(TOKEN)
    image = tmp_path / "fixture.png"
    image.write_bytes(base64.b64decode(media()["data"]))
    calls = []
    response = {"state": "completed", "result": {"images": [media()]}}

    def fake_http(url, data, headers, timeout):
        calls.append((url, data))
        return response

    monkeypatch.setattr(cli, "_json_http", fake_http)
    base = ["quota-broker", "gateway", "--token-file", str(credential)]
    args = [
        "run",
        "--request-key",
        "fixture",
        "--capability",
        "vision",
        "--input-file",
        str(image),
        "--mime-type",
        "image/png",
    ]
    assert invoke(monkeypatch, capsys, base + args) == response
    assert calls[-1][1]["input"] == {"messages": [{"role": "user", "content": [media()]}]}
    output = tmp_path / "result.png"
    safe = invoke(monkeypatch, capsys, base + ["result", "fixture", "--output-file", str(output)])
    assert safe["output_written"] and "data" not in json.dumps(safe)
    body = {"input": {"texts": ["fixture"]}, "options": {"dimensions": 4}}
    assert (
        invoke(
            monkeypatch,
            capsys,
            base + ["run", "--request-key", "fixture", "--capability", "embedding", "--task-stdin"],
            json.dumps(body),
        )
        == response
    )
    assert calls[-1][1]["options"] == {"dimensions": 4}
    captured = len(calls)
    with pytest.raises(ValueError, match="limit"):
        invoke(monkeypatch, capsys, base + ["--max-media-input-bytes", "8"] + args)
    assert len(calls) == captured


def test_json_depth_strings_and_serialized_result_default_boundary():
    _json_depth(b'{"input":"[[[{{{\\""}')
    with pytest.raises(ValueError):
        _json_depth(b"[" * 33 + b"0" + b"]" * 33)
    maximum = MediaLimits().result_bytes
    assert len(_bounded_json({"x": "a" * (maximum - 8)}, maximum)) == maximum
    with pytest.raises(ValueError):
        _bounded_json({"x": "a" * (maximum - 7)}, maximum)


@pytest.fixture
def api(tmp_path):
    def forbidden(*_args):
        raise AssertionError("no credential or provider I/O permitted")

    gateway = Gateway(
        tmp_path / "media.sqlite", (), HMAC_KEY, forbidden, forbidden, clock=lambda: NOW
    )
    server = make_gateway_server(gateway, TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def http(api, body, *, declared_length=None):
    headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    request = urllib.request.Request(
        api[1] + "/v1/tasks",
        body,
        headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_http_rejects_default_input_8m_request_16m_and_depth_before_provider(api):
    too_large = media(raw=b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024))
    body = {
        "request_key": "oversized-input",
        "capability": "vision",
        "input": {"messages": [{"role": "user", "content": [too_large]}]},
    }
    raw = json.dumps(body).encode()
    assert len(raw) < MediaLimits().request_bytes
    assert http(api, raw)[0] == 400
    assert http(api, b"{}", declared_length=MediaLimits().request_bytes + 1)[0] == 400
    assert http(api, b'{"input":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}")[0] == 400


def test_http_serialized_result_bound_fixed_error_and_custom_limits(api, monkeypatch):
    monkeypatch.setattr(
        api[0],
        "run",
        lambda _body: {"state": "completed", "result": {"text": "x" * MediaLimits().result_bytes}},
    )
    status, failure = http(api, b"{}")
    assert status == 503 and failure == {"error": "internal_error"}
    assert "x" not in json.dumps(failure)


def test_gateway_serve_media_configuration_is_passed_without_execution(tmp_path, monkeypatch):
    observed = {}
    config = tmp_path / "config.json"
    config.write_text('{"targets":[]}')
    digest = tmp_path / "fixture-digest"
    digest.write_bytes(HMAC_KEY)
    credential = tmp_path / "fixture-token"
    credential.write_text(TOKEN)

    def gateway(*_args, **kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "Gateway", gateway)
    monkeypatch.setattr(cli, "doppler_resolver", lambda *_args: object())
    monkeypatch.setattr(
        cli,
        "make_gateway_server",
        lambda *_args, **_kwargs: SimpleNamespace(
            serve_forever=lambda: None, server_close=lambda: None
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quota-broker",
            "gateway-serve",
            "--config",
            str(config),
            "--db",
            str(tmp_path / "db"),
            "--digest-key-file",
            str(digest),
            "--client-token-file",
            str(credential),
            "--doppler-token-file",
            "unused-fixture",
            "--doppler-project",
            "fixture",
            "--doppler-config",
            "fixture",
            "--max-media-input-bytes",
            "1024",
            "--max-request-bytes",
            "2048",
            "--max-result-bytes",
            "4096",
        ],
    )
    cli.main()
    assert observed["media_limits"] == MediaLimits(1024, 2048, 4096)


def private_database(tmp_path, name):
    tmp_path.chmod(0o700)
    descriptor = os.open(tmp_path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def server_for(gateway, queue=None):
    server = make_gateway_server(gateway, TOKEN, port=0, queue=queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, "http://127.0.0.1:" + str(server.server_port)


def client_base(tmp_path, base):
    credential = tmp_path / "fixture-client-token"
    credential.write_text(TOKEN)
    return ["quota-broker", "gateway", "--json", "--url", base, "--token-file", str(credential)]


def test_real_embedding_http_cli_and_encrypted_queue_lifecycle(tmp_path, monkeypatch, capsys):
    private_database(tmp_path, "typed.sqlite")
    gateway, calls, _secrets = setup(tmp_path)
    queue = DurableQueue(gateway, b"fixture-private-queue-key".ljust(32, b"!"))
    server, thread, base = server_for(gateway, queue)
    argv = client_base(tmp_path, base)
    args = ["--request-key", "sync-vectors", "--capability", "embedding", "--task-stdin"]
    value = embedding_task("sync-vectors")
    try:
        explain = invoke(monkeypatch, capsys, argv + ["explain", *args], json.dumps(value))
        assert explain["selected_target_id"] == "embed" and calls == []
        done = invoke(monkeypatch, capsys, argv + ["run", *args], json.dumps(value))
        assert done["state"] == "completed" and done["result"] == {
            "vectors": [[0.1, 0.2], [0.3, 0.4]]
        }
        assert len(calls) == 1 and gateway.validate_task(value)["max_output_tokens"] == 0
        args[1] = "queued-vectors"
        queued = embedding_task("queued-vectors")
        submitted = invoke(monkeypatch, capsys, argv + ["submit", *args], json.dumps(queued))
        assert submitted["state"] == "queued" and "result" not in submitted
        tick = invoke(monkeypatch, capsys, argv + ["worker", "--once"])
        assert tick["state"] == "completed" and "result" not in tick
        received = invoke(monkeypatch, capsys, argv + ["result", "queued-vectors"])
        assert received["result"] == done["result"] and len(calls) == 2
        assert "result" not in invoke(
            monkeypatch, capsys, argv + ["queue-status", "queued-vectors"]
        )
        stored = (tmp_path / "typed.sqlite").read_bytes()
        assert b"private fixture source" not in stored and b'"vectors"' not in stored
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def audio_gateway(tmp_path, capability, profile, raw_response, headers=None):
    features = ["audio_input", "text"] if capability == "audio_transcription" else ["tts", "text"]
    registry = catalog("groq", profile, capability, "fixture-audio", features=features)
    spec = registry.resolve("fixture-audio", "groq")
    item = replace(
        target("audio", "groq", "fixture-audio", capability=capability),
        quotas=(Quota("audio-requests", "requests", 10, "day"),),
        model_info=spec,
    )
    calls = []

    def transport(request, timeout, bound):
        calls.append(request)
        return 200, headers or {}, raw_response

    gateway = Gateway(
        tmp_path / "audio.sqlite",
        (item,),
        HMAC_KEY,
        lambda _ref: "fixture-local-secret",
        clock=lambda: NOW,
        registry=registry,
        family_transport=transport,
    )
    return gateway, calls


def test_real_audio_file_http_cli_ingestion_uses_inline_part_no_local_path(
    tmp_path, monkeypatch, capsys
):
    audio = tmp_path / "explicit-approved-input.wav"
    audio.write_bytes(base64.b64decode(wav()["data"]))
    gateway, calls = audio_gateway(
        tmp_path,
        "audio_transcription",
        "groq_audio_transcription",
        json.dumps({"text": "fixture transcription", "language": "en"}).encode(),
    )
    server, thread, base = server_for(gateway)
    try:
        args = [
            "run",
            "--request-key",
            "typed-audio",
            "--capability",
            "audio_transcription",
            "--input-file",
            str(audio),
            "--mime-type",
            "audio/wav",
        ]
        done = invoke(
            monkeypatch, capsys, client_base(tmp_path, base) + args, '{"options":{"language":"en"}}'
        )
        assert done["state"] == "completed" and done["result"]["text"] == "fixture transcription"
        assert len(calls) == 1 and audio.read_bytes() in calls[0].body
        assert b'filename="input.audio"' in calls[0].body
        assert str(audio).encode() not in calls[0].body
        assert str(audio) not in json.dumps(done)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_tts_http_cli_export_and_encrypted_queue_result(tmp_path, monkeypatch, capsys):
    private_database(tmp_path, "audio.sqlite")
    binary = base64.b64decode(wav()["data"])
    gateway, calls = audio_gateway(
        tmp_path, "tts", "groq_tts", binary, {"content-type": "audio/wav"}
    )
    queue = DurableQueue(gateway, b"fixture-private-queue-key".ljust(32, b"!"))
    server, thread, base = server_for(gateway, queue)
    argv = client_base(tmp_path, base)
    output = tmp_path / "approved-voice.wav"
    value = {
        "input": {"text": "private fixture speech"},
        "options": {"voice": "troy", "format": "wav"},
    }
    try:
        args = [
            "run",
            "--request-key",
            "sync-tts",
            "--capability",
            "tts",
            "--task-stdin",
            "--output-file",
            str(output),
        ]
        done = invoke(monkeypatch, capsys, argv + args, json.dumps(value))
        assert done["state"] == "completed" and done["output_written"]
        assert output.read_bytes() == binary and stat.S_IMODE(output.stat().st_mode) == 0o600
        assert (
            "data" not in json.dumps(done) and done["result"]["audio"]["mime_type"] == "audio/wav"
        )
        queued = invoke(
            monkeypatch,
            capsys,
            argv + ["submit", "--request-key", "queued-tts", "--capability", "tts", "--task-stdin"],
            json.dumps(value),
        )
        assert queued["state"] == "queued"
        assert invoke(monkeypatch, capsys, argv + ["worker", "--once"])["state"] == "completed"
        full = invoke(monkeypatch, capsys, argv + ["result", "queued-tts"])
        assert base64.b64decode(full["result"]["audio"]["data"]) == binary
        other = tmp_path / "approved-queued-voice.wav"
        exported = invoke(
            monkeypatch, capsys, argv + ["result", "queued-tts", "--output-file", str(other)]
        )
        assert exported["output_written"] and other.read_bytes() == binary
        assert len(calls) == 2
        with pytest.raises(ValueError, match="exclusively"):
            invoke(
                monkeypatch, capsys, argv + ["result", "queued-tts", "--output-file", str(other)]
            )
        assert other.read_bytes() == binary and len(calls) == 2
        assert b"private fixture speech" not in (tmp_path / "audio.sqlite").read_bytes()
        assert binary not in (tmp_path / "audio.sqlite").read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
