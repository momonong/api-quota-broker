"""One bounded buffered POST for a registered family; content remains in pipes.

The child only performs HTTP. Its parent owns the total deadline, including
request upload and body reads, and kills it on timeout. No retries or redirects.
"""

import base64
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .gateway_providers import ProviderError, ProviderPhaseTimeout
from .provider_policy import ENDPOINT_HOSTS

_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_HEADER_NAME = re.compile(_TOKEN)
_MEDIA_TYPE = re.compile(_TOKEN + "/" + _TOKEN + r"(?:; *" + _TOKEN + "=" + _TOKEN + ")*")
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "proxy-connection",
        "content-type",
        "accept",
    }
)


def _clean_text(value: object, *, maximum: int = 16_384, empty: bool = False) -> bool:
    if (
        not isinstance(value, str)
        or (not value and not empty)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    try:
        value.encode("latin-1")
    except UnicodeError:
        return False
    return True


def _headers_valid(value: object, *, request: bool = False) -> bool:
    if not isinstance(value, dict) or len(value) > 100:
        return False
    seen: set[str] = set()
    for name, text in value.items():
        if (
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or not _clean_text(text, empty=True)
            or name.lower() in seen
            or (request and name.lower() in _RESERVED_HEADERS)
        ):
            return False
        seen.add(name.lower())
    return True


@dataclass(frozen=True)
class FamilyRequest:
    url: str
    headers: dict[str, str]
    body: bytes
    content_type: str
    # An in-memory fixture view. Never persisted in status, usage, or diagnostics.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FamilyResponse:
    content: dict[str, Any] | None
    usage: dict[str, int] = field(default_factory=dict)
    request_id: str | None = None
    finish_reason: str | None = None
    truncated: bool | None = None


def json_request(url: str, headers: dict[str, str], payload: dict[str, Any]) -> FamilyRequest:
    try:
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ProviderError("invalid_family_json") from None
    if len(body) > _MAX_REQUEST_BYTES:
        raise ProviderError("invalid_family_json")
    return FamilyRequest(url, headers, body, "application/json", payload)


def form_request(
    url: str,
    headers: dict[str, str],
    fields: dict[str, str],
    *,
    file_field: str | None = None,
    file_bytes: bytes | None = None,
    file_mime: str | None = None,
    filename: str = "input.bin",
) -> FamilyRequest:
    if not isinstance(fields, dict) or any(
        not isinstance(name, str) or not _HEADER_NAME.fullmatch(name) or not isinstance(value, str)
        for name, value in fields.items()
    ):
        raise ProviderError("invalid_family_form")
    if file_field is None:
        if file_bytes is not None or file_mime is not None:
            raise ProviderError("invalid_family_file")
    elif (
        not isinstance(file_field, str)
        or not _HEADER_NAME.fullmatch(file_field)
        or not isinstance(file_bytes, bytes)
        or not _clean_text(filename, maximum=256)
        or any(c in filename for c in '\\"/')
        or not _clean_text(file_mime)
        or not isinstance(file_mime, str)
        or not _MEDIA_TYPE.fullmatch(file_mime)
    ):
        raise ProviderError("invalid_family_file")
    boundary = "broker-" + uuid.uuid4().hex
    body = bytearray()
    values = [*fields.keys(), *([file_field, filename, file_mime] if file_field else [])]
    if any(not isinstance(v, str) or any(c in v for c in '\r\n"') for v in values):
        raise ProviderError("invalid_family_form")
    for name, value in fields.items():
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise ProviderError("invalid_family_form") from None
        if len(body) + len(encoded) > _MAX_REQUEST_BYTES:
            raise ProviderError("invalid_family_form")
        body.extend(encoded)
        body.extend(b"\r\n")
    if file_field:
        if file_bytes is None or file_mime is None:
            raise ProviderError("invalid_family_file")
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {file_mime}\r\n\r\n'.encode()
        )
        if len(body) + len(file_bytes) > _MAX_REQUEST_BYTES:
            raise ProviderError("invalid_family_file")
        body.extend(file_bytes)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    if len(body) > _MAX_REQUEST_BYTES:
        raise ProviderError("invalid_family_form")
    return FamilyRequest(
        url, headers, bytes(body), "multipart/form-data; boundary=" + boundary, dict(fields)
    )


