"""Fixture-only family contracts: media bounds, typed schemas, and resource gaps."""

import base64
import io
import struct
import subprocess
import wave
from dataclasses import replace

import pytest
from pypdf import PdfWriter

from quota_broker.families import FamilyError, FamilyRegistry, MediaLimits

REGISTRY = FamilyRegistry.builtin()
LIMITS = MediaLimits()


def media(kind="image", mime="image/png", raw=None):
    if raw is None:
        # A fixture header; contracts validate signatures, not full image decoding.
        raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + b"\0" * 17
    return {"type": kind, "mime_type": mime, "data": base64.b64encode(raw).decode()}


def wav(frames=8001):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * frames)
    return media("audio", "audio/wav", buffer.getvalue())


def messages(content="hello"):
    return {"messages": [{"role": "user", "content": content}]}


def prepare(capability, value, options=None, limits=LIMITS):
    return REGISTRY.prepare(
        capability,
        value,
        options or {},
        limits,
        123 if REGISTRY.uses_output_tokens(capability) else 0,
    )


def result(capability, value, limits=LIMITS):
    return REGISTRY.validate_result(capability, value, limits)


@pytest.mark.parametrize(
    "capability,value,output",
    [
        ("text_generation", messages(), {"text": "answer"}),
        ("vision", messages([media()]), {"text": "answer"}),
        ("code_completion", messages(), {"text": "print(1)"}),
        ("ocr", {"document": media()}, {"pages": [{"page": 1, "text": "recognized"}]}),
        ("audio_transcription", {"audio": wav()}, {"text": "hello", "language": "en"}),
        ("audio_translation", {"audio": wav()}, {"text": "translated"}),
        ("tts", {"text": "hello"}, {"audio": wav()}),
        ("image_generation", {"prompt": "an image"}, {"images": [media()]}),
        ("embedding", {"texts": ["hello"]}, {"vectors": [[0.1, -0.2]]}),
        (
            "rerank",
            {"query": "q", "documents": ["a", "b"]},
            {"scores": [{"index": 1, "score": 0.9}]},
        ),
        ("classification", {"texts": ["hello"]}, {"classes": [[{"label": "a", "score": 0.8}]]}),
        (
            "moderation",
            {"texts": ["hello"]},
            {"results": [{"flagged": False, "categories": {"a": False}, "scores": {"a": 0.1}}]},
        ),
    ],
)
def test_all_builtin_families_prepare_and_validate(capability, value, output):
    prepared = prepare(capability, value)
    assert prepared.input == value
    assert prepared.resources["requests"] == 1
    assert prepared.input_bytes > 0
    assert result(capability, output) == output


def test_registry_is_explicit_and_immutable():
    assert len(REGISTRY.contracts) == 13
    with pytest.raises(TypeError):
        REGISTRY.contracts["new"] = object
    with pytest.raises(FamilyError, match="unsupported family"):
        prepare("unknown", {})


@pytest.mark.parametrize(
    "field", ["decoded_input_bytes", "request_bytes", "result_bytes", "max_parts"]
)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_limits_require_positive_integers(field, invalid):
    with pytest.raises(FamilyError, match="invalid media limits"):
        replace(LIMITS, **{field: invalid})


def test_text_bounds_include_tools_and_multiturn_structure_without_mutating_caller():
    value = {
        "messages": [
            {"role": "system", "content": "help"},
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
            {"role": "assistant", "content": "", "tool_calls": [call()]},
            {"role": "tool", "content": "done", "tool_call_id": "call-1"},
        ]
    }
    options = {
        "tools": [tool()],
        "tool_choice": "lookup",
        "temperature": 0.1,
        "response_format": "json_object",
        "reasoning_effort": "low",
        "stream": False,
    }
    prepared = prepare("text_generation", value, options)
    assert prepared.resources["input_tokens"] == prepared.input_bytes
    assert prepared.resources["total_tokens"] == prepared.input_bytes + 123
    assert prepared.required_features == ("json_output", "reasoning", "text", "tool_calling")
    prepared.input["messages"][0]["content"] = "changed"
    prepared.options["tools"][0]["function"]["parameters"]["type"] = "changed"
    assert value["messages"][0]["content"] == "help"
    assert options["tools"][0]["function"]["parameters"]["type"] == "object"


def call(arguments="{}"):
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": arguments},
    }


