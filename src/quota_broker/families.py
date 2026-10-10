"""Pure, registered request/result contracts; no transport, files, or inference.

Media is inline base64 only. Resource bounds deliberately omit metrics that cannot
be inferred from validated content (for example compressed duration or provider billing).
Tool definitions and calls are data; this module never executes tools.
"""

import base64
import binascii
import io
import json
import math
import re
import subprocess
import sys
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar, cast


class FamilyError(ValueError):
    """Safe fixed diagnostics which never interpolate request content."""

    def __init__(self, message: str = "invalid family payload") -> None:
        super().__init__(message if message in _ERRORS else "invalid family payload")


_ERRORS = frozenset(
    {
        "invalid family payload",
        "unsupported family",
        "unsupported option",
        "streaming and realtime are unsupported",
        "family payload exceeds limit",
        "invalid media",
        "invalid family result",
        "invalid media limits",
    }
)


@dataclass(frozen=True)
class MediaLimits:
    decoded_input_bytes: int = 8 * 1024 * 1024
    request_bytes: int = 16 * 1024 * 1024
    result_bytes: int = 32 * 1024 * 1024
    max_parts: int = 32

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.decoded_input_bytes,
                self.request_bytes,
                self.result_bytes,
                self.max_parts,
            )
        ):
            raise FamilyError("invalid media limits")


@dataclass(frozen=True)
class PreparedInput:
    input: dict[str, object]
    options: dict[str, object]
    resources: dict[str, int]
    input_bytes: int
    required_features: tuple[str, ...]


