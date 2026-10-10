"""Pure, conservative normalization of official model-list metadata.

No HTTP, credentials, account eligibility, or inference lives here. Unknown
capabilities remain visible, and human display names/descriptions never persist.
Schemas: ai.google.dev/api/models; docs.mistral.ai/api/endpoint/models;
console.groq.com/docs/api-reference; developers.cloudflare.com/workers-ai/models/;
openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .discovery import DiscoveryError
from .provider_policy import catalog_model_id_reason

MODEL_FIELDS = (
    "model",
    "capability",
    "hosting",
    "endpoint",
    "protocol",
    "free_eligibility",
    "free_source",
    "status",
    "context_tokens",
    "max_output_tokens",
    "features",
)
SOURCES = {
    "nvidia": "https://integrate.api.nvidia.com/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "cloudflare": "https://developers.cloudflare.com/workers-ai/models/",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "ocrspace": "https://ocr.space/ocrapi",
}
MAX_MODELS = 10_000


class ParserError(DiscoveryError):
    """Fixed diagnostics, never a reflection of provider-controlled content."""

    phase = "parse_models"

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__("invalid_request", "invalid official model metadata")
        self.reason = (
            reason
            if reason
            in {
                "schema",
                "timestamp",
                "model_id",
                "model_id_type",
                "model_id_empty",
                "model_id_length",
                "model_id_characters",
                "model_id_url",
                "model_id_secret_pattern",
                "model_id_provider_format",
                "context_bound",
                "collection_schema",
                "item_schema",
                "options",
                "duplicate_model",
                "row_count",
            }
            else "schema"
        )
        self.detail = (
            detail
            if detail
            in {
                "type",
                "empty",
                "length",
                "characters",
                "path",
                "provider_format",
                "url",
                "secret_pattern",
            }
            else None
        )


def _invalid(reason: str = "schema", detail: str | None = None) -> ParserError:
    return ParserError(reason, detail)


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _invalid("timestamp")
    return value.astimezone(UTC).isoformat()


def _id(value: object) -> str:
    reason = catalog_model_id_reason(value)
    if reason is not None:
        raise _invalid("model_id_" + reason, reason)
    assert isinstance(value, str)
    return value


def _tokens(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 10**8 else None


def _row(
    model: str,
    capability: str = "unknown",
    *,
    endpoint: str | None = None,
    protocol: str | None = None,
    context: object = None,
    output: object = None,
    features: set[str] | None = None,
    free: str = "unknown",
    free_source: str | None = None,
) -> dict[str, Any]:
    context_tokens, output_tokens = _tokens(context), _tokens(output)
    if context_tokens is not None and output_tokens is not None and output_tokens > context_tokens:
        raise _invalid("context_bound")
    return {
        "model": model,
        "capability": capability,
        "hosting": "hosted",
        "endpoint": endpoint,
        "protocol": protocol,
        "free_eligibility": free,
        "free_source": free_source,
        "status": "listed",
        "context_tokens": context_tokens,
        "max_output_tokens": output_tokens,
        "features": sorted(features or set()),
    }


def _collection(provider: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    key = "models" if provider == "google" else "result" if provider == "cloudflare" else "data"
    if raw.get("error") or raw.get("errors") or raw.get("success") is False:
        raise _invalid("collection_schema")
    values = raw.get(key)
    if not isinstance(values, list) or len(values) > MAX_MODELS:
        raise _invalid("collection_schema")
    if provider == "cloudflare" and raw.get("success") is not True:
        raise _invalid("collection_schema")
    if any(not isinstance(item, dict) for item in values):
        raise _invalid("item_schema")
    return values


def _complete(provider: str, raw: dict[str, Any], count: int, output_modalities: str) -> bool:
    if not count or (provider == "openrouter" and output_modalities != "all"):
        return False
    for name in ("nextPageToken", "next_page_token", "next_cursor", "next", "previous", "prev"):
        if raw.get(name) not in (None, ""):
            return False
    for name in ("has_more", "hasMore"):
        if name in raw and raw[name] is not False:
            return False
    links = raw.get("links")
    if links is not None:
        if not isinstance(links, dict):
            return False
        if any(links.get(key) not in (None, "") for key in ("next", "previous", "prev")):
            return False
    if "offset" in raw and (type(raw["offset"]) is not int or raw["offset"] != 0):
        return False
    for name in ("total_count", "total"):
        if name in raw and (type(raw[name]) is not int or raw[name] != count):
            return False
    pagination = raw.get("pagination")
    if pagination is not None and (
        not isinstance(pagination, dict)
        or not _complete("nvidia", {**pagination, "pagination": None}, count, "all")
    ):
        return False
    if "limit" in raw and "total_count" not in raw and "total" not in raw:
        limit = raw["limit"]
        if type(limit) is not int or limit < 1 or count >= limit:
            return False
    if provider == "cloudflare":
        info = raw.get("result_info")
        if not isinstance(info, dict):
            return False
        if any(type(info.get(key)) is not int for key in ("page", "total_pages", "total_count")):
            return False
        if info["page"] != 1 or info["total_pages"] != 1 or info["total_count"] != count:
            return False
    return True


def _nvidia(item: dict[str, Any]) -> list[dict[str, Any]]:
    # The public /v1/models list does not describe per-model tasks or protocols.
    # In particular, a name containing "embed", "vision", or "riva" is not proof.
    return [_row(_id(item.get("id")))]


def _groq(item: dict[str, Any]) -> list[dict[str, Any]]:
    features = {"inactive"} if item.get("active") is False else set()
    # Groq's model list mixes chat and speech models and has no task discriminator.
    return [
        _row(
            _id(item.get("id")),
            context=item.get("context_window"),
            output=item.get("max_completion_tokens"),
            features=features,
        )
    ]


MISTRAL_FAMILIES = {
    "completion_chat": ("text_generation", "openai_inference", "/v1/chat/completions"),
    "completion_fim": ("code_completion", "mistral_fim", "/v1/fim/completions"),
    "ocr": ("ocr", "mistral_ocr", "/v1/ocr"),
    "classification": ("classification", "mistral_classification", "/v1/classifications"),
    "moderation": ("moderation", "mistral_moderation", "/v1/moderations"),
    "audio_transcription": (
        "audio_transcription",
        "mistral_audio_transcription",
        "/v1/audio/transcriptions",
    ),
    "audio_transcription_realtime": (
        "realtime_audio_transcription",
        "mistral_audio_realtime",
        None,
    ),
    "audio_speech": ("tts", "mistral_tts", "/v1/audio/speech"),
}
MISTRAL_FEATURES = {
    "vision": {"vision", "image_input"},
    "audio": {"audio_input"},
    "function_calling": {"tool_calling"},
    "reasoning": {"reasoning"},
}


def _mistral(item: dict[str, Any], checked_at: datetime) -> list[dict[str, Any]]:
    model = _id(item.get("id"))
    capabilities = item.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    features = {"inactive"} if item.get("archived") is True else set()
    for key, supported in MISTRAL_FEATURES.items():
        if capabilities.get(key) is True:
            features.update(supported)
    deprecation = item.get("deprecation")
    if isinstance(deprecation, str):
        try:
            instant = datetime.fromisoformat(deprecation)
            if instant.tzinfo is not None:
                features.add("deprecated" if instant <= checked_at else "deprecation_scheduled")
        except ValueError:
            pass
    rows = []
    for flag, (family, protocol, path) in MISTRAL_FAMILIES.items():
        if capabilities.get(flag) is True:
            rows.append(
                _row(
                    model,
                    family,
                    endpoint="https://api.mistral.ai" + path if path else None,
                    protocol=protocol,
                    context=item.get("max_context_length"),
                    features=features,
                )
            )
    known_flags = (
        set(MISTRAL_FAMILIES) | set(MISTRAL_FEATURES) | {"fine_tuning", "unified_resources"}
    )
    if any(flag not in known_flags and value is True for flag, value in capabilities.items()):
        rows.append(
            _row(
                model,
                context=item.get("max_context_length"),
                features=features | {"unrecognized_capability"},
            )
        )
    return rows or [_row(model, context=item.get("max_context_length"), features=features)]


GEMINI_METHODS = {
    "generateContent": ("text_generation", "gemini_inference"),
    "generateMessage": ("text_generation", "gemini_generate_message"),
    "generateText": ("text_generation", "gemini_generate_text"),
    "embedContent": ("embedding", "gemini_embeddings"),
    "embedText": ("embedding", "gemini_embed_text"),
}


def _google(item: dict[str, Any]) -> list[dict[str, Any]]:
    name = item.get("name")
    if not isinstance(name, str) or not re.fullmatch(
        r"models/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name
    ):
        raise _invalid("model_id_provider_format", "provider_format")
    model = _id(name.removeprefix("models/"))
    methods = item.get("supportedGenerationMethods")
    if not isinstance(methods, list):
        methods = []
    features = {"reasoning"} if item.get("thinking") is True else set()
    if "countTokens" in methods:
        features.add("token_counting")
    if "predict" in methods:
        features.add("predict")  # Method alone does not prove image/audio generation.
    rows: dict[str, dict[str, Any]] = {}
    for method, (family, protocol) in GEMINI_METHODS.items():
        if method in methods and family not in rows:
            rows[family] = _row(
                model,
                family,
                endpoint=f"https://generativelanguage.googleapis.com/v1beta/{name}:{method}",
                protocol=protocol,
                context=item.get("inputTokenLimit"),
                output=item.get("outputTokenLimit"),
                features=features,
            )
    auxiliary = {"countTokens", "batchGenerateContent", "batchEmbedContents"}
    if rows and any(
        isinstance(method, str) and method not in GEMINI_METHODS and method not in auxiliary
        for method in methods
    ):
        rows["unknown"] = _row(
            model,
            context=item.get("inputTokenLimit"),
            output=item.get("outputTokenLimit"),
            features=features | {"unrecognized_generation_method"},
        )
    return list(rows.values()) or [
        _row(
            model,
            context=item.get("inputTokenLimit"),
            output=item.get("outputTokenLimit"),
            features=features,
        )
    ]


CLOUDFLARE_TASKS = {
    "Text Generation": "text_generation",
    "Text Embeddings": "embedding",
    "Text Classification": "classification",
    "Image Classification": "image_classification",
    "Object Detection": "object_detection",
    "Text-to-Image": "image_generation",
    "Text-to-Speech": "tts",
    "Automatic Speech Recognition": "audio_transcription",
    "Translation": "translation",
    "Image-to-Text": "vision",
}


def _cloudflare(item: dict[str, Any]) -> list[dict[str, Any]]:
    # Workers AI uses its canonical @cf/... ``name`` as the inference model ID;
    # the response's UUID ``id`` and human description are not routing identities.
    model = _id(item.get("name"))
    task = item.get("task")
    task_name = task.get("name") if isinstance(task, dict) else None
    family = CLOUDFLARE_TASKS.get(task_name, "unknown") if isinstance(task_name, str) else "unknown"
    features = {"deprecated"} if item.get("deprecated") is True else set()
    # Task labels prove a family, not its wire schema: multiple Workers AI models
    # in a task use incompatible inputs/outputs. Selecting a packaged profile
    # requires model-specific official schema evidence beyond this listing.
    protocol = None
    features.add("protocol_schema_unknown")
    endpoint = None
    if re.fullmatch(r"@cf/[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*", model):
        endpoint = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/" + model
    return [_row(model, family, endpoint=endpoint, protocol=protocol, features=features)]


MODALITIES = {
    "text",
    "image",
    "audio",
    "video",
    "file",
    "embeddings",
    "rerank",
    "speech",
    "transcription",
    "decisions",
}
OUTPUT_FAMILIES = {
    "text": (
        "text_generation",
        "openai_inference",
        "https://openrouter.ai/api/v1/chat/completions",
    ),
    "image": (
        "image_generation",
        "openrouter_image_generation",
        "https://openrouter.ai/api/v1/images",
    ),
    "audio": ("audio_generation", "openrouter_audio_generation", None),
    "speech": ("tts", "openrouter_tts", None),
    "transcription": ("audio_transcription", "openrouter_audio_transcription", None),
    "video": ("video_generation", "openrouter_video_generation", None),
    "embeddings": ("embedding", "openai_embeddings", "https://openrouter.ai/api/v1/embeddings"),
    "rerank": ("rerank", "openrouter_rerank", None),
    "decisions": ("decisions", "openrouter_decisions", None),
}
OPENROUTER_PARAMETERS = {
    "tools": "tool_calling",
    "reasoning": "reasoning",
    "reasoning_effort": "reasoning",
    "response_format": "json_output",
    "structured_outputs": "structured_output",
}


def _pricing(value: object) -> str:
    if not isinstance(value, dict) or not {"prompt", "completion"} <= value.keys():
        return "unknown"
    prices = []
    for field in value.values():
        if not isinstance(field, (str, int, float)) or isinstance(field, bool):
            return "unknown"
        try:
            price = Decimal(str(field))
        except InvalidOperation:
            return "unknown"
        if not price.is_finite():
            return "unknown"
        prices.append(price)
    if any(price > 0 for price in prices):
        return "paid"
    if any(price < 0 for price in prices):
        return "unknown"  # Dynamic-price sentinels are not zero pricing evidence.
    return "free"


def _openrouter(item: dict[str, Any]) -> list[dict[str, Any]]:
    model = _id(item.get("id"))
    architecture = item.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    inputs, outputs = architecture.get("input_modalities"), architecture.get("output_modalities")
    # ``modality`` is an explicit legacy representation, never a model-name guess.
    legacy = architecture.get("modality")
    if isinstance(legacy, str) and legacy.count("->") == 1:
        legacy_inputs, legacy_outputs = legacy.split("->")
        if inputs is None:
            inputs = legacy_inputs.split("+")
        if outputs is None:
            outputs = legacy_outputs.split("+")
    features: set[str] = set()
    if isinstance(inputs, list):
        features.update(
            value + "_input" for value in inputs if isinstance(value, str) and value in MODALITIES
        )
        if "image" in inputs:
            features.add("vision")
    if isinstance(outputs, list):
        features.update(
            value + "_output" for value in outputs if isinstance(value, str) and value in MODALITIES
        )
    parameters = item.get("supported_parameters")
    if isinstance(parameters, list):
        features.update(
            OPENROUTER_PARAMETERS[value]
            for value in parameters
            if isinstance(value, str) and value in OPENROUTER_PARAMETERS
        )
    provider = item.get("top_provider")
    if not isinstance(provider, dict):
        provider = {}
    free = _pricing(item.get("pricing"))
    rows = []
    if isinstance(outputs, list):
        for modality, (family, protocol, endpoint) in OUTPUT_FAMILIES.items():
            if modality in outputs:
                rows.append(
                    _row(
                        model,
                        family,
                        endpoint=endpoint,
                        protocol=protocol,
                        context=item.get("context_length"),
                        output=provider.get("max_completion_tokens"),
                        features=features,
                        free=free,
                        free_source=SOURCES["openrouter"] if free != "unknown" else None,
                    )
                )
        if rows and any(
            isinstance(value, str) and value not in OUTPUT_FAMILIES for value in outputs
        ):
            rows.append(
                _row(
                    model,
                    context=item.get("context_length"),
                    output=provider.get("max_completion_tokens"),
                    features=features | {"unrecognized_output_modality"},
                    free=free,
                    free_source=SOURCES["openrouter"] if free != "unknown" else None,
                )
            )
    return rows or [
        _row(
            model,
            context=item.get("context_length"),
            output=provider.get("max_completion_tokens"),
            features=features,
            free=free,
            free_source=SOURCES["openrouter"] if free != "unknown" else None,
        )
    ]


def ocrspace_snapshot(checked_at: datetime) -> dict[str, Any]:
    """Dated official-document facts, not an API call or account attestation.

    OCR.space's current API reference declares all three engines in the Free
    plan. Engine 1 is deprecated; the wording does not prove completed retirement.
    """
    rows = []
    for engine in (1, 2, 3):
        features = {"image_input", "pdf_input", "text_output"}
        if engine == 1:
            features.add("deprecated")
        else:
            features.add("language_detection")
        if engine == 3:
            features.update({"handwriting", "table_markdown"})
        rows.append(
            _row(
                f"ocr.space/engine{engine}",
                "ocr",
                endpoint="https://api.ocr.space/parse/image",
                protocol="ocrspace_inference",
                features=features,
                free="free",
                free_source=SOURCES["ocrspace"],
            )
        )
    return {
        "schema_version": 1,
        "provider": "ocrspace",
        "source": SOURCES["ocrspace"],
        "checked_at": _time(checked_at),
        "complete": True,
        "models": rows,
    }


def parse_models(
    provider: str,
    raw: dict[str, Any],
    checked_at: datetime,
    *,
    output_modalities: str = "text",
) -> dict[str, Any]:
    """Normalize one official list page; pagination gaps always remain partial."""
    checked = _time(checked_at)
    if (
        not isinstance(provider, str)
        or provider not in SOURCES
        or not isinstance(raw, dict)
        or not isinstance(output_modalities, str)
        or output_modalities not in {"text", "all"}
    ):
        raise _invalid("options")
    if provider == "ocrspace":
        if raw:
            raise _invalid()
        return ocrspace_snapshot(checked_at)
    values = _collection(provider, raw)
    result = []
    seen_models: set[str] = set()
    seen_rows: set[tuple[str, str]] = set()
    for value in values:
        if provider == "nvidia":
            rows = _nvidia(value)
        elif provider == "groq":
            rows = _groq(value)
        elif provider == "mistral":
            rows = _mistral(value, checked_at)
        elif provider == "google":
            rows = _google(value)
        elif provider == "cloudflare":
            rows = _cloudflare(value)
        else:
            rows = _openrouter(value)
        model = rows[0]["model"]
        if model in seen_models:
            raise _invalid("duplicate_model")
        seen_models.add(model)
        for row in rows:
            identity = (row["model"], row["capability"])
            if identity in seen_rows:
                raise _invalid("duplicate_model")
            seen_rows.add(identity)
        result.extend(rows)
        if len(result) > MAX_MODELS:
            raise _invalid("row_count")
    source = SOURCES[provider]
    if provider == "openrouter" and output_modalities == "all":
        source += "?output_modalities=all"
    return {
        "schema_version": 1,
        "provider": provider,
        "source": source,
        "checked_at": checked,
        "complete": _complete(provider, raw, len(values), output_modalities),
        "models": sorted(result, key=lambda row: (row["model"], row["capability"])),
    }
