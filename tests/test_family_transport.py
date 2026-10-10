"""Pure child and pipe fixtures for bounded official family POST transport."""

import base64
import builtins
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace

import pytest

from quota_broker import family_transport as transport
from quota_broker.family_transport import (
    form_request,
    json_request,
    send_family_request,
)
from quota_broker.gateway_providers import ProviderError, ProviderPhaseTimeout
from quota_broker.provider_policy import ENDPOINT_HOSTS

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SECRET = "fixture-auth-marker"
PRIVATE_BODY = "fixture-private-body-你好"


def request():
    return json_request(URL, {"Authorization": "Bearer " + SECRET}, {"text": PRIVATE_BODY})


def wire(raw=b"fixture", status=200, headers=None):
    return json.dumps({"status": status, "headers": headers or {}}).encode() + b"\n" + raw


class FakeProcess:
    def __init__(self, output=b"", returncode=0, *, timeout=False):
        self.output = output
        self.returncode = returncode
        self.timeout = timeout
        self.communications = []
        self.events = []
        self.waits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()

    def communicate(self, input=None, timeout=None):
        self.communications.append((input, timeout))
        self.events.append("communicate")
        if self.timeout and len(self.communications) == 1:
            raise subprocess.TimeoutExpired("fixed-child", timeout)
        return self.output, None

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self):
        self.events.append("wait")
        self.waits += 1
        return self.returncode


def fake_child(monkeypatch, output=None, returncode=0, *, timeout=False):
    process = FakeProcess(wire() if output is None else output, returncode, timeout=timeout)
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return process, calls


def forbid_child(monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: pytest.fail("invalid request spawned child")
    )


@pytest.mark.parametrize(
    "host", sorted({host for hosts in ENDPOINT_HOSTS.values() for host in hosts})
)
def test_only_registered_official_https_hosts_use_single_child(monkeypatch, host):
    process, calls = fake_child(monkeypatch)
    assert (
        send_family_request(replace(request(), url="https://" + host + "/v1/fixture"), 7, 100)[0]
        == 200
    )
    assert len(calls) == 1 and len(process.communications) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.groq.com/v1/fixture",
        "https://unofficial.invalid/v1/fixture",
        "https://user@api.groq.com/v1/fixture",
        "https://user:pass@api.groq.com/v1/fixture",
        "https://api.groq.com:443/v1/fixture",
        "https://api.groq.com/v1/fixture?query=private",
        "https://api.groq.com/v1/fixture#fragment",
        "https://api.groq.com.evil.invalid/v1/fixture",
        "https://api.groq.com./v1/fixture",
        "https://API.GROQ.COM/v1/fixture",
        "https://[broken",
        "https://api.groq.com/v1/fixture\x00",
        "https://api.groq.com/v1/fixture\t",
        "https://api.groq.com/v1/fixture\n",
        "https://api.groq.com/v1/fixture\x7f",
    ],
)
def test_url_identity_and_raw_control_bytes_rejected_before_child(monkeypatch, url):
    forbid_child(monkeypatch)
    with pytest.raises(ProviderError):
        send_family_request(replace(request(), url=url), 7, 100)


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "fixture\r\nInjected: yes"},
        {"Authorization": "fixture\x00"},
        {"Authorization": "fixture\t"},
        {"Authorization": "fixture\x7f"},
        {"Bad\nName": "fixture"},
        {"Bad:Name": "fixture"},
        {"Bad Name": "fixture"},
        {"": "fixture"},
        {"Authorization": 1},
        {1: "fixture"},
    ],
)
def test_header_names_values_and_control_bytes_rejected_before_child(monkeypatch, headers):
    forbid_child(monkeypatch)
    with pytest.raises(ProviderError):
        send_family_request(replace(request(), headers=headers), 7, 100)


@pytest.mark.parametrize(
    "content_type",
    ["application/json\r\nX: injected", "text/plain\x00", "text/plain\t", "text/plain\x7f", 1],
)
def test_content_type_cannot_smuggle_headers_or_controls(monkeypatch, content_type):
    forbid_child(monkeypatch)
    with pytest.raises(ProviderError):
        send_family_request(replace(request(), content_type=content_type), 7, 100)