def tool():
    return {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


def test_structured_json_option_requires_feature_and_bounded_schema():
    prepared = prepare("text_generation", messages(), {"json_schema": {"type": "object"}})
    assert "structured_output" in prepared.required_features
    with pytest.raises(FamilyError):
        prepare("text_generation", messages(), {"json_schema": [], "response_format": "text"})
    with pytest.raises(FamilyError):
        prepare(
            "text_generation",
            messages(),
            {"json_schema": {"type": "object"}, "response_format": "text"},
        )


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": 1},
        {"stream": True},
        {"realtime": True},
        {"realtime": False},
        {"stream": 0},
        {"temperature": float("nan")},
        {"top_p": 2},
        {"seed": True},
        {"tool_choice": "auto"},
        {"tools": [tool()], "tool_choice": "missing"},
        {"tools": [tool(), tool()]},
        {"stop": [""]},
        {"reasoning_effort": "unexpected"},
    ],
)
def test_options_fail_closed(options):
    with pytest.raises(FamilyError):
        prepare("text_generation", messages(), options)


@pytest.mark.parametrize("arguments", ["not-json", "[]", '{"a":NaN}', '{"a":Infinity}'])
def test_tool_arguments_are_bounded_finite_json_objects(arguments):
    with pytest.raises(FamilyError):
        result("text_generation", {"tool_calls": [call(arguments)]})


def test_wav_duration_is_computed_and_unknown_metrics_are_absent():
    prepared = prepare("audio_transcription", {"audio": wav()})
    assert prepared.resources == {"requests": 1, "audio_seconds": 2}
    compressed = media("audio", "audio/ogg", b"OggS" + b"\0" * 23)
    prepared = prepare("audio_transcription", {"audio": compressed})
    assert "audio_seconds" not in prepared.resources
    assert "input_tokens" not in prepared.resources and "total_tokens" not in prepared.resources
    pdf = pdf_media()
    prepared = prepare("ocr", {"document": pdf})
    assert prepared.resources == {"requests": 1, "conversions": 1, "pages": 1}
    with pytest.raises(FamilyError):
        prepare("audio_transcription", {"audio": wav(), "duration": 1})
    with pytest.raises(FamilyError):
        prepare("ocr", {"document": pdf, "pages": 1})


def test_mixed_unknown_audio_duration_is_not_underestimated():
    prepared = prepare(
        "text_generation", messages([wav(), media("audio", "audio/ogg", b"OggS" + b"\0" * 23)])
    )
    assert "audio_seconds" not in prepared.resources


def test_generated_image_count_is_reserved():
    assert prepare("image_generation", {"prompt": "image"}, {"n": 3}).resources["images"] == 3
    assert prepare("ocr", {"document": media()}).resources["images"] == 1


@pytest.mark.parametrize(
    "value",
    [
        {"audio": {"type": "audio", "url": "https://example.invalid/a.wav"}},
        {"audio": {"type": "audio", "path": "/tmp/audio.wav"}},
        {"audio": {"type": "audio", "mime_type": "audio/wav", "data": ""}},
        {"audio": {"type": "audio", "mime_type": "audio/wav", "data": "AAAA"}},
        {"audio": {**wav(), "duration": 1}},
    ],
)
def test_inline_media_only_and_no_caller_metrics(value):
    with pytest.raises(FamilyError):
        prepare("audio_transcription", value)


@pytest.mark.parametrize(
    "value",
    [
        {"messages": [{"role": "user", "content": [{**media(), "mime_type": "image/svg+xml"}]}]},
        {"messages": [{"role": "user", "content": [{**media(), "mime_type": "image/jpeg"}]}]},
        messages([{**media(), "data": "AAAA\nAAA="}]),
        messages([{**media(), "data": "AA=A"}]),
        messages([{**media(), "data": "AB=="}]),
        messages([]),
        {"messages": []},
        {"messages": [{"role": "invalid", "content": "hi"}]},
        {"messages": [{"role": "tool", "content": "result"}]},
        {"messages": [{"role": "user", "content": "hello", "url": "https://example.invalid"}]},
    ],
)
def test_bad_message_or_media_shapes(value):
    with pytest.raises(FamilyError):
        prepare("text_generation", value)