def _keys(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if (
        not isinstance(value, dict)
        or not all(isinstance(k, str) for k in value)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise FamilyError()
    return value


def _string(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise FamilyError()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise FamilyError() from None
    return value


def _integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise FamilyError()
    return value


def _number(value: object, low: float = -math.inf, high: float = math.inf) -> float:
    if type(value) not in (int, float):
        raise FamilyError()
    try:
        result = float(cast(int | float, value))
    except (OverflowError, ValueError):
        raise FamilyError() from None
    if not math.isfinite(result) or not low <= result <= high:
        raise FamilyError()
    return result


def _list(value: object, maximum: int, *, empty: bool = False) -> list:
    if not isinstance(value, list) or len(value) > maximum or (not value and not empty):
        raise FamilyError()
    return value


def _json_size(value: object, maximum: int, *, depth: int = 0) -> int:
    # Validate depth and types before serialization, including cycles and nonfinite numbers.
    if depth > 16:
        raise FamilyError("family payload exceeds limit")
    if isinstance(value, dict):
        if len(value) > 1024:
            raise FamilyError("family payload exceeds limit")
        for key, item in value.items():
            _string(key, empty=True)
            _json_size(item, maximum, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 65536:
            raise FamilyError("family payload exceeds limit")
        for item in value:
            _json_size(item, maximum, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > maximum:
            raise FamilyError("family payload exceeds limit")
        _string(value, empty=True)
    elif value is not None and type(value) not in (bool, int, float):
        raise FamilyError()
    elif type(value) in (int, float):
        _number(value)
    if depth:
        return 0
    try:
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > maximum:
                raise FamilyError("family payload exceeds limit")
    except FamilyError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise FamilyError() from None
    if size > maximum:
        raise FamilyError("family payload exceeds limit")
    return size


_MEDIA = {
    "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
    "audio": frozenset(
        {
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/ogg",
            "audio/flac",
            "audio/mp4",
            "audio/webm",
            "audio/aac",
        }
    ),
    "document": frozenset({"application/pdf"}),
}


def _header(mime: str, raw: bytes) -> bool:
    checks = {
        "image/png": lambda: raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33,
        "image/jpeg": lambda: raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"),
        "image/gif": lambda: raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 13,
        "image/webp": lambda: raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 20,
        "audio/wav": lambda: raw[:4] == b"RIFF" and raw[8:12] == b"WAVE",
        "audio/x-wav": lambda: raw[:4] == b"RIFF" and raw[8:12] == b"WAVE",
        "audio/mpeg": lambda: (
            (raw.startswith(b"ID3") and len(raw) >= 10)
            or (len(raw) >= 4 and raw[0] == 255 and raw[1] & 224 == 224)
        ),
        "audio/aac": lambda: len(raw) >= 7 and raw[0] == 255 and raw[1] & 246 == 240,
        "audio/ogg": lambda: raw.startswith(b"OggS") and len(raw) >= 27,
        "audio/flac": lambda: raw.startswith(b"fLaC") and len(raw) >= 42,
        "audio/mp4": lambda: raw[4:8] == b"ftyp" and len(raw) >= 16,
        "audio/webm": lambda: raw.startswith(b"\x1aE\xdf\xa3") and len(raw) >= 8,
        "application/pdf": lambda: (
            bool(re.match(rb"%PDF-1\.[0-7]|%PDF-2\.0", raw)) and raw.rstrip().endswith(b"%%EOF")
        ),
    }
    return checks[mime]()


_PDF_PROGRAM = """
import io
import sys
if sys.platform.startswith('linux'):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
from pypdf import PdfReader
try:
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=True)
    if reader.is_encrypted:
        raise ValueError()
    count = len(reader.pages)
    if not 1 <= count <= 128:
        raise ValueError()
    # Materialize content streams under the memory/time limits, detecting corrupt
    # or compressed bomb payloads without interpreting scripts or following links.
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None:
            contents.get_data()
    sys.stdout.write(str(count))
except Exception:
    sys.exit(1)
"""


def _pdf_pages(raw: bytes) -> int:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _PDF_PROGRAM],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise FamilyError("invalid media") from None
    if completed.returncode != 0 or not re.fullmatch(rb"[1-9][0-9]{0,2}", completed.stdout):
        raise FamilyError("invalid media")
    count = int(completed.stdout)
    if not 1 <= count <= 128:
        raise FamilyError("invalid media")
    return count


@dataclass
class _Content:
    limits: MediaLimits
    output: bool = False
    parts: int = 0
    decoded_bytes: int = 0
    images: int = 0
    audio_seconds: int = 0
    pages: int = 0
    audio_unknown: bool = False
    media_present: bool = False
    overlay_words: int = 0
    pending: list[tuple[str, str, str]] = field(default_factory=list)
    features: set[str] = field(default_factory=set)

    def count(self) -> None:
        self.parts += 1
        if self.parts > self.limits.max_parts:
            raise FamilyError("family payload exceeds limit")

    def media(self, value: object, allowed: set[str]) -> dict[str, object]:
        part = _keys(value, {"type", "mime_type", "data"}, {"thought_signature"})
        kind, mime, data = part["type"], part["mime_type"], part["data"]
        if (
            not isinstance(kind, str)
            or kind not in allowed
            or not isinstance(mime, str)
            or mime not in _MEDIA[kind]
            or not isinstance(data, str)
            or not data
        ):
            raise FamilyError("invalid media")
        self.count()
        # Bound allocation before decoding. Require canonical padded RFC 4648 base64.
        if (
            len(data) % 4
            or len(data)
            > 4
            * ((self.limits.result_bytes if self.output else self.limits.decoded_input_bytes) + 2)
            // 3
            + 4
        ):
            raise FamilyError("family payload exceeds limit")
        padding = len(data) - len(data.rstrip("="))
        estimate = len(data) // 4 * 3 - padding
        bound = self.limits.result_bytes if self.output else self.limits.decoded_input_bytes
        if estimate <= 0 or self.decoded_bytes + estimate > bound:
            raise FamilyError("family payload exceeds limit")
        self.decoded_bytes += estimate
        self.media_present = True
        self.pending.append((kind, mime, data))
        if kind == "image":
            self.images += 1
            self.features.add("vision")
        elif kind == "audio":
            self.features.add("audio_input")
        elif kind == "document":
            self.features.add("document_input")
        normalized: dict[str, object] = {"type": kind, "mime_type": mime, "data": data}
        if "thought_signature" in part:
            normalized["thought_signature"] = _string(part["thought_signature"])
            self.features.add("google_continuation")
        return normalized

    def finish(self) -> None:
        # All part counts and aggregate decoded lengths are checked before allocating.
        for kind, mime, data in self.pending:
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise FamilyError("invalid media") from None
            if base64.b64encode(raw).decode("ascii") != data or not _header(mime, raw):
                raise FamilyError("invalid media")
            if kind == "document":
                self.pages += _pdf_pages(raw)
            if kind == "audio":
                if mime in {"audio/wav", "audio/x-wav"}:
                    try:
                        with wave.open(io.BytesIO(raw), "rb") as wav:
                            frames, rate = wav.getnframes(), wav.getframerate()
                            expected = frames * wav.getnchannels() * wav.getsampwidth()
                            if frames <= 0 or rate <= 0 or len(wav.readframes(frames)) != expected:
                                raise FamilyError("invalid media")
                            self.audio_seconds += math.ceil(frames / rate)
                    except (wave.Error, EOFError, OSError):
                        raise FamilyError("invalid media") from None
                else:
                    self.audio_unknown = True


_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$defs",
        "$ref",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "uniqueItems",
        "pattern",
        "format",
        "default",
    }
)
_SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def _validate_schema(schema: object) -> None:
    value = _keys(schema, set(), set(_SCHEMA_KEYS))
    for key, item in value.items():
        if key == "type":
            types = item if isinstance(item, list) else [item]
            if (
                not types
                or len(types) > 7
                or not all(isinstance(t, str) for t in types)
                or len(set(types)) != len(types)
                or not set(types) <= _SCHEMA_TYPES
            ):
                raise FamilyError()
        elif key in {"properties", "$defs"}:
            mapping = _keys(item, set(), set(item) if isinstance(item, dict) else set())
            for name, child in mapping.items():
                if len(_string(name)) > 128:
                    raise FamilyError()
                _validate_schema(child)
        elif key in {"items", "not"}:
            _validate_schema(item)
        elif key == "additionalProperties":
            if type(item) is not bool:
                _validate_schema(item)
        elif key in {"anyOf", "oneOf", "allOf"}:
            for child in _list(item, 32):
                _validate_schema(child)
        elif key == "required":
            required = [_string(name) for name in _list(item, 128, empty=True)]
            if len(set(required)) != len(required):
                raise FamilyError()
        elif key == "$ref":
            reference = _string(item)
            if len(reference) > 512 or not reference.startswith("#/$defs/"):
                raise FamilyError()
        elif key in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}:
            number = _number(item)
            if key == "multipleOf" and number <= 0:
                raise FamilyError()
        elif key in {
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
        }:
            _integer(item, 0, 2**31 - 1)
        elif key == "uniqueItems":
            if type(item) is not bool:
                raise FamilyError()
        elif key == "enum":
            values = _list(item, 128)
            encoded = [json.dumps(v, sort_keys=True, allow_nan=False) for v in values]
            if len(set(encoded)) != len(encoded):
                raise FamilyError()
        elif key not in {"const", "default"}:
            if len(_string(item)) > 4096:
                raise FamilyError()
    for lower, upper in (
        ("minimum", "maximum"),
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minProperties", "maxProperties"),
    ):
        if lower in value and upper in value and value[lower] > value[upper]:
            raise FamilyError()


def _schema(value: object) -> dict:
    _json_size(value, 65536)
    _validate_schema(value)
    return json.loads(json.dumps(value))


def _tool_calls(value: object, limits: MediaLimits) -> list[dict]:
    result = []
    for item in _list(value, limits.max_parts):
        call = _keys(item, {"id", "type", "function"}, {"thought_signature", "provider_call_id"})
        if call["type"] != "function":
            raise FamilyError()
        function = _keys(call["function"], {"name", "arguments"})
        name = _string(function["name"])
        arguments = _string(function["arguments"])
        if len(name) > 128 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise FamilyError()
        if len(arguments) > 65536:
            raise FamilyError("family payload exceeds limit")
        try:
            parsed = json.loads(arguments)
        except (ValueError, RecursionError):
            raise FamilyError() from None
        if not isinstance(parsed, dict):
            raise FamilyError()
        _json_size(parsed, 65536)
        normalized: dict[str, object] = {
            "id": _string(call["id"]),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        if "thought_signature" in call:
            # Opaque provider state stays attached to this exact call. Never
            # decode, interpret, combine, or use it as diagnostic metadata.
            normalized["thought_signature"] = _string(call["thought_signature"])
        if "provider_call_id" in call:
            normalized["provider_call_id"] = (
                None if call["provider_call_id"] is None else _string(call["provider_call_id"])
            )
        result.append(normalized)
    return result


def _messages(value: object, content: _Content) -> list[dict]:
    result = []
    for item in _list(value, content.limits.max_parts):
        message = _keys(
            item, {"role", "content"}, {"name", "tool_call_id", "tool_calls", "provider_state"}
        )
        if message["role"] not in ("system", "user", "assistant", "tool"):
            raise FamilyError()
        normalized = {"role": message["role"]}
        state = None
        visible = content
        if "provider_state" in message:
            if message["role"] != "assistant":
                raise FamilyError()
            state = _google_state(message["provider_state"], content)
            visible = _Content(content.limits)
        body = message["content"]
        if isinstance(body, str):
            normalized["content"] = _string(body, empty=bool(message.get("tool_calls")))
            visible.count()
            visible.features.add("text")
        else:
            parts: list[dict[str, object]] = []
            for part in _list(body, content.limits.max_parts):
                if isinstance(part, dict) and part.get("type") == "text":
                    _keys(part, {"type", "text"}, {"thought_signature"})
                    normalized_part: dict[str, object] = {
                        "type": "text",
                        "text": _string(part["text"], empty="thought_signature" in part),
                    }
                    if "thought_signature" in part:
                        normalized_part["thought_signature"] = _string(part["thought_signature"])
                        visible.features.add("google_continuation")
                    parts.append(normalized_part)
                    visible.count()
                    visible.features.add("text")
                else:
                    parts.append(visible.media(part, {"image", "audio", "document"}))
            normalized["content"] = parts
        for key in ("name", "tool_call_id"):
            if key in message:
                normalized[key] = _string(message[key])
                content.features.add("tool_calling")
        if "tool_calls" in message:
            if message["role"] != "assistant":
                raise FamilyError()
            normalized["tool_calls"] = _tool_calls(message["tool_calls"], content.limits)
            content.features.add("tool_calling")
            if any("thought_signature" in call for call in normalized["tool_calls"]):
                content.features.add("google_continuation")
        if message["role"] == "tool" and "tool_call_id" not in message:
            raise FamilyError()
        if state is not None:
            _google_state_matches(normalized, state)
            normalized["provider_state"] = state
            content.features.update(visible.features)
            content.features.add("google_continuation")
        result.append(normalized)
    return result


def _google_state(value: object, content: _Content) -> dict:
    state = _keys(value, {"provider", "model", "parts"})
    model = _string(state["model"])
    if state["provider"] != "google" or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model
    ):
        raise FamilyError()
    parts = []
    for item in _list(state["parts"], content.limits.max_parts):
        part = _keys(
            item, set(), {"text", "functionCall", "inlineData", "thoughtSignature", "thought"}
        )
        kinds = part.keys() & {"text", "functionCall", "inlineData"}
        if len(kinds) != 1 or ("thought" in part and type(part["thought"]) is not bool):
            raise FamilyError()
        normalized: dict[str, object] = {}
        if "text" in part:
            normalized["text"] = _string(part["text"], empty=True)
            content.count()
            content.features.add("text")
        elif "functionCall" in part:
            call = _keys(part["functionCall"], {"name"}, {"args", "id"})
            name = _string(call["name"])
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", name):
                raise FamilyError()
            function: dict[str, object] = {"name": name}
            if "args" in call:
                if not isinstance(call["args"], dict):
                    raise FamilyError()
                _json_size(call["args"], 65536)
                function["args"] = json.loads(json.dumps(call["args"]))
            if "id" in call:
                function["id"] = _string(call["id"])
            normalized["functionCall"] = function
            content.count()
            content.features.add("tool_calling")
        else:
            inline = _keys(part["inlineData"], {"mimeType", "data"})
            mime = inline["mimeType"]
            kind = (
                next((kind for kind, types in _MEDIA.items() if mime in types), None)
                if isinstance(mime, str)
                else None
            )
            if kind is None:
                raise FamilyError("invalid media")
            media = content.media({"type": kind, "mime_type": mime, "data": inline["data"]}, {kind})
            normalized["inlineData"] = {"mimeType": media["mime_type"], "data": media["data"]}
        if "thoughtSignature" in part:
            normalized["thoughtSignature"] = _string(part["thoughtSignature"])
        if "thought" in part:
            normalized["thought"] = part["thought"]
        parts.append(normalized)
    return {"provider": "google", "model": model, "parts": parts}


def _google_state_matches(message: dict, state: dict) -> None:
    visible: list[dict[str, object]] = []
    calls = []
    for part in state["parts"]:
        if "functionCall" in part:
            calls.append(part)
        elif not part.get("thought", False):
            if "text" in part:
                typed: dict[str, object] = {"type": "text", "text": part["text"]}
            else:
                inline = part["inlineData"]
                mime = inline["mimeType"]
                kind = next(kind for kind, types in _MEDIA.items() if mime in types)
                typed = {"type": kind, "mime_type": mime, "data": inline["data"]}
            if "thoughtSignature" in part:
                typed["thought_signature"] = part["thoughtSignature"]
            visible.append(typed)
    actual = message["content"]
    if isinstance(actual, str):
        if any(part["type"] != "text" for part in visible) or actual != "".join(
            cast(str, part["text"]) for part in visible
        ):
            raise FamilyError()
    elif actual != visible:
        raise FamilyError()
    actual_calls = message.get("tool_calls", [])
    if len(actual_calls) != len(calls):
        raise FamilyError()
    for actual_call, part in zip(actual_calls, calls, strict=True):
        provider = part["functionCall"]
        function = actual_call["function"]
        provider_id = actual_call.get(
            "provider_call_id", actual_call["id"] if "id" in provider else None
        )
        if (
            function["name"] != provider["name"]
            or json.dumps(json.loads(function["arguments"]), sort_keys=True, separators=(",", ":"))
            != json.dumps(provider.get("args", {}), sort_keys=True, separators=(",", ":"))
            or provider_id != provider.get("id")
            or actual_call.get("thought_signature") != part.get("thoughtSignature")
        ):
            raise FamilyError()


def _options(value: object, allowed: frozenset[str], content: _Content) -> dict[str, object]:
    options = _keys(value, set(), set(allowed) | {"stream", "realtime"})
    if options.get("stream") is True or options.get("realtime") is True:
        raise FamilyError("streaming and realtime are unsupported")
    if "realtime" in options:
        raise FamilyError("unsupported option")
    result: dict[str, object] = {}
    for key, item in options.items():
        if key == "stream":
            if item is not False:
                raise FamilyError("unsupported option")
            result[key] = False
        elif key in {"temperature", "top_p", "speed"}:
            bounds = {"temperature": (0, 2), "top_p": (0, 1), "speed": (0.25, 4)}
            result[key] = _number(item, *bounds[key])
        elif key in {"seed", "n", "dimensions", "top_n"}:
            bounds_int = {
                "seed": (0, 2**32 - 1),
                "n": (1, content.limits.max_parts),
                "dimensions": (1, 65536),
                "top_n": (1, content.limits.max_parts),
            }
            result[key] = _integer(item, *bounds_int[key])
        elif key in {
            "normalize",
            "multi_label",
            "detect_orientation",
            "scale",
            "is_table",
            "overlay",
        }:
            if type(item) is not bool:
                raise FamilyError()
            result[key] = item
        elif key in {"stop", "labels"}:
            strings = [_string(v) for v in _list(item, content.limits.max_parts)]
            if any(len(v) > 1024 for v in strings) or len(set(strings)) != len(strings):
                raise FamilyError()
            result[key] = strings
        elif key == "response_format":
            if item not in ("text", "json_object"):
                raise FamilyError("unsupported option")
            result[key] = item
            if item == "json_object":
                content.features.add("json_output")
        elif key == "json_schema":
            result[key] = _schema(item)
            content.features.add("structured_output")
        elif key == "tools":
            tools = []
            names = set()
            for tool in _list(item, content.limits.max_parts):
                tool = _keys(tool, {"type", "function"})
                function = _keys(
                    tool["function"], {"name", "parameters"}, {"description", "strict"}
                )
                name = _string(function["name"])
                if (
                    tool["type"] != "function"
                    or name in names
                    or len(name) > 128
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name)
                ):
                    raise FamilyError()
                names.add(name)
                fn: dict[str, object] = {
                    "name": name,
                    "parameters": _schema(function["parameters"]),
                }
                if "description" in function:
                    fn["description"] = _string(function["description"])
                if "strict" in function:
                    if type(function["strict"]) is not bool:
                        raise FamilyError()
                    fn["strict"] = function["strict"]
                tools.append({"type": "function", "function": fn})
            result[key] = tools
            content.features.add("tool_calling")
        elif key == "tool_choice":
            result[key] = _string(item)
            content.features.add("tool_calling")
        elif key == "reasoning_effort":
            if item not in ("none", "minimal", "low", "medium", "high"):
                raise FamilyError("unsupported option")
            result[key] = item
            content.features.add("reasoning")
        elif key == "input_type":
            if item not in ("query", "passage"):
                raise FamilyError("unsupported option")
            result[key] = item
        elif key == "suffix":
            suffix = _string(item, empty=True)
            if len(suffix.encode("utf-8")) > 65536:
                raise FamilyError("family payload exceeds limit")
            result[key] = suffix
        else:
            text = _string(item)
            if len(text) > 4096:
                raise FamilyError("family payload exceeds limit")
            if key in {"language", "source_language", "target_language"} and not re.fullmatch(
                r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", text
            ):
                raise FamilyError()
            if key == "format":
                formats = (
                    {"png", "jpeg", "webp"}
                    if "size" in allowed
                    else {"wav", "mp3", "ogg", "flac", "aac", "opus"}
                )
                if text not in formats:
                    raise FamilyError("unsupported option")
            if key == "size" and not re.fullmatch(r"[1-9][0-9]{0,3}x[1-9][0-9]{0,3}", text):
                raise FamilyError()
            if key == "voice" and len(text) > 128:
                raise FamilyError()
            result[key] = text
    if "tool_choice" in result:
        names = {tool["function"]["name"] for tool in cast(list[dict], result.get("tools", []))}
        if not names or result["tool_choice"] not in names | {"auto", "none", "required"}:
            raise FamilyError()
    if "json_schema" in result and result.get("response_format") == "text":
        raise FamilyError()
    return result