_HTTP_CHILD = r"""
import base64,json,sys,urllib.request,urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
try:
 config=json.loads(sys.stdin.buffer.read())
 body=base64.b64decode(config['body'],validate=True)
 headers={**config['headers'],'Content-Type':config['content_type'],'Accept':'*/*'}
 req=urllib.request.Request(config['url'],data=body,headers=headers,method='POST')
 opener=urllib.request.build_opener(NoRedirect())
 try:
  response=opener.open(req,timeout=config['timeout'])
 except urllib.error.HTTPError as exc:
  response=exc
 with response:
  header_items=list(response.headers.items())
  lengths=[value for name,value in header_items if name.lower()=='content-length']
  encodings=[value for name,value in header_items if name.lower()=='transfer-encoding']
  if len(lengths)>1 or len(encodings)>1 or (lengths and encodings): sys.exit(2)
  raw=response.read(config['response_bound']+1)
  if len(raw)>config['response_bound']: sys.exit(3)
  head=json.dumps({'status':response.code,'headers':dict(header_items)},separators=(',',':')).encode()
  if len(head)>16384: sys.exit(3)
  sys.stdout.buffer.write(head+b'\n'+raw)
except Exception:
 sys.exit(2)
"""


def validate_family_request(request: FamilyRequest) -> None:
    """Pure admission shared by Registry before dispatch and the actual transport.

    This validates the buffered wire request without credentials lookup, files,
    subprocesses, or network. It does not authorize an endpoint for a model;
    Registry additionally enforces its selected adapter's exact endpoint.
    """
    if (
        not isinstance(request, FamilyRequest)
        or not isinstance(request.url, str)
        or not request.url.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in request.url)
    ):
        raise ProviderError("invalid_family_transport")
    try:
        parsed = urlsplit(request.url)
    except ValueError:
        raise ProviderError("invalid_family_transport") from None
    approved = {host for hosts in ENDPOINT_HOSTS.values() for host in hosts}
    if (
        parsed.scheme != "https"
        or parsed.netloc != parsed.hostname
        or parsed.hostname not in approved
        or parsed.query
        or parsed.fragment
        or not isinstance(request.body, bytes)
        or len(request.body) > _MAX_REQUEST_BYTES
        or not _headers_valid(request.headers, request=True)
        or not _clean_text(request.content_type, maximum=1024)
        or not _MEDIA_TYPE.fullmatch(request.content_type)
    ):
        raise ProviderError("invalid_family_transport")


def send_family_request(
    request: FamilyRequest, timeout: float, response_bound: int
) -> tuple[int, dict[str, str], bytes]:
    validate_family_request(request)
    if (
        type(timeout) not in (float, int)
        or not 0 < timeout <= 180
        or type(response_bound) is not int
        or not 1 <= response_bound <= 128 * 1024 * 1024
    ):
        raise ProviderError("invalid_family_transport")
    config = json.dumps(
        {
            "url": request.url,
            "headers": request.headers,
            "content_type": request.content_type,
            "body": base64.b64encode(request.body).decode(),
            "timeout": timeout,
            "response_bound": response_bound,
        },
        separators=(",", ":"),
    ).encode()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _HTTP_CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        with process as child:
            try:
                output, _ = child.communicate(config, timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise ProviderPhaseTimeout("family_total_deadline") from None
            except OSError:
                child.kill()
                child.communicate()
                raise ProviderError("family_transport_failed") from None
            if child.returncode != 0:
                raise ProviderError(
                    "family_response_bound" if child.returncode == 3 else "family_transport_failed"
                )
    except OSError:
        raise ProviderError("family_transport_failed") from None
    header, delimiter, raw = output.partition(b"\n")
    if not delimiter or len(header) > 16_384 or len(raw) > response_bound:
        raise ProviderError("invalid_family_response")
    try:
        received = json.loads(header, object_pairs_hook=_unique_object)
        if not isinstance(received, dict) or set(received) != {"status", "headers"}:
            raise ValueError
        status, response_headers = received["status"], received["headers"]
        if (
            type(status) is not int
            or not 100 <= status <= 599
            or not _headers_valid(response_headers)
        ):
            raise ValueError
        lengths = [
            value for name, value in response_headers.items() if name.lower() == "content-length"
        ]
        if lengths and (
            not re.fullmatch(r"[0-9]{1,12}", lengths[0]) or int(lengths[0]) != len(raw)
        ):
            raise ValueError
        if lengths and any(name.lower() == "transfer-encoding" for name in response_headers):
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ProviderError("invalid_family_response") from None
    return status, response_headers, raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result