def test_part_and_aggregate_byte_limits_apply_before_any_base64_decoding(monkeypatch):
    def unexpected_decode(*args, **kwargs):
        pytest.fail("allocation happened before aggregate validation")

    monkeypatch.setattr(base64, "b64decode", unexpected_decode)
    image = media()
    # media() only encodes and is unaffected by the decoding sentinel.
    with pytest.raises(FamilyError, match="exceeds limit"):
        prepare("vision", messages([image, image]), limits=replace(LIMITS, decoded_input_bytes=40))
    with pytest.raises(FamilyError):
        prepare("vision", messages([image, image]), limits=replace(LIMITS, max_parts=1))


def test_truncated_wav_is_rejected():
    part = wav()
    raw = base64.b64decode(part["data"])
    part["data"] = base64.b64encode(raw[:-2]).decode()
    with pytest.raises(FamilyError, match="invalid media"):
        prepare("audio_transcription", {"audio": part})


def test_request_result_and_json_depth_bounds():
    with pytest.raises(FamilyError, match="exceeds limit"):
        prepare("text_generation", messages("a" * 100), limits=replace(LIMITS, request_bytes=99))
    with pytest.raises(FamilyError, match="exceeds limit"):
        result("text_generation", {"text": "a" * 100}, replace(LIMITS, result_bytes=99))
    schema = {"type": "object"}
    for _ in range(20):
        schema = {"items": schema}
    with pytest.raises(FamilyError, match="exceeds limit"):
        prepare("text_generation", messages(), {"json_schema": schema})


@pytest.mark.parametrize(
    "capability,value",
    [
        ("embedding", {"vectors": [[float("nan")]]}),
        ("embedding", {"vectors": [[float("inf")]]}),
        ("embedding", {"vectors": [[True]]}),
        ("embedding", {"vectors": [[0.1], [0.1, 0.2]]}),
        ("rerank", {"scores": [{"index": -1, "score": 0.1}]}),
        ("rerank", {"scores": [{"index": 0, "score": 0.1}, {"index": 0, "score": 0.2}]}),
        ("classification", {"classes": [[{"label": "x", "score": float("inf")}]]}),
        ("moderation", {"results": [{"flagged": 1, "categories": {}, "scores": {}}]}),
        ("moderation", {"results": [{"flagged": False, "categories": {"x": False}, "scores": {}}]}),
        ("tts", {"audio": media()}),
        ("image_generation", {"images": [{"url": "https://example.invalid/a.png"}]}),
        ("text_generation", {"text": "ok", "provider_payload": {}}),
    ],
)
def test_results_are_typed_finite_and_no_provider_extras(capability, value):
    with pytest.raises(FamilyError):
        result(capability, value)


def test_empty_and_oversized_result_arrays_rejected():
    with pytest.raises(FamilyError):
        result("embedding", {"vectors": []})
    with pytest.raises(FamilyError):
        result("embedding", {"vectors": [[0.1]] * 33})
    with pytest.raises(FamilyError):
        result("image_generation", {"images": [media()] * 33})


def test_safe_errors_never_echo_caller_content():
    secret_marker = "caller-private-marker"
    with pytest.raises(FamilyError) as exc:
        prepare("text_generation", messages(), {secret_marker: secret_marker})
    assert secret_marker not in str(exc.value)
    assert str(FamilyError(secret_marker)) == "invalid family payload"


def pdf_media(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt("fixture-only")
    buffer = io.BytesIO()
    writer.write(buffer)
    return media("document", "application/pdf", buffer.getvalue())


def test_pdf_pages_are_trusted_local_estimates_and_encrypted_or_invalid_rejected():
    assert prepare("ocr", {"document": pdf_media(3)}).resources["pages"] == 3
    assert "document_input" in prepare("ocr", {"document": pdf_media()}).required_features
    for part in (
        pdf_media(encrypted=True),
        pdf_media(129),
        media("document", "application/pdf", b"%PDF-1.7\n%%EOF"),
    ):
        with pytest.raises(FamilyError, match="invalid media"):
            prepare("ocr", {"document": part})


def test_pdf_child_timeout_is_safe_and_never_logs_document(monkeypatch):
    part = pdf_media()

    def timeout(argv, **kwargs):
        assert argv[1:3] == ["-I", "-c"]
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        assert isinstance(kwargs["input"], bytes)
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(FamilyError, match="invalid media"):
        prepare("ocr", {"document": part})


@pytest.mark.parametrize(
    "code,stdout", [(1, b"1"), (0, b"0"), (0, b"129"), (0, b"private-doc"), (0, b"1\nextra")]
)
def test_pdf_child_failure_or_untrusted_output_is_rejected(monkeypatch, code, stdout):
    part = pdf_media()
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], code, stdout)
    )
    with pytest.raises(FamilyError, match="invalid media"):
        prepare("ocr", {"document": part})