@pytest.mark.parametrize(
    "timeout,bound",
    [
        (0, 10),
        (-1, 10),
        (181, 10),
        (float("nan"), 10),
        (float("inf"), 10),
        (1, 0),
        (1, -1),
        (1, True),
        (1, 128 * 1024 * 1024 + 1),
    ],
)
def test_total_deadline_and_response_bound_are_strict_before_child(monkeypatch, timeout, bound):
    forbid_child(monkeypatch)
    with pytest.raises(ProviderError):
        send_family_request(request(), timeout, bound)


def test_credentials_body_and_url_are_only_in_stdin_and_binary_response_is_preserved(monkeypatch):
    binary = b"\x00\xff\x80\n\r\nfixture\0"
    process, calls = fake_child(
        monkeypatch, wire(binary, headers={"Content-Length": str(len(binary))})
    )
    status, headers, raw = send_family_request(request(), 13, 100)
    assert status == 200 and raw == binary and headers["Content-Length"] == str(len(binary))
    argv, kwargs = calls[0]
    assert argv == [sys.executable, "-c", transport._HTTP_CHILD]
    assert all(marker not in " ".join(argv) for marker in (SECRET, PRIVATE_BODY, URL))
    assert kwargs == {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
    }
    stdin, timeout = process.communications[0]
    config = json.loads(stdin)
    assert config["headers"]["Authorization"] == "Bearer " + SECRET
    assert config["url"] == URL and config["timeout"] == timeout == 13
    assert base64.b64decode(config["body"], validate=True) == request().body
    assert process.waits == 1


def test_total_timeout_covers_upload_and_response_kills_drains_waits_without_retry(monkeypatch):
    # Parent cannot observe these phases; the same wall-clock communicate deadline
    # bounds all of them, including writing the potentially large stdin request.
    process, calls = fake_child(monkeypatch, timeout=True)
    with pytest.raises(ProviderPhaseTimeout, match="family_total_deadline") as exc:
        send_family_request(request(), 3, 100)
    assert SECRET not in str(exc.value) and PRIVATE_BODY not in str(exc.value)
    assert len(calls) == 1
    assert process.communications[0][1] == 3
    assert process.communications[1] == (None, None)
    assert process.events == ["communicate", "kill", "communicate", "wait"]


@pytest.mark.parametrize(
    "returncode,reason",
    [(3, "family_response_bound"), (2, "family_transport_failed"), (-9, "family_transport_failed")],
)
def test_child_failure_has_fixed_safe_error_and_no_retry(monkeypatch, returncode, reason):
    process, calls = fake_child(monkeypatch, (SECRET + PRIVATE_BODY).encode(), returncode)
    with pytest.raises(ProviderError, match=reason) as exc:
        send_family_request(request(), 3, 100)
    assert SECRET not in str(exc.value) and PRIVATE_BODY not in str(exc.value)
    assert len(calls) == 1 and len(process.communications) == 1


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"no-delimiter",
        b"not-json\nbody",
        b"[]\nbody",
        b"null\nbody",
        b"1\nbody",
        b"{}\nbody",
        b'{"status":200}\nbody',
        b'{"status":200,"headers":[]}\nbody',
        b'{"status":true,"headers":{}}\nbody',
        b'{"status":99,"headers":{}}\nbody',
        b'{"status":600,"headers":{}}\nbody',
        b'{"status":"200","headers":{}}\nbody',
        b'{"status":200,"headers":{"x":1}}\nbody',
        b"\xff\nbody",
        b" " * 16385 + b"\nbody",
        wire(b"a" * 101),
        wire(b"short", headers={"Content-Length": "100"}),
        wire(b"body", headers={"Content-Length": "invalid"}),
        wire(b"body", headers={"Content-Length": "-1"}),
        wire(b"body", headers={"Content-Length": "4", "content-length": "5"}),
        wire(b"body", headers={"X-Request-Id": "fixture\r\nprivate"}),
        wire(b"body", headers={"X-Request-Id": "fixture\x00private"}),
    ],
)
def test_oversized_truncated_malformed_or_invalid_response_has_safe_error(monkeypatch, output):
    fake_child(monkeypatch, output)
    with pytest.raises(ProviderError, match="invalid_family_response") as exc:
        send_family_request(request(), 3, 100)
    assert SECRET not in str(exc.value) and PRIVATE_BODY not in str(exc.value)