class FamilyContract:
    options: ClassVar[frozenset[str]] = frozenset()
    text_output: ClassVar[bool] = False
    feature: ClassVar[str] = "text"

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        raise NotImplementedError

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        raise NotImplementedError

    @classmethod
    def prepare(
        cls, value: dict, options: dict, limits: MediaLimits, max_output_tokens: int
    ) -> PreparedInput:
        _integer(max_output_tokens, 1 if cls.text_output else 0, 65536)
        size = _json_size({"input": value, "options": options}, limits.request_bytes)
        content = _Content(limits)
        normalized = cls.normalize(value, content)
        normalized_options = _options(options, cls.options, content)
        content.finish()
        content.features.add(cls.feature)
        resources = {"requests": 1}
        if not content.media_present:
            # UTF-8 bytes plus JSON/tool syntax give a conservative text-token bound.
            resources["input_tokens"] = size
        if cls.text_output:
            resources["output_tokens"] = max_output_tokens
            if "input_tokens" in resources:
                resources["total_tokens"] = resources["input_tokens"] + max_output_tokens
        if content.images:
            resources["images"] = content.images
        if content.pages:
            resources["pages"] = content.pages
        if content.audio_seconds and not content.audio_unknown:
            resources["audio_seconds"] = content.audio_seconds
        cls.add_resources(resources, normalized, normalized_options)
        return PreparedInput(
            normalized, normalized_options, resources, size, tuple(sorted(content.features))
        )

    @classmethod
    def add_resources(cls, resources: dict[str, int], value: dict, options: dict) -> None:
        pass

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        """Validate relationships after the input and result contracts have passed."""

    @classmethod
    def validate_result(cls, value: dict, limits: MediaLimits) -> dict[str, object]:
        _json_size(value, limits.result_bytes)
        try:
            content = _Content(limits, output=True)
            result = cls.result(value, content)
            content.finish()
            return result
        except FamilyError:
            raise FamilyError("invalid family result") from None