@pytest.mark.parametrize(
    "schema",
    [
        {"unknown_keyword": True},
        {"type": "bogus"},
        {"type": ["string", "string"]},
        {"properties": []},
        {"required": [1]},
        {"$ref": "https://example.invalid/schema"},
        {"minimum": 2, "maximum": 1},
        {"items": 1},
        {"enum": [1, 1]},
        {"multipleOf": 0},
        {"uniqueItems": 1},
        {"additionalProperties": []},
    ],
)
def test_json_schema_keywords_types_and_remote_refs_are_strict(schema):
    with pytest.raises(FamilyError):
        prepare("text_generation", messages(), {"json_schema": schema})


def test_json_schema_supported_subset_is_preserved():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "maxLength": 10}},
        "required": ["name"],
        "additionalProperties": False,
        "$defs": {"color": {"enum": ["red", "blue"]}},
    }
    prepared = prepare("text_generation", messages(), {"json_schema": schema})
    assert prepared.options["json_schema"] == schema


@pytest.mark.parametrize(
    "capability,value,options",
    [
        ("tts", {"text": "hello"}, {"format": "unknown"}),
        ("tts", {"text": "hello"}, {"speed": 0}),
        ("image_generation", {"prompt": "image"}, {"size": "huge"}),
        ("image_generation", {"prompt": "image"}, {"format": "svg"}),
        ("audio_transcription", {"audio": wav()}, {"language": "caller-unknown-value"}),
        ("embedding", {"texts": ["hi"]}, {"dimensions": 0}),
        ("classification", {"texts": ["hi"]}, {"labels": ["a", "a"]}),
        ("rerank", {"query": "q", "documents": ["a"]}, {"top_n": 2}),
    ],
)
def test_nontext_family_options_have_semantic_bounds(capability, value, options):
    with pytest.raises(FamilyError):
        prepare(capability, value, options)


def test_aggregate_serialized_result_bound_has_safe_limit_message():
    with pytest.raises(FamilyError, match="exceeds limit"):
        result("embedding", {"vectors": [[0.123] * 10]}, replace(LIMITS, result_bytes=40))


def test_all_finite_vector_values_are_accepted():
    assert result("embedding", {"vectors": [[1.7e308]]}) == {"vectors": [[1.7e308]]}


@pytest.mark.parametrize("input_type", ["query", "passage"])
def test_embedding_input_type_is_explicit_and_preserved(input_type):
    prepared = prepare("embedding", {"texts": ["text"]}, {"input_type": input_type})
    assert prepared.options["input_type"] == input_type
    assert "input_type" not in prepare("embedding", {"texts": ["text"]}).options


@pytest.mark.parametrize("input_type", ["document", "auto", True, [], None])
def test_embedding_input_type_unknown_or_nonstring_is_rejected(input_type):
    with pytest.raises(FamilyError):
        prepare("embedding", {"texts": ["text"]}, {"input_type": input_type})


def test_code_suffix_is_independent_bounded_and_not_accepted_as_chat_option():
    value = messages("prefix")
    for suffix in ("tail", ""):
        prepared = prepare("code_completion", value, {"suffix": suffix})
        assert prepared.options["suffix"] == suffix
        assert prepared.input == value
    with pytest.raises(FamilyError):
        prepare("text_generation", value, {"suffix": "tail"})
    with pytest.raises(FamilyError):
        prepare("code_completion", value, {"suffix": "x" * 65537})
    with pytest.raises(FamilyError):
        prepare("code_completion", value, {"suffix": 1})
    with pytest.raises(FamilyError):
        prepare("code_completion", {"prompt": "prefix"}, {"suffix": "tail"})


@pytest.mark.parametrize("name", ["detect_orientation", "scale", "is_table", "overlay"])
def test_ocr_boolean_options_preserved_and_nonbool_rejected(name):
    for boolean in (True, False):
        assert prepare("ocr", {"document": media()}, {name: boolean}).options[name] is boolean
    with pytest.raises(FamilyError):
        prepare("ocr", {"document": media()}, {name: 1})
    with pytest.raises(FamilyError):
        prepare("ocr", {"document": media()}, {name: "true"})


