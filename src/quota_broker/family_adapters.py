"""Packaged pure official protocol profiles; no transport, files or secret IO.

Profiles are selected by an administrator, never guessed from model names.
Workers AI profiles describe one documented schema, not every model in a family.
Unsupported fields, remote output URLs, streaming and unknown schemas fail closed.
Sources: console.groq.com/docs/api-reference; docs.mistral.ai/api/endpoint/;
ai.google.dev/api/generate-content; developers.cloudflare.com/workers-ai/models/;
openrouter.ai/docs/guides/overview/multimodal/; ocr.space/ocrapi.
"""

import base64
import binascii
import copy
import hashlib
import io
import json
import math
import re
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .families import FamilyError
from .family_transport import FamilyRequest, FamilyResponse, form_request, json_request
from .gateway_providers import ProviderError
from .provider_policy import catalog_model_id_reason

if TYPE_CHECKING:
    from .registry import ModelSpec

MAX_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024
CF_PATH = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
GOOGLE_GENERATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GOOGLE_EMBED = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
CHAT_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
EMBED_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1/embeddings",
    "mistral": "https://api.mistral.ai/v1/embeddings",
    "openrouter": "https://openrouter.ai/api/v1/embeddings",
}
CHAT_OPTIONS = frozenset(
    {
        "temperature",
        "top_p",
        "seed",
        "stop",
        "response_format",
        "json_schema",
        "tools",
        "tool_choice",
        "reasoning_effort",
        "stream",
    }
)
TEXT_FEATURES = frozenset(
    {"text", "vision", "json_output", "structured_output", "tool_calling", "reasoning"}
)
Serializer = Callable[["ModelSpec", str, dict[str, str], dict[str, Any]], FamilyRequest]
Parser = Callable[["ModelSpec", str, dict[str, str], bytes], FamilyResponse]
Admission = Callable[["ModelSpec", dict[str, Any]], None]


def _bad() -> ProviderError:
    return ProviderError("invalid_family_response")


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad()
    return value


def _list(value: Any, maximum: int = 64) -> list[Any]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise _bad()
    return value


def _text(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise _bad()
    return value


def _identifier(value: Any) -> str:
    if catalog_model_id_reason(value) is not None:
        raise FamilyError()
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _bad()
        result[key] = value
    return result


def _json(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise _bad()
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _bad() from None
    return _dict(result)


def _invalid_constant(value: str) -> Any:
    raise _bad()


def _number(value: Any) -> float:
    if type(value) not in {int, float}:
        raise _bad()
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise _bad() from None
    if not math.isfinite(result):
        raise _bad()
    return result


def _usage(value: Any) -> dict[str, int]:
    """Only explicit provider counters; no output sizes or local duration estimates."""
    if value is None:
        return {}
    usage = _dict(value)
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": ("output_tokens", "completion_tokens", "candidatesTokenCount"),
        "total_tokens": ("total_tokens", "totalTokenCount"),
        "audio_seconds": ("audio_seconds", "prompt_audio_seconds"),
        "pages": ("pages", "pages_processed"),
        "images": ("images",),
        "neurons": ("neurons",),
        "conversions": ("conversions",),
    }
    result = {}
    for metric, names in aliases.items():
        reported = [usage[name] for name in names if name in usage and usage[name] is not None]
        if not reported:
            continue
        if metric == "audio_seconds" and any(type(number) is float for number in reported):
            if any(
                type(number) not in {int, float} or not math.isfinite(number) or number < 0
                for number in reported
            ):
                raise _bad()
            continue  # Integer quota units cannot represent a fractional actual duration.
        if any(type(number) is not int or not 0 <= number <= 10**12 for number in reported):
            raise _bad()
        if len(set(reported)) != 1:
            raise _bad()
        result[metric] = reported[0]
    return result


def _token_usage(value: Any) -> dict[str, int]:
    usage = _usage(value)
    if all(name in usage for name in ("input_tokens", "output_tokens", "total_tokens")) and (
        usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]
    ):
        raise _bad()
    return usage