def test_json_and_multipart_helpers_preserve_content_without_file_writes(monkeypatch):
    monkeypatch.setattr(builtins, "open", lambda *a, **kw: pytest.fail("payload wrote a file"))
    payload = {"text": "你好\nline2", "messages": [{"role": "user", "content": "fixture"}]}
    encoded = json_request(URL, {"Authorization": SECRET}, payload)
    assert json.loads(encoded.body) == payload and encoded.content_type == "application/json"
    binary = b"\x00\xff\r\n\x80fixture"
    form = form_request(
        URL,
        {"Authorization": SECRET},
        {"prompt": "你好\nline2"},
        file_field="file",
        file_bytes=binary,
        file_mime="audio/wav",
        filename="input.wav",
    )
    boundary = form.content_type.split("boundary=", 1)[1].encode()
    assert form.body.startswith(b"--" + boundary + b"\r\n")
    assert form.body.endswith(b"--" + boundary + b"--\r\n")
    assert "你好\nline2".encode() in form.body and binary in form.body
    assert b'name="file"; filename="input.wav"' in form.body
    assert b"Content-Type: audio/wav\r\n\r\n" in form.body
    assert form.payload == {"prompt": "你好\nline2"}


@pytest.mark.parametrize(
    "fields,file_args",
    [
        ({"bad\nname": "fixture"}, {}),
        ({"bad\x00name": "fixture"}, {}),
        ({"field": 1}, {}),
        (
            {"field": "fixture"},
            {"file_field": "file", "file_bytes": None, "file_mime": "audio/wav"},
        ),
        (
            {"field": "fixture"},
            {"file_field": "file", "file_bytes": b"fixture", "file_mime": "audio/wav\r\nX: bad"},
        ),
        (
            {"field": "fixture"},
            {
                "file_field": "file",
                "file_bytes": b"fixture",
                "file_mime": "audio/wav",
                "filename": "input\x00.wav",
            },
        ),
        (
            {"field": "fixture"},
            {"file_field": "file", "file_bytes": "not-bytes", "file_mime": "audio/wav"},
        ),
    ],
)
def test_multipart_metadata_and_types_fail_safely(fields, file_args):
    with pytest.raises(ProviderError):
        form_request(URL, {}, fields, **file_args)


def run_child_fixture(
    monkeypatch,
    raw=b"binary\x00\xff",
    *,
    bound=100,
    status=200,
    http_error=False,
    response_headers=None,
    read_error=False,
):
    """Execute fixed child code with an entirely in-memory urllib fixture."""
    captured = {"opens": 0, "reads": []}

    class Response(io.BytesIO):
        code = status

        def __init__(self, content):
            super().__init__(content)
            self.headers = response_headers or {"Content-Type": "application/octet-stream"}

        def read(self, size=-1):
            captured["reads"].append(size)
            if read_error:
                raise OSError("fixture upstream failure")
            return super().read(size)

    class Opener:
        def open(self, req, timeout):
            captured["opens"] += 1
            captured["request"] = req
            captured["timeout"] = timeout
            response = Response(raw)
            if http_error:
                raise urllib.error.HTTPError(
                    URL, status, "fixture failure", response.headers, response
                )
            return response

    def build_opener(handler):
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        assert (
            handler.redirect_request(None, None, 302, "redirect", {}, "https://unofficial.invalid")
            is None
        )
        return Opener()

    config = {
        "url": URL,
        "headers": {"Authorization": SECRET},
        "body": base64.b64encode(b"fixture-body").decode(),
        "content_type": "application/json",
        "timeout": 9,
        "response_bound": bound,
    }
    output = io.BytesIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(config).encode())))
        patch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        patch.setattr(urllib.request, "build_opener", build_opener)
        try:
            exec(transport._HTTP_CHILD, {})  # noqa: S102 - trusted constant with fixture-only urllib
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue(), captured


def test_fixed_child_posts_once_disables_redirects_and_preserves_binary_response(monkeypatch):
    code, output, captured = run_child_fixture(monkeypatch)
    assert code == 0 and captured["opens"] == 1 and captured["reads"] == [101]
    req = captured["request"]
    assert req.get_method() == "POST" and req.data == b"fixture-body"
    assert req.get_header("Authorization") == SECRET and req.full_url == URL
    head, _, raw = output.partition(b"\n")
    assert json.loads(head)["status"] == 200 and raw == b"binary\x00\xff"


def test_fixed_child_oversized_read_exits_with_no_output_or_retry(monkeypatch):
    code, output, captured = run_child_fixture(monkeypatch, raw=b"x" * 101)
    assert code == 3 and output == b"" and captured["opens"] == 1