def test_typed_text_translation_has_distinct_strict_language_identifiers():
    options = {"source_language": "en", "target_language": "zh-tw"}
    prepared = prepare("translation", {"text": "hello"}, options)
    assert prepared.required_features == ("text", "translation")
    assert prepared.input == {"text": "hello"} and prepared.options == options
    assert "output_tokens" not in prepared.resources and "total_tokens" not in prepared.resources
    assert result("translation", {"text": "你好"}) == {"text": "你好"}
    for bad in (
        {},
        {"source_language": "en"},
        {"source_language": "EN", "target_language": "en"},
        {"source_language": "en_US", "target_language": "zh"},
        {"source_language": "https://example.invalid", "target_language": "zh"},
    ):
        with pytest.raises(FamilyError):
            prepare("translation", {"text": "hello"}, bad)
    with pytest.raises(FamilyError):
        prepare("translation", "legacy-string", options)
    with pytest.raises(FamilyError):
        result("translation", {"text": "hello", "language": "en"})


def correspondence(capability, value, output, options=None, resources=None):
    REGISTRY.validate_correspondence(capability, value, options or {}, output, resources)


@pytest.mark.parametrize(
    "capability,value,output",
    [
        ("embedding", {"texts": ["a", "b"]}, {"vectors": [[0.1], [0.2]]}),
        ("classification", {"texts": ["a"]}, {"classes": [[{"label": "a", "score": 1}]]}),
        (
            "moderation",
            {"texts": ["a"]},
            {"results": [{"flagged": False, "categories": {"a": False}, "scores": {"a": 0}}]},
        ),
        (
            "rerank",
            {"query": "a", "documents": ["a", "b"]},
            {"scores": [{"index": 1, "score": 0.2}]},
        ),
        ("image_generation", {"prompt": "image"}, {"images": [media()]}),
        ("ocr", {"document": media()}, {"pages": [{"text": "text", "page": 1}]}),
        ("audio_transcription", {"audio": wav()}, {"text": ""}),
        ("text_generation", messages(), {"tool_calls": [call()]}),
    ],
)
def test_correspondence_accepts_matching_counts_and_empty_asr_or_tool_only_result(
    capability, value, output
):
    normalized = result(capability, output)
    correspondence(capability, value, normalized)


@pytest.mark.parametrize(
    "capability,value,output,options",
    [
        ("embedding", {"texts": ["a", "b"]}, {"vectors": [[0.1]]}, {}),
        ("embedding", {"texts": ["a"]}, {"vectors": [[0.1, 0.2]]}, {"dimensions": 1}),
        ("classification", {"texts": ["a", "b"]}, {"classes": [[{"label": "a", "score": 1}]]}, {}),
        (
            "moderation",
            {"texts": ["a", "b"]},
            {"results": [{"flagged": False, "categories": {"a": False}, "scores": {"a": 0}}]},
            {},
        ),
        ("rerank", {"documents": ["a"]}, {"scores": [{"index": 1, "score": 0.2}]}, {}),
        (
            "rerank",
            {"documents": ["a", "b"]},
            {"scores": [{"index": 0, "score": 0.2}, {"index": 0, "score": 0.3}]},
            {},
        ),
        (
            "rerank",
            {"documents": ["a", "b"]},
            {"scores": [{"index": 0, "score": 0.2}, {"index": 1, "score": 0.3}]},
            {"top_n": 1},
        ),
        ("image_generation", {"prompt": "image"}, {"images": [media(), media()]}, {}),
        ("ocr", {"document": media()}, {"pages": [{"text": "a"}, {"text": "b"}]}, {}),
        ("ocr", {"document": media()}, {"pages": [{"text": "a", "page": 2}]}, {}),
    ],
)
def test_correspondence_rejects_count_dimension_index_or_output_bound_mismatch(
    capability, value, output, options
):
    with pytest.raises(FamilyError, match="invalid family result"):
        correspondence(capability, value, output, options)