class TextContract(FamilyContract):
    options = frozenset(
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
        }
    )
    text_output = True

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"messages"})
        return {"messages": _messages(item["messages"], content)}

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, set(), {"text", "messages", "tool_calls"})
        if not item:
            raise FamilyError()
        result: dict[str, object] = {}
        if "text" in item:
            result["text"] = _string(item["text"], empty=bool(item.get("tool_calls")))
        if "messages" in item:
            result["messages"] = _messages(item["messages"], content)
        if "tool_calls" in item:
            result["tool_calls"] = _tool_calls(item["tool_calls"], content.limits)
        return result


class VisionContract(TextContract):
    feature = "vision"

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        result = super().normalize(value, content)
        if not content.images:
            raise FamilyError()
        return result


class CodeContract(TextContract):
    options = TextContract.options | {"suffix"}


class TextTranslationContract(FamilyContract):
    feature = "translation"
    # Registered buffered translation profiles expose no output-token parameter.
    # Their result bytes remain bounded; token pricing requires an operator bound.
    text_output = False
    options = frozenset({"source_language", "target_language"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"text"})
        content.count()
        content.features.add("text")
        return {"text": _string(item["text"])}

    @classmethod
    def add_resources(cls, resources: dict[str, int], value: dict, options: dict) -> None:
        source = options.get("source_language")
        target = options.get("target_language")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source.lower() == target.lower()
        ):
            raise FamilyError()

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"text"})
        return {"text": _string(item["text"])}