def _request_id(headers: dict[str, str], value: Any = None) -> str | None:
    candidates = [value] + [
        v for k, v in headers.items() if k.lower() in {"x-request-id", "request-id"}
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if (
            isinstance(candidate, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", candidate)
            and not re.search(r"(?:gsk_|sk-|dp\.st\.|bearer)", candidate, re.IGNORECASE)
        ):
            return candidate
    return None


def _finish(value: Any) -> tuple[str | None, bool | None]:
    known = {
        "stop",
        "length",
        "tool_calls",
        "content_filter",
        "STOP",
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
    }
    if not isinstance(value, str) or value not in known:
        return None, None
    return value.lower(), value in {"length", "MAX_TOKENS"}


def _decode(data: Any, maximum: int = MAX_BYTES) -> bytes:
    if not isinstance(data, str) or not data or len(data) > 4 * ((maximum + 2) // 3) + 4:
        raise _bad()
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise _bad() from None
    if not 0 < len(raw) <= maximum or base64.b64encode(raw).decode() != data:
        raise _bad()
    return raw


def _sniff(raw: bytes, kind: str) -> str:
    signatures = {
        "image": (
            ("image/png", raw.startswith(b"\x89PNG\r\n\x1a\n")),
            ("image/jpeg", raw.startswith(b"\xff\xd8\xff")),
            ("image/webp", raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"),
        ),
        "audio": (
            ("audio/wav", raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"),
            (
                "audio/mpeg",
                raw.startswith(b"ID3") or (len(raw) >= 4 and raw[0] == 255 and raw[1] & 224 == 224),
            ),
            ("audio/ogg", raw.startswith(b"OggS")),
            ("audio/flac", raw.startswith(b"fLaC")),
        ),
    }
    for mime, matched in signatures[kind]:
        if matched:
            return mime
    raise _bad()


def _media(kind: str, data: Any, mime: Any = None) -> dict[str, str]:
    raw = _decode(data)
    detected = _sniff(raw, kind)
    if mime is not None and mime != detected:
        raise _bad()
    return {"type": kind, "mime_type": detected, "data": data}


def _inline_uri(part: dict[str, Any]) -> str:
    _decode(part.get("data"), MAX_INPUT_BYTES)
    mime = part.get("mime_type")
    if not isinstance(mime, str) or not re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", mime):
        raise FamilyError()
    return f"data:{mime};base64,{part['data']}"


def _from_uri(value: Any, kind: str = "image") -> dict[str, str]:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise _bad()  # Never fetch or return provider-controlled remote URLs.
    match = re.fullmatch(r"data:([a-z]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", value)
    if match is None:
        raise _bad()
    return _media(kind, match[2], match[1])


def _input(data: dict[str, Any], key: str) -> Any:
    value = data.get("input")
    if not isinstance(value, dict) or key not in value:
        raise FamilyError()
    return value[key]


def _plain_message(data: dict[str, Any]) -> str:
    messages = _input(data, "messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise FamilyError("unsupported option")
    message = messages[0]
    if (
        not isinstance(message, dict)
        or set(message) != {"role", "content"}
        or message["role"] != "user"
    ):
        raise FamilyError("unsupported option")
    if not isinstance(message["content"], str) or not message["content"]:
        raise FamilyError("unsupported option")
    return message["content"]


def _options(data: dict[str, Any]) -> dict[str, Any]:
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise FamilyError()
    return options


@dataclass(frozen=True)
class FamilyAdapter:
    """Mapping-based dispatch; adding a profile does not edit a central switch."""

    endpoints: Mapping[str, str]
    serializers: Mapping[str, Serializer]
    parsers: Mapping[str, Parser]
    supported_options_by_family: Mapping[str, frozenset[str]]
    supported_features: frozenset[str]
    admission: Admission | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "supported_features", self.supported_features | self.supported_capabilities
        )

    def uses_output_tokens(self, capability: str) -> bool:
        return capability in {"text_generation", "vision", "code_completion"}

    @property
    def supported_capabilities(self) -> frozenset[str]:
        return frozenset(self.serializers)

    def allowed_origins(self, provider: str) -> set[str]:
        endpoint = self.endpoints.get(provider)
        if endpoint is None:
            return set()
        parsed = urlsplit(endpoint)
        return {f"https://{parsed.netloc}"}

    def endpoint_for(self, spec: "ModelSpec", capability: str, account_id: str) -> str:
        template = self.endpoints.get(spec.provider)
        if template is None or capability not in self.supported_capabilities:
            raise FamilyError("unsupported family")
        model = _identifier(spec.model)
        if spec.origin not in self.allowed_origins(spec.provider):
            raise FamilyError()
        if "{account_id}" in template:
            if not isinstance(account_id, str) or (
                account_id != "{account_id}" and not re.fullmatch(r"[A-Fa-f0-9]{32}", account_id)
            ):
                raise FamilyError()
            if not re.fullmatch(
                r"@cf/[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*", model
            ):
                raise FamilyError()
        if "generativelanguage.googleapis.com" in template and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model
        ):
            raise FamilyError()
        return template.replace("{model}", model).replace("{account_id}", account_id)

    def admit_task(self, spec: "ModelSpec", data: dict[str, Any]) -> None:
        capability = data.get("capability")
        declared = spec.capabilities or (spec.capability,)
        if capability not in declared or capability not in self.supported_capabilities:
            raise FamilyError("unsupported family")
        maximum = data.get("max_output_tokens")
        if type(maximum) is not int or (
            not 1 <= maximum <= spec.max_output_tokens
            if self.uses_output_tokens(capability)
            else maximum != 0
        ):
            raise FamilyError()
        options = _options(data)
        if options.get("stream") is True or "realtime" in options:
            raise FamilyError("streaming and realtime are unsupported")
        if "stream" in options and options["stream"] is not False:
            raise FamilyError("unsupported option")
        if set(options) - self.supported_options_by_family[capability]:
            raise FamilyError("unsupported option")
        if not isinstance(data.get("input"), dict):
            raise FamilyError()
        required = set(data.get("requirements", {}).get("features", []))
        required.update(data.get("required_features", ()))
        if not required <= set(spec.features) or not required <= self.supported_features:
            raise FamilyError("unsupported option")
        self.endpoint_for(spec, capability, "{account_id}")
        if self.admission is not None:
            self.admission(spec, data)

    def request_task(
        self, spec: "ModelSpec", account_id: str, secret: str, data: dict[str, Any]
    ) -> FamilyRequest:
        self.admit_task(spec, data)
        url = self.endpoint_for(spec, data["capability"], account_id)
        if not isinstance(secret, str) or not secret or any(c in secret for c in "\r\n"):
            raise FamilyError()
        headers = (
            {}
            if self.auth_scheme == "form"
            else {
                self.auth_header: ("Bearer " + secret) if self.auth_scheme == "Bearer" else secret,
            }
        )
        # Form profiles receive their key only as an in-memory private field.
        payload = copy.deepcopy(data)
        if self.auth_scheme == "form":
            payload["_form_key"] = secret
        try:
            return self.serializers[data["capability"]](spec, url, headers, payload)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, FamilyError):
                raise
            raise FamilyError() from None

    def interpret_task(
        self, spec: "ModelSpec", capability: str, status: int, headers: dict[str, str], raw: bytes
    ) -> FamilyResponse:
        if capability not in self.supported_capabilities or capability not in (
            spec.capabilities or (spec.capability,)
        ):
            raise FamilyError("unsupported family")
        if type(status) is not int or not 100 <= status <= 599:
            raise _bad()
        if not 200 <= status < 300:
            return FamilyResponse(None, request_id=_request_id(headers))
        try:
            return self.parsers[capability](spec, capability, headers, raw)
        except (
            KeyError,
            TypeError,
            ValueError,
            IndexError,
            OverflowError,
            AttributeError,
            RecursionError,
        ) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise _bad() from None


def _openai_messages(spec: "ModelSpec", messages: Any) -> list[dict[str, Any]]:
    result = copy.deepcopy(_list(messages))
    allowed_media = {
        "nvidia": {"image"},
        "groq": {"image"},
        "mistral": {"image"},
        "openrouter": {"image", "audio", "document"},
    }
    audio_formats = {
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/mp4": "m4a",
    }
    for message in result:
        content = message["content"]
        if isinstance(content, str):
            continue
        converted = []
        for part in _list(content):
            kind = part.get("type")
            if kind == "text":
                converted.append({"type": "text", "text": part["text"]})
            elif kind in allowed_media[spec.provider]:
                if kind == "image":
                    converted.append({"type": "image_url", "image_url": {"url": _inline_uri(part)}})
                elif kind == "audio":
                    _decode(part["data"], MAX_INPUT_BYTES)
                    converted.append(
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": part["data"],
                                "format": audio_formats[part["mime_type"]],
                            },
                        }
                    )
                else:
                    converted.append(
                        {
                            "type": "file",
                            "file": {"filename": "input.pdf", "file_data": _inline_uri(part)},
                        }
                    )
            else:
                raise FamilyError("unsupported option")
        message["content"] = converted
    return result


def _chat_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    options = _options(data)
    if "json_schema" in options and "response_format" in options:
        raise FamilyError("unsupported option")
    for message in _input(data, "messages"):
        if (
            "provider_state" in message
            or any(
                "thought_signature" in call or "provider_call_id" in call
                for call in message.get("tool_calls", [])
            )
            or isinstance(message.get("content"), list)
            and any("thought_signature" in part for part in message["content"])
        ):
            raise FamilyError("unsupported option")
    _openai_messages(spec, _input(data, "messages"))


def _chat(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    options = copy.deepcopy(_options(data))
    if spec.provider == "mistral" and "seed" in options:
        options["random_seed"] = options.pop("seed")
    output_key = {"groq": "max_completion_tokens"}.get(spec.provider, "max_tokens")
    payload = {
        "model": spec.model,
        "messages": _openai_messages(spec, _input(data, "messages")),
        output_key: data["max_output_tokens"],
        "stream": False,
    }
    schema = options.pop("json_schema", None)
    response_format = options.pop("response_format", None)
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "broker_response", "schema": schema, "strict": True},
        }
    elif response_format is not None:
        payload["response_format"] = {"type": response_format}
    choice = options.get("tool_choice")
    if choice is not None and choice not in {"auto", "none", "required"}:
        options["tool_choice"] = {"type": "function", "function": {"name": choice}}
    payload.update(options)
    return json_request(url, headers, payload)