def test_correspondence_pdf_uses_optional_trusted_prepared_page_bound():
    part = pdf_media(2)
    output = {"pages": [{"text": "a", "page": 1}, {"text": "b", "page": 2}]}
    correspondence("ocr", {"document": part}, output, resources={"pages": 2})
    with pytest.raises(FamilyError):
        correspondence("ocr", {"document": part}, output, resources={"pages": 1})
    with pytest.raises(FamilyError):
        correspondence(
            "ocr",
            {"document": part},
            {"pages": [{"text": "a", "page": 1}, {"text": "b", "page": 1}]},
        )


def test_correspondence_registry_dispatches_registered_class_and_rejects_unknown_family():
    from quota_broker.families import FamilyContract

    class FixtureContract(FamilyContract):
        @classmethod
        def correspondence(cls, value, options, result, resources):
            assert value == {"fixture": True}

    registry = FamilyRegistry({"fixture": FixtureContract})
    registry.validate_correspondence("fixture", {"fixture": True}, {}, {})
    with pytest.raises(FamilyError, match="unsupported family"):
        registry.validate_correspondence("unknown", {}, {}, {})


@pytest.mark.parametrize("capability", ["text_generation", "vision", "code_completion"])
def test_only_token_output_families_expose_token_cap_hook(capability):
    assert REGISTRY.uses_output_tokens(capability) is True
    with pytest.raises(FamilyError):
        REGISTRY.prepare(capability, messages(), {}, LIMITS, 0)


@pytest.mark.parametrize(
    "capability,value",
    [
        ("translation", {"text": "hello"}),
        ("ocr", {"document": media()}),
        ("audio_transcription", {"audio": wav()}),
        ("audio_translation", {"audio": wav()}),
        ("tts", {"text": "hello"}),
        ("image_generation", {"prompt": "image"}),
        ("embedding", {"texts": ["hello"]}),
        ("rerank", {"query": "q", "documents": ["a"]}),
        ("classification", {"texts": ["hello"]}),
        ("moderation", {"texts": ["hello"]}),
    ],
)
def test_non_token_output_families_accept_zero_without_inventing_output_tokens(capability, value):
    assert REGISTRY.uses_output_tokens(capability) is False
    options = (
        {"source_language": "en", "target_language": "zh"} if capability == "translation" else {}
    )
    prepared = REGISTRY.prepare(capability, value, options, LIMITS, 0)
    assert "output_tokens" not in prepared.resources and "total_tokens" not in prepared.resources


def overlay_fixture():
    return {
        "has_overlay": True,
        "lines": [
            {
                "words": [{"text": "你好", "left": 106, "top": 91, "width": 11.5, "height": 9}],
                "max_height": 13,
                "min_top": 90,
            }
        ],
        "message": "",
    }


def test_ocr_overlay_preserves_complete_coordinates_and_optional_message():
    value = {"pages": [{"text": "你好", "page": 1, "overlay": overlay_fixture()}]}
    normalized = result("ocr", value)
    assert normalized == value
    normalized["pages"][0]["overlay"]["lines"][0]["words"][0]["text"] = "changed"
    assert value["pages"][0]["overlay"]["lines"][0]["words"][0]["text"] == "你好"
    assert (
        result("ocr", {"pages": [{"text": "", "overlay": {"has_overlay": False, "lines": []}}]})[
            "pages"
        ][0]["overlay"]["lines"]
        == []
    )


@pytest.mark.parametrize("name", ["left", "top", "width", "height"])
@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "1"])
def test_ocr_overlay_word_geometry_requires_finite_nonnegative_numbers(name, value):
    overlay = overlay_fixture()
    overlay["lines"][0]["words"][0][name] = value
    with pytest.raises(FamilyError):
        result("ocr", {"pages": [{"text": "hello", "overlay": overlay}]})


@pytest.mark.parametrize(
    "name,value",
    [("max_height", -1), ("min_top", float("inf")), ("max_height", True), ("min_top", "0")],
)
def test_ocr_overlay_line_geometry_is_strict(name, value):
    overlay = overlay_fixture()
    overlay["lines"][0][name] = value
    with pytest.raises(FamilyError):
        result("ocr", {"pages": [{"text": "hello", "overlay": overlay}]})


@pytest.mark.parametrize(
    "overlay",
    [
        {"has_overlay": 1, "lines": []},
        {"has_overlay": True, "lines": [], "message": None},
        {"has_overlay": True, "lines": [], "url": "https://example.invalid"},
        {"has_overlay": True, "lines": [{"words": [], "max_height": 1}]},
    ],
)
def test_ocr_overlay_unknown_fields_and_wrong_types_are_rejected(overlay):
    with pytest.raises(FamilyError):
        result("ocr", {"pages": [{"text": "hello", "overlay": overlay}]})