class OCRContract(FamilyContract):
    feature = "ocr"
    text_output = False
    options = frozenset({"language", "detect_orientation", "scale", "is_table", "overlay"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"document"})
        return {"document": content.media(item["document"], {"document", "image"})}

    @classmethod
    def add_resources(cls, resources: dict[str, int], value: dict, options: dict) -> None:
        resources["conversions"] = 1

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        bound = (resources or {}).get("pages")
        if bound is None and value["document"]["type"] == "image":
            bound = 1
        pages = result["pages"]
        numbered = [page["page"] for page in pages if "page" in page]
        if len(set(numbered)) != len(numbered) or (
            bound is not None and (len(pages) > bound or any(page > bound for page in numbered))
        ):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"pages"})
        pages = []
        for page in _list(item["pages"], content.limits.max_parts):
            page = _keys(page, {"text"}, {"page", "overlay"})
            normalized: dict[str, object] = {"text": _string(page["text"], empty=True)}
            if "page" in page:
                normalized["page"] = _integer(page["page"], 1, 2**31 - 1)
            if "overlay" in page:
                normalized["overlay"] = _ocr_overlay(page["overlay"], content)
            pages.append(normalized)
        return {"pages": pages}


def _ocr_overlay(value: object, content: _Content) -> dict[str, object]:
    overlay = _keys(value, {"has_overlay", "lines"}, {"message"})
    if type(overlay["has_overlay"]) is not bool:
        raise FamilyError()
    lines = []
    # Overlay words are text geometry, rather than uploaded media parts. These
    # limits accommodate normal documents while serialized result bytes remain
    # the overall bound for all pages and their coordinates.
    for line in _list(overlay["lines"], content.limits.max_parts * 128, empty=True):
        line = _keys(line, {"words", "max_height", "min_top"})
        words = []
        for word in _list(line["words"], content.limits.max_parts * 128, empty=True):
            content.overlay_words += 1
            if content.overlay_words > content.limits.max_parts * 2048:
                raise FamilyError("family payload exceeds limit")
            word = _keys(word, {"text", "left", "top", "width", "height"})
            words.append(
                {
                    "text": _string(word["text"], empty=True),
                    **{name: _number(word[name], 0) for name in ("left", "top", "width", "height")},
                }
            )
        lines.append(
            {
                "words": words,
                "max_height": _number(line["max_height"], 0),
                "min_top": _number(line["min_top"], 0),
            }
        )
    result: dict[str, object] = {"has_overlay": overlay["has_overlay"], "lines": lines}
    if "message" in overlay:
        result["message"] = _string(overlay["message"], empty=True)
    return result