def test_deep_malformed_child_header_is_safe_provider_error(monkeypatch):
    fake_child(monkeypatch, b"[" * 400 + b"]" * 400 + b"\nbody")
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(200)
        with pytest.raises(ProviderError, match="invalid_family_response"):
            send_family_request(request(), 3, 100)
    finally:
        sys.setrecursionlimit(previous)


def test_spawn_failure_does_not_leak_os_diagnostic_or_request_content(monkeypatch):
    def fail(*a, **kw):
        raise OSError(PRIVATE_BODY)

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(ProviderError) as exc:
        send_family_request(request(), 3, 100)
    assert PRIVATE_BODY not in str(exc.value)


@pytest.mark.parametrize("status", [302, 307, 429, 500, 503])
def test_child_http_errors_remain_single_response_without_follow_or_retry(monkeypatch, status):
    code, output, captured = run_child_fixture(monkeypatch, status=status, http_error=True)
    assert code == 0 and captured["opens"] == 1 and captured["reads"] == [101]
    head, _, raw = output.partition(b"\n")
    assert json.loads(head)["status"] == status and raw == b"binary\x00\xff"


def test_child_response_headers_bound_has_no_output_or_retry(monkeypatch):
    code, output, captured = run_child_fixture(monkeypatch, response_headers={"x": "a" * 16385})
    assert code == 3 and output == b"" and captured["opens"] == 1


def test_child_body_read_failure_is_fixed_failure_without_retry(monkeypatch):
    code, output, captured = run_child_fixture(monkeypatch, read_error=True)
    assert code == 2 and output == b"" and captured["opens"] == 1


def test_child_rejects_duplicate_content_length_before_dict_collapses_headers(monkeypatch):
    headers = Message()
    headers.add_header("Content-Length", "100")
    headers.add_header("Content-Length", "4")
    code, output, captured = run_child_fixture(monkeypatch, raw=b"body", response_headers=headers)
    assert code == 2 and output == b"" and captured["opens"] == 1 and captured["reads"] == []


def test_child_rejects_ambiguous_length_and_transfer_encoding(monkeypatch):
    code, output, captured = run_child_fixture(
        monkeypatch, response_headers={"Content-Length": "4", "Transfer-Encoding": "chunked"}
    )
    assert code == 2 and output == b"" and captured["reads"] == []


def test_parent_rejects_ambiguous_length_and_transfer_encoding(monkeypatch):
    fake_child(
        monkeypatch, wire(b"body", headers={"Content-Length": "4", "Transfer-Encoding": "chunked"})
    )
    with pytest.raises(ProviderError, match="invalid_family_response"):
        send_family_request(request(), 3, 100)


def test_overflowing_deadline_and_reserved_headers_rejected_before_child(monkeypatch):
    forbid_child(monkeypatch)
    for timeout in (True, "3", 10**1000):
        with pytest.raises(ProviderError):
            send_family_request(request(), timeout, 100)
    for header in ("Host", "Content-Length", "Transfer-Encoding", "Proxy-Authorization"):
        with pytest.raises(ProviderError):
            send_family_request(replace(request(), headers={header: "fixture"}), 3, 100)


@pytest.mark.parametrize("payload", [{"private": float("nan")}, {"private": object()}])
def test_json_helper_has_fixed_safe_invalid_payload_error(payload):
    with pytest.raises(ProviderError, match="invalid_family_json"):
        json_request(URL, {}, payload)


def test_duplicate_json_envelope_or_header_keys_are_rejected(monkeypatch):
    for output in (
        b'{"status":200,"status":500,"headers":{}}\nbody',
        b'{"status":200,"headers":{"X-Id":"first","X-Id":"second"}}\nbody',
    ):
        fake_child(monkeypatch, output)
        with pytest.raises(ProviderError, match="invalid_family_response"):
            send_family_request(request(), 3, 100)


def test_pure_request_admission_runs_without_child_and_catches_bad_wire_fields(monkeypatch):
    forbid_child(monkeypatch)
    transport.validate_family_request(request())
    for invalid in (
        replace(request(), headers={"Authorization": "fixture\x00key"}),
        replace(request(), url="https://unofficial.invalid/fixture"),
        replace(request(), content_type="application/json\r\nInjected: yes"),
        replace(request(), body="not-bytes"),
    ):
        with pytest.raises(ProviderError, match="invalid_family_transport"):
            transport.validate_family_request(invalid)