def _chat_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    choices = _list(body.get("choices"), 1)
    choice = _dict(choices[0])
    message = _dict(choice.get("message"))
    if message.get("role", "assistant") != "assistant":
        raise _bad()
    content = message.get("content")
    calls = message.get("tool_calls")
    normalized: dict[str, Any] = {"role": "assistant", "content": ""}
    if isinstance(content, str):
        normalized["content"] = content
    elif isinstance(content, list):
        parts = []
        for part in _list(content):
            if part.get("type") == "text":
                parts.append({"type": "text", "text": _text(part.get("text"), empty=True)})
            elif part.get("type") == "image_url":
                parts.append(_from_uri(_dict(part.get("image_url")).get("url")))
            else:
                raise _bad()
        normalized["content"] = parts
    elif content is not None:
        raise _bad()
    result: dict[str, Any] = {"messages": [normalized]}
    if calls:
        normalized["tool_calls"] = copy.deepcopy(_list(calls))
        result["tool_calls"] = copy.deepcopy(calls)
    if isinstance(content, str) and content:
        result["text"] = content
    if not content and not calls:
        raise _bad()
    finish, truncated = _finish(choice.get("finish_reason"))
    return FamilyResponse(
        result,
        _token_usage(body.get("usage")),
        _request_id(headers, body.get("id")),
        finish,
        truncated,
    )