class TranscriptionContract(FamilyContract):
    feature = "audio_input"
    text_output = False
    options = frozenset({"language", "prompt"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"audio"})
        return {"audio": content.media(item["audio"], {"audio"})}

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"text"}, {"language"})
        result: dict[str, object] = {"text": _string(item["text"], empty=True)}
        if "language" in item:
            result["language"] = _string(item["language"])
        return result


class TranslationContract(TranscriptionContract):
    options = frozenset({"language", "prompt", "target_language"})


class TTSContract(FamilyContract):
    feature = "tts"
    options = frozenset({"voice", "format", "speed"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"text"})
        content.count()
        return {"text": _string(item["text"])}

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"audio"})
        return {"audio": content.media(item["audio"], {"audio"})}


class ImageContract(FamilyContract):
    feature = "image_generation"
    options = frozenset({"size", "n", "format"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"prompt"})
        content.count()
        return {"prompt": _string(item["prompt"])}

    @classmethod
    def add_resources(cls, resources: dict[str, int], value: dict, options: dict) -> None:
        resources["images"] = options.get("n", 1)

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        if len(result["images"]) > options.get("n", 1):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"images"})
        return {
            "images": [
                content.media(image, {"image"})
                for image in _list(item["images"], content.limits.max_parts)
            ]
        }