def test_ocr_overlay_has_document_sized_array_bounds_and_aggregate_word_bound():
    line = overlay_fixture()["lines"][0]
    overlay = {"has_overlay": True, "lines": [line] * 33}
    assert (
        len(
            result("ocr", {"pages": [{"text": "hello", "overlay": overlay}]})["pages"][0][
                "overlay"
            ]["lines"]
        )
        == 33
    )
    overlay["lines"] = [line] * 129
    with pytest.raises(FamilyError):
        result(
            "ocr", {"pages": [{"text": "hello", "overlay": overlay}]}, replace(LIMITS, max_parts=1)
        )
    many_words = {**line, "words": line["words"] * 32}
    overlay["lines"] = [many_words] * 100
    with pytest.raises(FamilyError):
        result(
            "ocr", {"pages": [{"text": "hello", "overlay": overlay}]}, replace(LIMITS, max_parts=1)
        )


def test_signed_tool_call_result_to_input_roundtrip_is_opaque_and_not_decoded(monkeypatch):
    signature = 'opaque/not-base64+provider-state="原樣"'
    signed_call = {**call(), "thought_signature": signature}
    monkeypatch.setattr(base64, "b64decode", lambda *a, **kw: pytest.fail("signature decoded"))
    output = {
        "messages": [{"role": "assistant", "content": "", "tool_calls": [signed_call]}],
        "tool_calls": [signed_call],
    }
    normalized = result("text_generation", output)
    prepared = prepare("text_generation", {"messages": normalized["messages"]})
    assert normalized["tool_calls"][0]["thought_signature"] == signature
    assert prepared.input["messages"][0]["tool_calls"][0]["thought_signature"] == signature
    assert "google_continuation" in prepared.required_features
    assert "thought_signature" not in prepared.resources


@pytest.mark.parametrize("signature", ["", "   ", None, 1, {}, []])
def test_thought_signature_must_be_an_opaque_nonempty_string(signature):
    signed_call = {**call(), "thought_signature": signature}
    with pytest.raises(FamilyError):
        result("text_generation", {"tool_calls": [signed_call]})


def test_signature_counts_toward_request_and_result_byte_limits():
    signed_call = {**call(), "thought_signature": "opaque" * 100}
    with pytest.raises(FamilyError):
        prepare(
            "text_generation",
            {"messages": [{"role": "assistant", "content": "", "tool_calls": [signed_call]}]},
            limits=replace(LIMITS, request_bytes=100),
        )
    with pytest.raises(FamilyError):
        result("text_generation", {"tool_calls": [signed_call]}, replace(LIMITS, result_bytes=100))


def test_signed_text_and_inline_media_parts_keep_signature_at_exact_part():
    parts = [
        {"type": "text", "text": "", "thought_signature": "opaque-text-state"},
        {**media(), "thought_signature": "opaque-image-state"},
    ]
    output = {"messages": [{"role": "assistant", "content": parts}]}
    normalized = result("text_generation", output)
    assert normalized == output
    prepared = prepare("vision", {"messages": normalized["messages"]})
    assert prepared.input == output and "google_continuation" in prepared.required_features
    with pytest.raises(FamilyError):
        prepare("text_generation", messages([{"type": "text", "text": ""}]))


def google_state_message():
    first = {
        **call('{"x":1}'),
        "provider_call_id": "provider-1",
        "thought_signature": "opaque-call-1",
    }
    second = {
        "id": "broker-call-2",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
        "provider_call_id": None,
    }
    state = {
        "provider": "google",
        "model": "gemini-fixture",
        "parts": [
            {
                "text": "private model thought",
                "thought": True,
                "thoughtSignature": "opaque-thought",
            },
            {
                "functionCall": {"name": "lookup", "args": {"x": 1}, "id": "provider-1"},
                "thoughtSignature": "opaque-call-1",
            },
            {"text": "visible answer", "thoughtSignature": "opaque-text"},
            {"functionCall": {"name": "lookup"}},
        ],
    }
    return {
        "role": "assistant",
        "content": "visible answer",
        "tool_calls": [first, second],
        "provider_state": state,
    }