def _embed(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    payload = {"model": spec.model, "input": _input(data, "texts"), "encoding_format": "float"}
    payload.update(_options(data))
    return json_request(url, headers, payload)


def _mistral_embed(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    payload = {"model": spec.model, "input": _input(data, "texts"), "encoding_format": "float"}
    if "dimensions" in _options(data):
        payload["output_dimension"] = _options(data)["dimensions"]
    return json_request(url, headers, payload)


def _nvidia_embed_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if _options(data).get("input_type") not in {"query", "passage"}:
        raise FamilyError("unsupported option")


def _nvidia_embed(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(
        url,
        headers,
        {
            "model": spec.model,
            "input": _input(data, "texts"),
            "input_type": _options(data)["input_type"],
            "encoding_format": "float",
            "truncate": "NONE",
        },
    )


def _embedding_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    vectors: dict[int, Any] = {}
    for item in _list(body.get("data")):
        record = _dict(item)
        index = record.get("index")
        if type(index) is not int or not 0 <= index < 64 or index in vectors:
            raise _bad()
        vectors[index] = [_number(v) for v in _list(record.get("embedding"), 65536)]
    if set(vectors) != set(range(len(vectors))):
        raise _bad()
    return FamilyResponse(
        {"vectors": [vectors[i] for i in range(len(vectors))]},
        _token_usage(body.get("usage")),
        _request_id(headers, body.get("id")),
    )


def _rerank(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(
        url,
        headers,
        {
            "model": spec.model,
            "query": {"text": _input(data, "query")},
            "passages": [{"text": text} for text in _input(data, "documents")],
            "truncate": "NONE",
        },
    )


def _rerank_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    scores = [
        {"index": item["index"], "score": _number(item["logit"])}
        for item in _list(body.get("rankings"))
    ]
    return FamilyResponse(
        {"scores": scores}, _usage(body.get("usage")), _request_id(headers, body.get("id"))
    )


def _fim_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    _plain_message(data)


def _fim(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    options = copy.deepcopy(_options(data))
    if "seed" in options:
        options["random_seed"] = options.pop("seed")
    return json_request(
        url,
        headers,
        {
            "model": spec.model,
            "prompt": _plain_message(data),
            "max_tokens": data["max_output_tokens"],
            "stream": False,
            **options,
        },
    )


def _mistral_ocr(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    part = _input(data, "document")
    field = "document_url" if part["type"] == "document" else "image_url"
    return json_request(
        url,
        headers,
        {
            "model": spec.model,
            "document": {"type": field, field: _inline_uri(part)},
            "include_image_base64": False,
        },
    )


def _mistral_ocr_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    pages = []
    for page in _list(body.get("pages")):
        index = page.get("index")
        if type(index) is not int or index < 0:
            raise _bad()
        pages.append({"page": index + 1, "text": _text(page.get("markdown"), empty=True)})
    return FamilyResponse(
        {"pages": pages}, _usage(body.get("usage_info")), _request_id(headers, body.get("id"))
    )


def _audio_form(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    audio = _input(data, "audio")
    options = copy.deepcopy(_options(data))
    options.pop("target_language", None)  # English-only admission checks it first.
    fields = {
        "model": spec.model,
        "response_format": "verbose_json",
        **{k: str(v) for k, v in options.items()},
    }
    return form_request(
        url,
        headers,
        fields,
        file_field="file",
        file_bytes=_decode(audio["data"], MAX_INPUT_BYTES),
        file_mime=audio["mime_type"],
        filename="input.audio",
    )


def _mistral_audio_form(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    audio = _input(data, "audio")
    fields = {"model": spec.model, **{k: str(v) for k, v in _options(data).items()}}
    return form_request(
        url,
        headers,
        fields,
        file_field="file",
        file_bytes=_decode(audio["data"], MAX_INPUT_BYTES),
        file_mime=audio["mime_type"],
        filename="input.audio",
    )


def _english_translation(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if _options(data).get("target_language", "en").lower() != "en":
        raise FamilyError("unsupported option")


def _asr_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    content = {"text": _text(body.get("text"), empty=True)}
    if body.get("language") is not None:
        content["language"] = _text(body["language"])
    return FamilyResponse(content, _usage(body.get("usage")), _request_id(headers, body.get("id")))


def _groq_tts_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    options = _options(data)
    if not options.get("voice") or options.get("format", "wav") != "wav":
        raise FamilyError("unsupported option")


def _groq_tts(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(
        url,
        headers,
        {
            "model": spec.model,
            "input": _input(data, "text"),
            "voice": _options(data)["voice"],
            "response_format": "wav",
        },
    )


def _audio_binary(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise _bad()
    return FamilyResponse(
        {"audio": _media("audio", base64.b64encode(raw).decode())}, request_id=_request_id(headers)
    )


def _mistral_tts_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if _options(data).get("format", "wav") not in {"wav", "mp3", "flac", "opus"}:
        raise FamilyError("unsupported option")


def _mistral_tts(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    options = _options(data)
    payload = {
        "model": spec.model,
        "input": _input(data, "text"),
        "response_format": options.get("format", "wav"),
        "stream": False,
    }
    if "voice" in options:
        payload["voice_id"] = options["voice"]
    return json_request(url, headers, payload)


def _mistral_tts_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    return FamilyResponse(
        {"audio": _media("audio", body.get("audio_data"))},
        _usage(body.get("usage")),
        _request_id(headers, body.get("id")),
    )


def _classify(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(url, headers, {"model": spec.model, "input": _input(data, "texts")})


def _moderation_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    results = []
    for record in _list(body.get("results")):
        categories = _dict(record.get("categories"))
        scores = _dict(record.get("category_scores"))
        if (
            not categories
            or set(categories) != set(scores)
            or any(type(v) is not bool for v in categories.values())
        ):
            raise _bad()
        results.append(
            {
                "flagged": any(categories.values()),
                "categories": categories,
                "scores": {k: _number(v) for k, v in scores.items()},
            }
        )
    return FamilyResponse(
        {"results": results}, _usage(body.get("usage")), _request_id(headers, body.get("id"))
    )


def _classification_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    # Official OpenAPI ClassificationTargetResult.scores is map<string, number>;
    # no probability range or normalization is declared.
    body = _json(raw)
    _text(body.get("id"))
    _text(body.get("model"))
    classes = []
    for record in _list(body.get("results")):
        labels = []
        for target, result in _dict(record).items():
            _text(target)
            for label, score in _dict(_dict(result).get("scores")).items():
                labels.append({"target": target, "label": _text(label), "score": _number(score)})
        if not labels:
            raise _bad()
        classes.append(labels)
    return FamilyResponse(
        {"classes": classes}, _usage(body.get("usage")), _request_id(headers, body.get("id"))
    )


def _checked_google_parts(value: Any) -> list[dict[str, Any]]:
    parts = copy.deepcopy(_list(value))
    for part in parts:
        part = _dict(part)
        fields = set(part) & {"text", "functionCall", "inlineData"}
        if len(fields) != 1 or set(part) - fields - {"thoughtSignature", "thought"}:
            raise _bad()
        if "thoughtSignature" in part:
            _text(part["thoughtSignature"])
        if "thought" in part and type(part["thought"]) is not bool:
            raise _bad()
        if "text" in part:
            _text(part["text"], empty=True)
        elif "functionCall" in part:
            call = _dict(part["functionCall"])
            if set(call) - {"name", "id", "args"}:
                raise _bad()
            name = _text(call.get("name"))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", name):
                raise _bad()
            if "id" in call:
                _text(call["id"])
            if "args" in call:
                _dict(call["args"])
        else:
            media = _dict(part["inlineData"])
            if set(media) != {"mimeType", "data"}:
                raise _bad()
            _text(media["mimeType"])
            _decode(media["data"])
    return parts


def _google_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    result = []
    for part in _list(content):
        if part.get("type") == "text":
            wire = {"text": part["text"]}
        elif part.get("type") in {"image", "audio", "document"}:
            _decode(part["data"], MAX_INPUT_BYTES)
            wire = {"inlineData": {"mimeType": part["mime_type"], "data": part["data"]}}
        else:
            raise FamilyError("unsupported option")
        if "thought_signature" in part:
            wire["thoughtSignature"] = part["thought_signature"]
        result.append(wire)
    return result


def _google_messages(
    spec: "ModelSpec", data: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    contents, system = [], []
    calls_by_id: dict[str, tuple[str, str | None]] = {}
    for message in _list(_input(data, "messages")):
        if message.get("name") is not None:
            raise FamilyError("unsupported option")
        parts = _google_parts(message["content"])
        role = message["role"]
        state = message.get("provider_state")
        calls = message.get("tool_calls", [])
        if state is not None:
            if (
                role != "assistant"
                or not isinstance(state, dict)
                or set(state) != {"provider", "model", "parts"}
                or state["provider"] != "google"
                or state["model"] != spec.model
            ):
                raise FamilyError("unsupported option")
            try:
                original_parts = _checked_google_parts(state["parts"])
                wire_calls = [part for part in original_parts if "functionCall" in part]
                visible_parts = [
                    part
                    for part in original_parts
                    if "functionCall" not in part and part.get("thought") is not True
                ]
                if isinstance(message["content"], str):
                    if any("text" not in part for part in visible_parts) or message[
                        "content"
                    ] != "".join(part["text"] for part in visible_parts):
                        raise FamilyError("unsupported option")
                elif parts != [
                    {k: v for k, v in part.items() if k != "thought"} for part in visible_parts
                ]:
                    raise FamilyError("unsupported option")
                if len(calls) != len(wire_calls):
                    raise FamilyError("unsupported option")
                for call, part in zip(calls, wire_calls, strict=True):
                    wire_call = part["functionCall"]
                    if (
                        call["function"]["name"] != wire_call["name"]
                        or json.loads(call["function"]["arguments"]) != wire_call.get("args", {})
                        or call.get("provider_call_id", call["id"]) != wire_call.get("id")
                        or call.get("thought_signature") != part.get("thoughtSignature")
                    ):
                        raise FamilyError("unsupported option")
            except (ProviderError, KeyError, TypeError, ValueError):
                raise FamilyError("unsupported option") from None
            parts = original_parts
        if role == "system":
            if calls or message.get("tool_call_id") or state:
                raise FamilyError("unsupported option")
            system.extend(parts)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in calls_by_id:
                raise FamilyError("unsupported option")
            name, provider_id = calls_by_id.pop(call_id)
            try:
                response = json.loads(message["content"], parse_constant=_invalid_constant)
                json.dumps(response, allow_nan=False)
            except (ValueError, TypeError, ProviderError, RecursionError):
                raise FamilyError("unsupported option") from None
            if not isinstance(response, dict):
                raise FamilyError("unsupported option")
            wire_response = {"name": name, "response": response}
            if provider_id is not None:
                wire_response["id"] = provider_id
            parts = [{"functionResponse": wire_response}]
        for call in calls:
            if call["id"] in calls_by_id:
                raise FamilyError("unsupported option")
            provider_id = call.get("provider_call_id", call["id"])
            calls_by_id[call["id"]] = (call["function"]["name"], provider_id)
            if state is None:
                wire_call = {
                    "name": call["function"]["name"],
                    "args": json.loads(call["function"]["arguments"]),
                }
                if provider_id is not None:
                    wire_call["id"] = provider_id
                wire_part = {"functionCall": wire_call}
                if "thought_signature" in call:
                    wire_part["thoughtSignature"] = call["thought_signature"]
                parts.append(wire_part)
        contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    if not contents:
        raise FamilyError()
    return contents, {"parts": system} if system else None


def _google_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    options = _options(data)
    if "json_schema" in options and "response_format" in options:
        raise FamilyError("unsupported option")
    if options.get("reasoning_effort") == "none" or any(
        "strict" in tool["function"] for tool in options.get("tools", [])
    ):
        raise FamilyError("unsupported option")
    if data["capability"] in {"text_generation", "vision"}:
        _google_messages(spec, data)
    if data["capability"] == "tts" and options.get("format", "wav") != "wav":
        raise FamilyError("unsupported option")
    if data["capability"] == "image_generation" and (
        options.get("n", 1) != 1 or options.get("format", "png") != "png"
    ):
        raise FamilyError("unsupported option")


def _google(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    contents, system = _google_messages(spec, data)
    options = copy.deepcopy(_options(data))
    config: dict[str, Any] = {"maxOutputTokens": data["max_output_tokens"]}
    mappings = {
        "temperature": "temperature",
        "top_p": "topP",
        "seed": "seed",
        "stop": "stopSequences",
    }
    for key, destination in mappings.items():
        if key in options:
            config[destination] = options[key]
    if "json_schema" in options:
        config.update(
            {"responseMimeType": "application/json", "responseJsonSchema": options["json_schema"]}
        )
    elif "response_format" in options:
        config["responseMimeType"] = {"text": "text/plain", "json_object": "application/json"}[
            options["response_format"]
        ]
    if "reasoning_effort" in options:
        config["thinkingConfig"] = {"thinkingLevel": options["reasoning_effort"]}
    payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
    if system:
        payload["systemInstruction"] = system
    if "tools" in options:
        declarations = []
        for tool in options["tools"]:
            function = tool["function"]
            if "strict" in function:
                raise FamilyError("unsupported option")
            declarations.append(
                {
                    "name": function["name"],
                    "parametersJsonSchema": function["parameters"],
                    **(
                        {"description": function["description"]}
                        if "description" in function
                        else {}
                    ),
                }
            )
        payload["tools"] = [{"functionDeclarations": declarations}]
    if "tool_choice" in options:
        choice = options["tool_choice"]
        modes = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
        config_choice: dict[str, Any] = {"mode": modes.get(choice, "ANY")}
        if choice not in modes:
            config_choice["allowedFunctionNames"] = [choice]
        payload["toolConfig"] = {"functionCallingConfig": config_choice}
    return json_request(url, headers, payload)


def _google_asr(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    audio = _input(data, "audio")
    _decode(audio["data"], MAX_INPUT_BYTES)
    options = _options(data)
    instruction = "Transcribe the spoken audio verbatim. Return only the transcription."
    if "language" in options:
        instruction += " The spoken language is " + options["language"] + "."
    if "prompt" in options:
        instruction += " Context: " + options["prompt"]
    return json_request(
        url,
        headers,
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": instruction},
                        {"inlineData": {"mimeType": audio["mime_type"], "data": audio["data"]}},
                    ],
                }
            ],
        },
    )


def _google_image(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(
        url,
        headers,
        {
            "contents": [{"role": "user", "parts": [{"text": _input(data, "prompt")}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        },
    )


def _google_tts(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    config: dict[str, Any] = {"responseModalities": ["AUDIO"]}
    if "voice" in _options(data):
        config["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": _options(data)["voice"]}}
        }
    return json_request(
        url,
        headers,
        {
            "contents": [{"role": "user", "parts": [{"text": _input(data, "text")}]}],
            "generationConfig": config,
        },
    )


def _pcm_wav(data: str, mime: str) -> dict[str, str]:
    # Gemini TTS documents mono, signed 16-bit little-endian PCM at 24 kHz.
    if mime not in {
        "audio/L16;codec=pcm;rate=24000",
        "audio/L16;rate=24000",
        "audio/pcm;rate=24000",
    }:
        raise _bad()
    raw = _decode(data)
    if len(raw) % 2 or len(raw) + 44 > MAX_BYTES:
        raise _bad()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(raw)
    return _media("audio", base64.b64encode(buffer.getvalue()).decode())


def _google_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    candidate = _dict(_list(body.get("candidates"), 1)[0])
    texts, images, audio, calls = [], [], [], []
    original_parts = _checked_google_parts(_dict(candidate.get("content")).get("parts"))
    for part_index, part in enumerate(original_parts):
        if "text" in part:
            if part.get("thought") is not True:
                texts.append(_text(part["text"], empty=True))
        elif "inlineData" in part:
            media = _dict(part["inlineData"])
            mime = _text(media.get("mimeType"))
            if mime.startswith("image/"):
                images.append(_media("image", media.get("data"), mime))
            elif mime.startswith("audio/"):
                audio.append(
                    _pcm_wav(media["data"], mime)
                    if "rate=24000" in mime
                    else _media("audio", media.get("data"), mime)
                )
            else:
                raise _bad()
        elif "functionCall" in part:
            call = _dict(part["functionCall"])
            provider_id = call.get("id")
            identity = json.dumps(
                body, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            typed_id = (
                provider_id
                or "gemini-call-"
                + hashlib.sha256(identity + str(part_index).encode()).hexdigest()[:32]
            )
            normalized_call = {
                "id": typed_id,
                "provider_call_id": provider_id,
                "type": "function",
                "function": {
                    "name": _text(call.get("name")),
                    "arguments": json.dumps(
                        _dict(call.get("args", {})), separators=(",", ":"), allow_nan=False
                    ),
                },
            }
            if "thoughtSignature" in part:
                normalized_call["thought_signature"] = part["thoughtSignature"]
            calls.append(normalized_call)
        else:
            raise _bad()
    content: dict[str, Any]
    if capability == "image_generation":
        if audio or calls or not images:
            raise _bad()
        content = {"images": images}
    elif capability == "tts":
        if images or calls or len(audio) != 1:
            raise _bad()
        content = {"audio": audio[0]}
    else:
        conversational = capability in {"text_generation", "vision"}
        if (
            not texts
            and not calls
            and not audio
            and not images
            or (not conversational and (audio or images))
        ):
            raise _bad()
        content = {"text": "".join(texts)} if texts or calls else {}
        if calls:
            content["tool_calls"] = calls
        if conversational:
            visible: str | list[dict[str, Any]] = "".join(texts)
            if images or audio:
                visible = []
                for part in original_parts:
                    if "functionCall" in part or part.get("thought") is True:
                        continue
                    if "text" in part:
                        item = {"type": "text", "text": part["text"]}
                    else:
                        wire_media = part["inlineData"]
                        kind = "image" if wire_media["mimeType"].startswith("image/") else "audio"
                        item = _media(kind, wire_media["data"], wire_media["mimeType"])
                    if "thoughtSignature" in part:
                        item["thought_signature"] = part["thoughtSignature"]
                    visible.append(item)
            message: dict[str, Any] = {
                "role": "assistant",
                "content": visible,
                "provider_state": {
                    "provider": "google",
                    "model": spec.model,
                    "parts": original_parts,
                },
            }
            if calls:
                message["tool_calls"] = calls
            content["messages"] = [message]
    finish, truncated = _finish(candidate.get("finishReason"))
    return FamilyResponse(
        content,
        _usage(body.get("usageMetadata")),
        _request_id(headers, body.get("responseId")),
        finish,
        truncated,
    )


def _google_embed_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if len(_input(data, "texts")) != 1:
        raise FamilyError("unsupported option")  # One embedContent call, never hidden fanout.


def _google_embed(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    payload: dict[str, Any] = {
        "model": "models/" + spec.model,
        "content": {"parts": [{"text": _input(data, "texts")[0]}]},
    }
    if "dimensions" in _options(data):
        payload["outputDimensionality"] = _options(data)["dimensions"]
    return json_request(url, headers, payload)


def _google_embed_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    vector = [_number(v) for v in _list(_dict(body.get("embedding")).get("values"), 65536)]
    return FamilyResponse(
        {"vectors": [vector]}, _usage(body.get("usageMetadata")), _request_id(headers)
    )


def _cf_body(raw: bytes) -> tuple[dict[str, Any], Any]:
    body = _json(raw)
    if body.get("success") is not True or body.get("errors"):
        raise _bad()
    return body, body.get("result")


def _cf_text(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    messages = _input(data, "messages")
    if any(
        not isinstance(message.get("content"), str) or set(message) - {"role", "content"}
        for message in messages
    ):
        raise FamilyError("unsupported option")
    options = copy.deepcopy(_options(data))
    if "response_format" in options:
        options["response_format"] = {"type": options["response_format"]}
    return json_request(
        url,
        headers,
        {"messages": messages, "max_tokens": data["max_output_tokens"], "stream": False, **options},
    )


def _cf_text_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    for message in _input(data, "messages"):
        if not isinstance(message.get("content"), str) or set(message) - {"role", "content"}:
            raise FamilyError("unsupported option")


def _cf_translation(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    options = _options(data)
    return json_request(
        url,
        headers,
        {
            "text": _input(data, "text"),
            "source_lang": options["source_language"],
            "target_lang": options["target_language"],
        },
    )


def _cf_translation_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body, result = _cf_body(raw)
    item = _dict(result)
    return FamilyResponse(
        {"text": _text(item.get("translated_text"))},
        _usage(item.get("usage")),
        _request_id(headers, body.get("request_id")),
    )


def _cf_text_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body, result = _cf_body(raw)
    item = _dict(result)
    return FamilyResponse(
        {"text": _text(item.get("response"))},
        _usage(item.get("usage")),
        _request_id(headers, body.get("request_id")),
    )


def _cf_embed(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(url, headers, {"text": _input(data, "texts")})


def _cf_embed_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body, result = _cf_body(raw)
    item = _dict(result)
    vectors = [[_number(v) for v in _list(vector, 65536)] for vector in _list(item.get("data"))]
    return FamilyResponse(
        {"vectors": vectors},
        _usage(item.get("usage")),
        _request_id(headers, body.get("request_id")),
    )


def _cf_asr(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    part = _input(data, "audio")
    _decode(part["data"], MAX_INPUT_BYTES)
    options = _options(data)
    payload = {
        "audio": part["data"],
        "task": "translate" if data["capability"] == "audio_translation" else "transcribe",
    }
    for key, destination in {"language": "language", "prompt": "initial_prompt"}.items():
        if key in options:
            payload[destination] = options[key]
    return json_request(url, headers, payload)


def _cf_asr_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body, result = _cf_body(raw)
    item = _dict(result)
    return FamilyResponse(
        {"text": _text(item.get("text"), empty=True)},
        _usage(item.get("usage")),
        _request_id(headers, body.get("request_id")),
    )


def _cf_tts_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if _options(data).get("format", "mp3") != "mp3":
        raise FamilyError("unsupported option")


def _cf_tts(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(url, headers, {"prompt": _input(data, "text")})


def _cf_image_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    options = _options(data)
    if options.get("n", 1) != 1 or options.get("format", "png") != "png":
        raise FamilyError("unsupported option")
    if "size" in options:
        size = options["size"].split("x")
        if len(size) != 2 or any(not v.isdigit() or not 256 <= int(v) <= 2048 for v in size):
            raise FamilyError("unsupported option")


def _cf_image(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    payload = {"prompt": _input(data, "prompt")}
    if "size" in _options(data):
        width, height = map(int, _options(data)["size"].split("x"))
        payload.update({"width": width, "height": height})
    return json_request(url, headers, payload)


def _image_binary(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise _bad()
    return FamilyResponse(
        {"images": [_media("image", base64.b64encode(raw).decode())]},
        request_id=_request_id(headers),
    )


def _cf_classify_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if len(_input(data, "texts")) != 1:
        raise FamilyError("unsupported option")


def _cf_classify(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    return json_request(url, headers, {"text": _input(data, "texts")[0]})


def _cf_classify_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body, result = _cf_body(raw)
    labels = [
        {"label": _text(item.get("label")), "score": _number(item.get("score"))}
        for item in _list(result)
    ]
    return FamilyResponse(
        {"classes": [labels]},
        _usage(body.get("usage")),
        _request_id(headers, body.get("request_id")),
    )


def _or_image(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    options = copy.deepcopy(_options(data))
    if "format" in options:
        options["output_format"] = options.pop("format")
    return json_request(
        url, headers, {"model": spec.model, "prompt": _input(data, "prompt"), **options}
    )


def _or_image_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if _options(data).get("n", 1) > 10:
        raise FamilyError("unsupported option")


def _or_image_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    images = [
        _media("image", item.get("b64_json"), item.get("media_type"))
        for item in _list(body.get("data"))
    ]
    return FamilyResponse(
        {"images": images}, _token_usage(body.get("usage")), _request_id(headers, body.get("id"))
    )


def _ocrspace_admit(spec: "ModelSpec", data: dict[str, Any]) -> None:
    if spec.model not in {"ocr.space/engine1", "ocr.space/engine2", "ocr.space/engine3"}:
        raise FamilyError("unsupported family")


def _ocrspace(
    spec: "ModelSpec", url: str, headers: dict[str, str], data: dict[str, Any]
) -> FamilyRequest:
    part = _input(data, "document")
    fields = {
        "apikey": data["_form_key"],
        "OCREngine": spec.model[-1],
        "isOverlayRequired": "false",
        "base64Image": _inline_uri(part),
    }
    options = _options(data)
    for key, destination in {
        "language": "language",
        "detect_orientation": "detectOrientation",
        "scale": "scale",
        "is_table": "isTable",
        "overlay": "isOverlayRequired",
    }.items():
        if key in options:
            value = options[key]
            fields[destination] = str(value).lower() if type(value) is bool else value
    return form_request(url, headers, fields)


def _ocrspace_result(
    spec: "ModelSpec", capability: str, headers: dict[str, str], raw: bytes
) -> FamilyResponse:
    body = _json(raw)
    if body.get("IsErroredOnProcessing") is not False or body.get("OCRExitCode") != 1:
        raise _bad()
    pages = []
    for record in _list(body.get("ParsedResults")):
        if record.get("FileParseExitCode") != 1 or record.get("ErrorMessage"):
            raise _bad()
        page: dict[str, Any] = {"text": _text(record.get("ParsedText"), empty=True)}
        if "TextOverlay" in record:
            page["overlay"] = _ocr_overlay(record["TextOverlay"])
        pages.append(page)
    return FamilyResponse({"pages": pages}, request_id=_request_id(headers))


def _ocr_overlay(value: Any) -> dict[str, Any]:
    item = _dict(value)
    if type(item.get("HasOverlay")) is not bool:
        raise _bad()
    lines = item.get("Lines")
    if not isinstance(lines, list) or len(lines) > 4096:
        raise _bad()
    normalized_lines = []
    total_words = 0
    for line in lines:
        line = _dict(line)
        words = line.get("Words")
        if not isinstance(words, list) or len(words) > 4096:
            raise _bad()
        total_words += len(words)
        if total_words > 65536:
            raise _bad()
        normalized_words = []
        for word in words:
            word = _dict(word)
            values = {
                key.lower(): _number(word.get(key)) for key in ("Left", "Top", "Width", "Height")
            }
            if any(number < 0 for number in values.values()):
                raise _bad()
            normalized_words.append({"text": _text(word.get("WordText"), empty=True), **values})
        height, top = _number(line.get("MaxHeight")), _number(line.get("MinTop"))
        if min(height, top) < 0:
            raise _bad()
        normalized_lines.append({"words": normalized_words, "max_height": height, "min_top": top})
    result = {"has_overlay": item["HasOverlay"], "lines": normalized_lines}
    if item.get("Message") is not None:
        result["message"] = _text(item["Message"], empty=True)
    return result


def _profile(
    endpoints: Mapping[str, str],
    capability: str,
    serializer: Serializer,
    parser: Parser,
    options: frozenset[str] = frozenset(),
    features: frozenset[str] | None = None,
    admission: Admission | None = None,
    *,
    google: bool = False,
    form: bool = False,
) -> FamilyAdapter:
    return FamilyAdapter(
        endpoints,
        {capability: serializer},
        {capability: parser},
        {capability: options},
        features or frozenset({capability, "text"}),
        admission,
        "x-goog-api-key" if google else "Authorization",
        "form" if form else "api_key" if google else "Bearer",
    )


PACKAGED_FAMILY_ADAPTERS: dict[str, object] = {
    "openai_inference": FamilyAdapter(
        CHAT_URLS,
        {"text_generation": _chat, "vision": _chat},
        {"text_generation": _chat_result, "vision": _chat_result},
        {"text_generation": CHAT_OPTIONS, "vision": CHAT_OPTIONS},
        TEXT_FEATURES | {"audio_input", "document_input"},
        _chat_admit,
    ),
    "openai_embeddings": _profile(
        {"openrouter": EMBED_URLS["openrouter"]},
        "embedding",
        _embed,
        _embedding_result,
        frozenset({"dimensions"}),
    ),
    "nvidia_embeddings": _profile(
        {"nvidia": EMBED_URLS["nvidia"]},
        "embedding",
        _nvidia_embed,
        _embedding_result,
        frozenset({"input_type"}),
        admission=_nvidia_embed_admit,
    ),
    # NVIDIA documents different request schemas for GTE/GTR and NV-Embed-QA/E5.
    # The administrator chooses the profile; the model name never selects it.
    "nvidia_gte_embeddings": _profile(
        {"nvidia": EMBED_URLS["nvidia"]}, "embedding", _embed, _embedding_result
    ),
    "mistral_embeddings": _profile(
        {"mistral": EMBED_URLS["mistral"]},
        "embedding",
        _mistral_embed,
        _embedding_result,
        frozenset({"dimensions"}),
    ),
    "nvidia_rerank": _profile(
        {"nvidia": "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking"},
        "rerank",
        _rerank,
        _rerank_result,
    ),
    "mistral_fim": _profile(
        {"mistral": "https://api.mistral.ai/v1/fim/completions"},
        "code_completion",
        _fim,
        _chat_result,
        frozenset({"temperature", "top_p", "seed", "stop", "suffix", "stream"}),
        admission=_fim_admit,
    ),
    "mistral_ocr": _profile(
        {"mistral": "https://api.mistral.ai/v1/ocr"},
        "ocr",
        _mistral_ocr,
        _mistral_ocr_result,
        features=frozenset({"ocr", "text", "document_input", "vision"}),
    ),
    "groq_audio_transcription": _profile(
        {"groq": "https://api.groq.com/openai/v1/audio/transcriptions"},
        "audio_transcription",
        _audio_form,
        _asr_result,
        frozenset({"language", "prompt"}),
        frozenset({"audio_input", "text"}),
    ),
    "groq_audio_translation": _profile(
        {"groq": "https://api.groq.com/openai/v1/audio/translations"},
        "audio_translation",
        _audio_form,
        _asr_result,
        frozenset({"prompt", "target_language"}),
        frozenset({"audio_input", "text"}),
        _english_translation,
    ),
    "groq_tts": _profile(
        {"groq": "https://api.groq.com/openai/v1/audio/speech"},
        "tts",
        _groq_tts,
        _audio_binary,
        frozenset({"voice", "format"}),
        admission=_groq_tts_admit,
    ),
    "mistral_audio_transcription": _profile(
        {"mistral": "https://api.mistral.ai/v1/audio/transcriptions"},
        "audio_transcription",
        _mistral_audio_form,
        _asr_result,
        frozenset({"language"}),
        frozenset({"audio_input", "text"}),
    ),
    "mistral_tts": _profile(
        {"mistral": "https://api.mistral.ai/v1/audio/speech"},
        "tts",
        _mistral_tts,
        _mistral_tts_result,
        frozenset({"voice", "format"}),
        admission=_mistral_tts_admit,
    ),
    "mistral_moderation": _profile(
        {"mistral": "https://api.mistral.ai/v1/moderations"},
        "moderation",
        _classify,
        _moderation_result,
    ),
    "mistral_classification": _profile(
        {"mistral": "https://api.mistral.ai/v1/classifications"},
        "classification",
        _classify,
        _classification_result,
    ),
    "gemini_inference": FamilyAdapter(
        {"google": GOOGLE_GENERATE},
        {
            "text_generation": _google,
            "vision": _google,
            "audio_transcription": _google_asr,
            "image_generation": _google_image,
            "tts": _google_tts,
        },
        {
            k: _google_result
            for k in ("text_generation", "vision", "audio_transcription", "image_generation", "tts")
        },
        {
            "text_generation": CHAT_OPTIONS,
            "vision": CHAT_OPTIONS,
            "audio_transcription": frozenset({"language", "prompt"}),
            "image_generation": frozenset(),
            "tts": frozenset({"voice", "format"}),
        },
        TEXT_FEATURES
        | {"audio_input", "document_input", "tts", "image_generation", "google_continuation"},
        _google_admit,
        "x-goog-api-key",
        "api_key",
    ),
    "gemini_embeddings": _profile(
        {"google": GOOGLE_EMBED},
        "embedding",
        _google_embed,
        _google_embed_result,
        frozenset({"dimensions"}),
        admission=_google_embed_admit,
        google=True,
    ),
    "cloudflare_text": _profile(
        {"cloudflare": CF_PATH},
        "text_generation",
        _cf_text,
        _cf_text_result,
        frozenset({"temperature", "top_p", "seed", "response_format", "stream"}),
        frozenset({"text", "json_output"}),
        admission=_cf_text_admit,
    ),
    "cloudflare_embeddings": _profile(
        {"cloudflare": CF_PATH}, "embedding", _cf_embed, _cf_embed_result
    ),
    "cloudflare_translation": _profile(
        {"cloudflare": CF_PATH},
        "translation",
        _cf_translation,
        _cf_translation_result,
        frozenset({"source_language", "target_language"}),
    ),
    "cloudflare_audio_transcription": _profile(
        {"cloudflare": CF_PATH},
        "audio_transcription",
        _cf_asr,
        _cf_asr_result,
        frozenset({"language", "prompt"}),
        frozenset({"audio_input", "text"}),
    ),
    "cloudflare_audio_translation": _profile(
        {"cloudflare": CF_PATH},
        "audio_translation",
        _cf_asr,
        _cf_asr_result,
        frozenset({"language", "prompt", "target_language"}),
        frozenset({"audio_input", "text"}),
        _english_translation,
    ),
    "cloudflare_tts": _profile(
        {"cloudflare": CF_PATH},
        "tts",
        _cf_tts,
        _audio_binary,
        frozenset({"format"}),
        admission=_cf_tts_admit,
    ),
    "cloudflare_image_generation": _profile(
        {"cloudflare": CF_PATH},
        "image_generation",
        _cf_image,
        _image_binary,
        frozenset({"size", "n", "format"}),
        admission=_cf_image_admit,
    ),
    "cloudflare_classification": _profile(
        {"cloudflare": CF_PATH},
        "classification",
        _cf_classify,
        _cf_classify_result,
        admission=_cf_classify_admit,
    ),
    "openrouter_image_generation": _profile(
        {"openrouter": "https://openrouter.ai/api/v1/images"},
        "image_generation",
        _or_image,
        _or_image_result,
        frozenset({"size", "n", "format"}),
        admission=_or_image_admit,
    ),
    "ocrspace_inference": _profile(
        {"ocrspace": "https://api.ocr.space/parse/image"},
        "ocr",
        _ocrspace,
        _ocrspace_result,
        frozenset({"language", "detect_orientation", "scale", "is_table", "overlay"}),
        frozenset({"ocr", "text", "vision", "document_input"}),
        _ocrspace_admit,
        form=True,
    ),
}

UNSUPPORTED_PROTOCOL_GAPS = {
    "openrouter_audio_output": "documented_audio_output_requires_streaming",
    "cloudflare_other_model_schemas": "model_run_task_family_does_not_define_one_wire_schema",
    "gemini_batch_embeddings": "embed_content_one_text_only_no_hidden_fanout",
    "realtime": "buffered_contract_has_no_realtime_execution_semantics",
}