class TextsContract(FamilyContract):
    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"texts"})
        strings = []
        for text in _list(item["texts"], content.limits.max_parts):
            content.count()
            strings.append(_string(text))
        return {"texts": strings}


class EmbeddingContract(TextsContract):
    feature = "embedding"
    options = frozenset({"dimensions", "normalize", "input_type"})

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        if len(result["vectors"]) != len(value["texts"]):
            raise FamilyError("invalid family result")
        dimensions = options.get("dimensions")
        if dimensions is not None and any(
            len(vector) != dimensions for vector in result["vectors"]
        ):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"vectors"})
        vectors = [
            [_number(number) for number in _list(vector, 65536)]
            for vector in _list(item["vectors"], content.limits.max_parts)
        ]
        if len({len(vector) for vector in vectors}) != 1:
            raise FamilyError()
        return {"vectors": vectors}


class RerankContract(FamilyContract):
    feature = "rerank"
    options = frozenset({"top_n"})

    @classmethod
    def normalize(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"query", "documents"})
        documents = []
        for document in _list(item["documents"], content.limits.max_parts):
            content.count()
            documents.append(_string(document))
        return {"query": _string(item["query"]), "documents": documents}

    @classmethod
    def add_resources(cls, resources: dict[str, int], value: dict, options: dict) -> None:
        if options.get("top_n", 1) > len(value["documents"]):
            raise FamilyError()

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        count = len(value["documents"])
        scores = result["scores"]
        indices = [score["index"] for score in scores]
        if (
            len(scores) > options.get("top_n", count)
            or len(set(indices)) != len(indices)
            or any(type(index) is not int or not 0 <= index < count for index in indices)
        ):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"scores"})
        scores = []
        seen = set()
        for score in _list(item["scores"], content.limits.max_parts):
            score = _keys(score, {"index", "score"})
            index = _integer(score["index"], 0, 2**31 - 1)
            if index in seen:
                raise FamilyError()
            seen.add(index)
            scores.append({"index": index, "score": _number(score["score"])})
        return {"scores": scores}