def test_google_provider_state_roundtrip_preserves_thought_interleaving_and_missing_call_fields():
    message = google_state_message()
    normalized = result(
        "text_generation", {"messages": [message], "tool_calls": message["tool_calls"]}
    )
    prepared = prepare("text_generation", {"messages": normalized["messages"]})
    assert normalized["messages"][0]["provider_state"] == message["provider_state"]
    assert prepared.input["messages"][0] == message
    assert [
        next(iter(part)) for part in prepared.input["messages"][0]["provider_state"]["parts"]
    ] == ["text", "functionCall", "text", "functionCall"]
    assert "google_continuation" in prepared.required_features
    assert "provider_state" not in prepared.resources
    assert message["provider_state"]["parts"][3]["functionCall"] == {"name": "lookup"}


def test_google_state_visible_parts_and_media_are_validated_without_double_resource_count(
    monkeypatch,
):
    image = media()
    text = {"type": "text", "text": "visible", "thought_signature": "opaque-text"}
    image["thought_signature"] = "opaque-image"
    message = {
        "role": "assistant",
        "content": [text, image],
        "tool_calls": [{**call(), "provider_call_id": None}],
        "provider_state": {
            "provider": "google",
            "model": "gemini-fixture",
            "parts": [
                {"text": "visible", "thoughtSignature": "opaque-text"},
                {
                    "inlineData": {"mimeType": image["mime_type"], "data": image["data"]},
                    "thoughtSignature": "opaque-image",
                },
                {"functionCall": {"name": "lookup"}},
            ],
        },
    }
    original_decode = base64.b64decode
    decodes = []

    def decode(data, **kwargs):
        decodes.append(data)
        return original_decode(data, **kwargs)

    monkeypatch.setattr(base64, "b64decode", decode)
    limits = replace(LIMITS, max_parts=3, decoded_input_bytes=33)
    prepared = prepare("vision", {"messages": [message]}, limits=limits)
    assert prepared.resources["images"] == 1
    assert decodes == [image["data"]]
    assert prepared.input["messages"][0] == message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(content="changed"),
        lambda m: m["tool_calls"][0]["function"].update(name="different"),
        lambda m: m["tool_calls"][0]["function"].update(arguments='{"x":true}'),
        lambda m: m["tool_calls"][0].update(provider_call_id="different"),
        lambda m: m["tool_calls"][0].update(thought_signature="different"),
        lambda m: m["provider_state"].update(provider="other"),
        lambda m: m["provider_state"].update(model="unsafe/model"),
        lambda m: m["provider_state"]["parts"][0].update(thought=1),
        lambda m: m["provider_state"]["parts"][0].update(thoughtSignature=""),
        lambda m: m["provider_state"]["parts"][0].update(functionResponse={}),
        lambda m: m["provider_state"]["parts"][0].update(functionCall={"name": "lookup"}),
        lambda m: m["provider_state"]["parts"][1]["functionCall"].update(args=[]),
        lambda m: m.update(role="user"),
    ],
)
def test_google_provider_state_rejects_invalid_parts_or_mismatched_convenience_view(mutate):
    message = google_state_message()
    mutate(message)
    with pytest.raises(FamilyError):
        prepare("text_generation", {"messages": [message]})


def test_google_state_bounds_and_unknown_executable_parts_fail_closed():
    message = google_state_message()
    with pytest.raises(FamilyError):
        prepare("text_generation", {"messages": [message]}, limits=replace(LIMITS, max_parts=3))
    message["provider_state"]["parts"] = [
        {"executableCode": {"language": "PYTHON", "code": "print(1)"}}
    ]
    with pytest.raises(FamilyError):
        prepare("text_generation", {"messages": [message]})


def test_classification_targets_and_labels_are_independent_with_finite_general_scores():
    value = {
        "classes": [
            [
                {"target": "topic", "label": "same", "score": 87},
                {"target": "sentiment", "label": "same", "score": -2.5},
            ]
        ]
    }
    assert result("classification", value) == value
    for invalid in (float("inf"), float("nan"), True, "1"):
        with pytest.raises(FamilyError):
            result(
                "classification",
                {"classes": [[{"target": "topic", "label": "same", "score": invalid}]]},
            )
    with pytest.raises(FamilyError):
        result("classification", {"classes": [[{"target": 1, "label": "same", "score": 1}]]})