class ClassificationContract(TextsContract):
    feature = "classification"
    options = frozenset({"labels", "multi_label"})

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        if len(result["classes"]) != len(value["texts"]):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"classes"})
        classes = []
        for group in _list(item["classes"], content.limits.max_parts):
            labels = []
            for label in _list(group, content.limits.max_parts):
                label = _keys(label, {"label", "score"}, {"target"})
                normalized = {"label": _string(label["label"]), "score": _number(label["score"])}
                if "target" in label:
                    normalized["target"] = _string(label["target"])
                labels.append(normalized)
            classes.append(labels)
        return {"classes": classes}


class ModerationContract(TextsContract):
    feature = "moderation"

    @classmethod
    def correspondence(
        cls, value: dict, options: dict, result: dict, resources: dict[str, int] | None
    ) -> None:
        if len(result["results"]) != len(value["texts"]):
            raise FamilyError("invalid family result")

    @classmethod
    def result(cls, value: object, content: _Content) -> dict[str, object]:
        item = _keys(value, {"results"})
        results = []
        for record in _list(item["results"], content.limits.max_parts):
            record = _keys(record, {"flagged", "categories", "scores"})
            if type(record["flagged"]) is not bool:
                raise FamilyError()
            categories = _keys(
                record["categories"],
                set(),
                set(record["categories"]) if isinstance(record["categories"], dict) else set(),
            )
            scores = _keys(record["scores"], set(), set(categories))
            if (
                not categories
                or len(categories) > content.limits.max_parts
                or set(scores) != set(categories)
                or any(type(v) is not bool for v in categories.values())
            ):
                raise FamilyError()
            results.append(
                {
                    "flagged": record["flagged"],
                    "categories": dict(categories),
                    "scores": {k: _number(v, 0, 1) for k, v in scores.items()},
                }
            )
        return {"results": results}


class FamilyRegistry:
    """Explicit class registry; manifests cannot introduce executable contracts."""

    def __init__(self, contracts: Mapping[str, type[FamilyContract]]) -> None:
        self.contracts = MappingProxyType(dict(contracts))

    @classmethod
    def builtin(cls) -> "FamilyRegistry":
        return cls(
            {
                "text_generation": TextContract,
                "vision": VisionContract,
                "code_completion": CodeContract,
                "translation": TextTranslationContract,
                "ocr": OCRContract,
                "audio_transcription": TranscriptionContract,
                "audio_translation": TranslationContract,
                "tts": TTSContract,
                "image_generation": ImageContract,
                "embedding": EmbeddingContract,
                "rerank": RerankContract,
                "classification": ClassificationContract,
                "moderation": ModerationContract,
            }
        )

    def prepare(
        self,
        capability: str,
        input: dict,
        options: dict,
        limits: MediaLimits,
        max_output_tokens: int,
    ) -> PreparedInput:
        contract = self.contracts.get(capability)
        if contract is None:
            raise FamilyError("unsupported family")
        return contract.prepare(input, options, limits, max_output_tokens)

    def uses_output_tokens(self, capability: str) -> bool:
        """Whether the family exposes an enforceable output-token request limit.

        False means no token bound is inferred from a text result or media bytes.
        Model context limits and administrator cost bounds are separate gates.
        """
        contract = self.contracts.get(capability)
        if contract is None:
            raise FamilyError("unsupported family")
        return contract.text_output

    def validate_result(
        self, capability: str, result: dict, limits: MediaLimits
    ) -> dict[str, object]:
        contract = self.contracts.get(capability)
        if contract is None:
            raise FamilyError("unsupported family")
        return contract.validate_result(result, limits)

    def validate_correspondence(
        self,
        capability: str,
        input: dict,
        options: dict,
        result: dict,
        resources: dict[str, int] | None = None,
    ) -> None:
        contract = self.contracts.get(capability)
        if contract is None:
            raise FamilyError("unsupported family")
        try:
            contract.correspondence(input, options, result, resources)
        except (KeyError, TypeError, IndexError):
            raise FamilyError("invalid family result") from None
