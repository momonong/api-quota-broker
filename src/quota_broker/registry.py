"""Instance-local, administrator-supplied provider catalog and fixed protocol adapters.

Manifest schema 1 references packaged or explicitly registered, tested adapters.
It is configuration, never executable code; callers cannot select a request URL.
"""

import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from .catalog import MODELS, Model, endpoint
from .family_transport import FamilyRequest, FamilyResponse, validate_family_request
from .provider_policy import catalog_model_id_reason

MAX_RESPONSE_BYTES = 262_144
RIVA_MODEL = "nvidia/riva-translate-4b-instruct-v2"
RIVA_LANGUAGES = frozenset(
    {
        "en",
        "cs",
        "da",
        "de",
        "el",
        "es-es",
        "es-us",
        "fi",
        "fr",
        "hu",
        "it",
        "lt",
        "lv",
        "nl",
        "no",
        "pl",
        "pt-pt",
        "pt-br",
        "ro",
        "ru",
        "sk",
        "sv",
        "zh-cn",
        "zh-tw",
        "ja",
        "hi",
        "ko",
        "et",
        "sl",
        "bg",
        "uk",
        "hr",
        "ar",
        "vi",
        "tr",
        "id",
        "th",
    }
)
GEMINI_PATH = "/v1beta/models/{model}:generateContent"
CLOUDFLARE_PATH = "/client/v4/accounts/{account_id}/ai/run/{model}"


class RegistryError(ValueError):
    """Invalid catalog or request outside the registered adapter contract."""


@dataclass(frozen=True)
class ModelSpec(Model):
    adapter: str = ""
    features: tuple[str, ...] = ()
    input_parameters: tuple[str, ...] = ("input",)
    output_parameters: tuple[str, ...] = ("max_output_tokens",)
    capabilities: tuple[str, ...] = ()
    family_adapters: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    adapter: str
    origin: str
    endpoint: str


class Adapter(Protocol):
    """Trusted, tested application code registered explicitly by its administrator.

    Implementations must preserve the approved URL, disable redirects, and never
    persist request content or credentials. Manifests can only name this code.
    An optional ``admit(spec, task)`` hook rejects adapter-specific input before
    credentials or quota reservations are used; rejection raises ValueError.
    Optional ``quota_rejection`` and ``quota_observations`` hooks carry tested
    provider quota semantics. Missing hooks mean unknown; generic HTTP 429 does
    not authorize retry. Observation evidence is validated before persistence.
    """

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
    ) -> tuple[str, dict[str, str], dict[str, object]]: ...

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]: ...

    def interpret(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]: ...


class _BuiltinAdapter:
    def admit(self, spec: ModelSpec, task: Mapping[str, object]) -> None:
        if spec.provider == "nvidia" and spec.model == RIVA_MODEL:
            source, target = task.get("source_language"), task.get("target_language")
            if (
                not isinstance(source, str)
                or source not in RIVA_LANGUAGES
                or not isinstance(target, str)
                or target not in RIVA_LANGUAGES
                or source == target
                or "en" not in {source, target}
            ):
                raise RegistryError("Riva translation needs a supported English language pair")
            content = task.get("input")
            if not isinstance(content, str) or len(content) > 1952:
                raise RegistryError("Riva text exceeds hosted input policy")

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        from .gateway_providers import official_request

        return official_request(
            spec.provider,
            spec.model,
            account_id,
            secret,
            content,
            max_output_tokens,
            source_language,
            target_language,
        )

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        from .gateway_providers import provider_http

        return provider_http(url, headers, payload, timeout)

    def interpret(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]:
        from .gateway_providers import interpret

        return interpret(spec.provider, status, raw, model_id=spec.model)

    def quota_rejection(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        sensitive: tuple[str, ...] = (),
    ) -> bool:
        from .gateway_providers import explicit_quota_rejection

        if _response_reflects(raw, headers, sensitive):
            return False
        return explicit_quota_rejection(spec.provider, status, headers, raw)

    def quota_observations(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        now: datetime,
        *,
        as_of: datetime | None = None,
    ) -> list[dict[str, object]]:
        if spec.provider != "groq":
            return []
        from .routing import groq_quota_observations

        return groq_quota_observations(headers, now, as_of=as_of)


def _response_reflects(
    raw: bytes,
    headers: dict[str, str],
    sensitive: tuple[str, ...],
) -> bool:
    if not any(sensitive):
        return False
    for value in sensitive:
        if value and value.encode("utf-8", errors="replace") in raw:
            return True
    strings = [*headers.keys(), *headers.values()]
    try:
        document = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        document = None
    pending = [(document, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            return True
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, dict):
            pending.extend((item, depth + 1) for item in (*value.keys(), *value.values()))
    return any(secret and secret in value for secret in sensitive for value in strings)


def _url(value: object, *, origin: bool = False) -> str:
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise RegistryError("invalid provider URL")
    if "\\" in value or "{" in value or "}" in value:
        raise RegistryError("invalid provider URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RegistryError("invalid provider URL") from exc
    if (
        not value.startswith("https://")
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or (port is not None and not 1 <= port <= 65535)
        or (origin and parsed.path)
        or (not origin and not parsed.path.startswith("/"))
    ):
        raise RegistryError("invalid provider URL")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise RegistryError(f"invalid {label}")
    return value


def _protocol_endpoint(adapter: str, origin: str, value: object) -> str:
    """Only the packaged path schemas may contain these two placeholders."""
    if adapter not in {"gemini_generate_content", "cloudflare_workers_ai"}:
        return _url(value)
    if not isinstance(value, str):
        raise RegistryError("invalid provider endpoint")
    path = GEMINI_PATH if adapter == "gemini_generate_content" else CLOUDFLARE_PATH
    if "{model}" in value:
        if value != origin + path:
            raise RegistryError("unapproved adapter endpoint schema")
    else:
        candidate = value.replace("{account_id}", "fixture")
        _url(candidate)
        if adapter == "gemini_generate_content":
            pattern = re.escape(origin + "/v1beta/models/") + r"[A-Za-z0-9._-]+:generateContent"
        else:
            pattern = re.escape(origin + "/client/v4/accounts/{account_id}/ai/run/") + (
                r"@cf/[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*"
            )
        if not re.fullmatch(pattern, value):
            raise RegistryError("unapproved adapter endpoint schema")
    _url(value.replace("{account_id}", "fixture").replace("{model}", "fixture"))
    return value


def _model_endpoint(provider: ProviderSpec, model: str) -> str:
    if provider.adapter == "gemini_generate_content":
        _identifier(model, "Gemini model id")
        expected = provider.origin + GEMINI_PATH.replace("{model}", model)
    elif provider.adapter == "cloudflare_workers_ai":
        if not re.fullmatch(
            r"@cf/[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*", model
        ):
            raise RegistryError("invalid Cloudflare model id")
        expected = provider.origin + CLOUDFLARE_PATH.replace("{model}", model)
    else:
        return provider.endpoint
    if provider.endpoint.replace("{model}", model) != expected:
        raise RegistryError("model does not match registered endpoint")
    return expected


def _fixed_endpoint(spec: ModelSpec, url: str) -> None:
    _url(url)
    expected = spec.endpoint_template
    if spec.adapter in {"builtin:cloudflare", "cloudflare_workers_ai"}:
        prefix = spec.origin + "/client/v4/accounts/"
        account = url.removeprefix(prefix).split("/", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise RegistryError("invalid Cloudflare account id")
        expected = endpoint(spec, account)
    if url != expected:
        raise RegistryError("unapproved provider URL")


def _valid_secret(secret: object) -> bool:
    return isinstance(secret, str) and bool(secret) and all(32 < ord(c) < 127 for c in secret)


def _text_request(
    spec: ModelSpec,
    secret: str,
    content: str,
    output: int,
    source: str | None,
    target: str | None,
) -> None:
    if (
        not _valid_secret(secret)
        or not isinstance(content, str)
        or not content.strip()
        or type(output) is not int
        or not 1 <= output <= spec.max_output_tokens
        or source is not None
        or target is not None
    ):
        raise RegistryError("unsupported adapter request")


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", item)
        for item in value
    ):
        raise RegistryError(f"invalid {label}")
    if len(value) != len(set(value)):
        raise RegistryError(f"duplicate {label}")
    return tuple(value)


def _object(value: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict) or not required <= value.keys():
        raise RegistryError(f"invalid {label}")
    if value.keys() - required - optional:
        raise RegistryError(f"unsupported {label} fields")
    return value


def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("duplicate manifest field")
        result[key] = value
    return result


class _FamilyAdapterBridge:
    """Keep legacy adapters intact while registering the typed protocol contract."""

    def __init__(self, implementation: Any):
        self.implementation = implementation
        self.supported_capabilities = implementation.supported_capabilities
        self.supported_features = implementation.supported_features

    def request(self, *args: Any, **kwargs: Any) -> Any:
        raise RegistryError("structured input is required for this adapter")

    def transport(self, *args: Any, **kwargs: Any) -> Any:
        raise RegistryError("structured transport is required for this adapter")

    def interpret(self, *args: Any, **kwargs: Any) -> Any:
        raise RegistryError("structured result is required for this adapter")

    def admit(self, spec: ModelSpec, task: Mapping[str, object]) -> None:
        self.implementation.admit_task(spec, dict(task))

    def candidate_endpoint(self, value: str, model: str) -> str:
        for template in self.implementation.endpoints.values():
            if value == template.replace("{model}", model):
                return template
        raise RegistryError("candidate endpoint differs from its protocol contract")


class Registry:
    def __init__(
        self,
        models: Mapping[tuple[str, str], ModelSpec],
        *,
        adapters: Mapping[str, Adapter] | None = None,
    ):
        implementations: dict[str, Adapter] = {
            "builtin:" + model.provider: _BuiltinAdapter() for model in MODELS.values()
        }
        implementations["openai_chat"] = OpenAIChatAdapter()
        implementations["gemini_generate_content"] = GeminiGenerateContentAdapter()
        implementations["cloudflare_workers_ai"] = CloudflareWorkersAIAdapter()
        from .family_adapters import PACKAGED_FAMILY_ADAPTERS

        implementations.update(
            {name: _FamilyAdapterBridge(value) for name, value in PACKAGED_FAMILY_ADAPTERS.items()}
        )
        for name, adapter in (adapters or {}).items():
            _identifier(name, "adapter name")
            if name in implementations:
                raise RegistryError("packaged adapter cannot be overridden")
            if any(
                not callable(getattr(adapter, method, None))
                for method in ("request", "transport", "interpret")
            ):
                raise RegistryError("invalid adapter implementation")
            for method in ("admit", "quota_rejection", "quota_observations"):
                hook = getattr(adapter, method, None)
                if hook is not None and not callable(hook):
                    raise RegistryError("invalid adapter hook")
            implementations[name] = adapter
        self.adapters: Mapping[str, Adapter] = MappingProxyType(implementations)
        if any(spec.adapter not in self.adapters for spec in models.values()):
            raise RegistryError("unregistered adapter")
        self.models: Mapping[tuple[str, str], ModelSpec] = MappingProxyType(dict(models))
        self.providers = frozenset(provider for provider, _ in self.models)

    def adapter_support(self, adapter_id: str, capability: str) -> bool | None:
        """Declared protocol contract, distinct from a model's enabled families."""
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            return False
        supported = getattr(adapter, "supported_capabilities", None)
        if supported is not None:
            return capability in supported
        if adapter_id.startswith("builtin:"):
            return any(
                spec.capability == capability and spec.adapter == adapter_id
                for spec in self.models.values()
            )
        return None

    def adapter_features(self, adapter_id: str) -> frozenset[str] | None:
        supported = getattr(self.adapters.get(adapter_id), "supported_features", None)
        return frozenset(supported) if supported is not None else None

    def fixed_family_adapter(self, adapter_id: str) -> bool:
        return isinstance(self.adapters.get(adapter_id), _FamilyAdapterBridge)

    @staticmethod
    def response_reflects(raw: bytes, headers: dict[str, str], private: tuple[str, ...]) -> bool:
        return _response_reflects(raw, headers, private)

    def supports_family(self, spec: ModelSpec, capability: str) -> bool:
        self._check(spec)
        return (
            capability in (spec.capabilities or (spec.capability,))
            and self.adapter_support(
                dict(spec.family_adapters).get(capability, spec.adapter), capability
            )
            is not False
        )

    def candidate_definition(self, row: dict) -> tuple[dict, dict]:
        """Use the selected adapter's declared contract to validate disabled catalog candidates."""
        adapter_id, capability = row["protocol"], row["capability"]
        if self.adapter_support(adapter_id, capability) is not True:
            raise RegistryError("candidate family has no declared protocol contract")
        from .families import FamilyRegistry

        token_output = FamilyRegistry.builtin().uses_output_tokens(capability)
        if row["endpoint"] is None or (
            token_output and (row["context_tokens"] is None or row["max_output_tokens"] is None)
        ):
            raise RegistryError("candidate model limits or endpoint are unknown")
        adapter = self.adapters[adapter_id]
        endpoint_hook = getattr(adapter, "candidate_endpoint", None)
        endpoint_value = (
            endpoint_hook(row["endpoint"], row["model"])
            if endpoint_hook is not None
            else row["endpoint"]
        )
        origin = "https://" + urlsplit(endpoint_value).netloc
        declaration = {
            "id": row["provider"],
            "adapter": adapter_id,
            "origin": origin,
            "endpoint": endpoint_value,
        }
        allowed_features = self.adapter_features(adapter_id)
        features = set(row["features"]) & set(allowed_features or ())
        features.add(capability)
        proposal = {
            "provider": row["provider"],
            "model": row["model"],
            "capability": capability,
            "context_tokens": row["context_tokens"] if row["context_tokens"] is not None else 0,
            "max_output_tokens": row["max_output_tokens"]
            if row["max_output_tokens"] is not None
            else 0,
            "features": sorted(features),
            "adapter": adapter_id,
        }
        self.from_manifest({"schema_version": 1, "providers": [declaration], "models": [proposal]})
        return declaration, proposal

    @classmethod
    def builtin(cls, *, adapters: Mapping[str, Adapter] | None = None) -> "Registry":
        return cls(
            {
                (model.provider, model.model): ModelSpec(
                    **asdict(model),
                    adapter="builtin:" + model.provider,
                    features=(model.capability, "text")
                    if model.capability != "ocr"
                    else ("ocr", "image"),
                    input_parameters=(
                        ("input", "source_language", "target_language")
                        if model.capability == "translation"
                        else ("input",)
                    ),
                )
                for model in MODELS.values()
            },
            adapters=adapters,
        )

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        adapters: Mapping[str, Adapter] | None = None,
    ) -> "Registry":
        try:
            document = json.loads(Path(path).read_text(), object_pairs_hook=_unique_json)
        except (OSError, ValueError) as exc:
            raise RegistryError("invalid registry manifest") from exc
        return cls.from_manifest(document, adapters=adapters)

    @classmethod
    def from_manifest(
        cls,
        document: object,
        *,
        adapters: Mapping[str, Adapter] | None = None,
    ) -> "Registry":
        """Validate an in-memory candidate with the same policy as a saved manifest."""
        document = _object(document, {"schema_version", "providers", "models"}, set(), "manifest")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise RegistryError("unsupported registry schema version")
        if not isinstance(document["providers"], list) or not isinstance(document["models"], list):
            raise RegistryError("invalid registry collections")
        registry = cls.builtin(adapters=adapters)
        providers: dict[str, ProviderSpec] = {}
        for value in document["providers"]:
            value = _object(value, {"id", "adapter", "origin", "endpoint"}, set(), "provider")
            provider_id = _identifier(value["id"], "provider id")
            if provider_id in providers:
                raise RegistryError("duplicate provider id")
            adapter_name = _identifier(value["adapter"], "adapter name")
            if adapter_name not in registry.adapters:
                raise RegistryError("unregistered adapter")
            origin = _url(value["origin"], origin=True)
            adapter = registry.adapters[adapter_name]
            if isinstance(adapter, _FamilyAdapterBridge):
                implementation = adapter.implementation
                if origin not in implementation.allowed_origins(provider_id):
                    raise RegistryError("unapproved family provider origin")
                url = value["endpoint"]
                if url != implementation.endpoints.get(provider_id):
                    raise RegistryError("unapproved family endpoint schema")
            else:
                url = _protocol_endpoint(adapter_name, origin, value["endpoint"])
            if (urlsplit(url).scheme, urlsplit(url).netloc) != (
                urlsplit(origin).scheme,
                urlsplit(origin).netloc,
            ):
                raise RegistryError("endpoint outside provider origin")
            if provider_id in registry.providers and not isinstance(adapter, _FamilyAdapterBridge):
                builtin = [s for (p, _), s in registry.models.items() if p == provider_id]
                incompatible = (
                    adapter_name != "openai_chat"
                    and not (provider_id == "google" and adapter_name == "gemini_generate_content")
                    and not (
                        provider_id == "cloudflare" and adapter_name == "cloudflare_workers_ai"
                    )
                )
                pinned = {s.origin for s in builtin} == {origin}
                if adapter_name == "openai_chat":
                    pinned = pinned and provider_id in {"nvidia", "groq", "mistral", "openrouter"}
                    pinned = pinned and {(s.origin, s.endpoint_template) for s in builtin} == {
                        (origin, url)
                    }
                if incompatible or not pinned:
                    raise RegistryError("builtin provider endpoint cannot be overridden")
            providers[provider_id] = ProviderSpec(provider_id, adapter_name, origin, url)
        models = dict(registry.models)
        supplied: set[tuple[str, str]] = set()
        required = {"provider", "model", "capability", "context_tokens", "max_output_tokens"}
        optional = {
            "features",
            "input_parameters",
            "output_parameters",
            "author",
            "host",
            "free_kind",
            "use_restrictions",
            "source",
            "verified_at",
            "capabilities",
            "adapter",
            "family_adapters",
            "replace_builtin",
        }
        for value in document["models"]:
            value = _object(value, required, optional, "model")
            provider_id = _identifier(value["provider"], "model provider")
            if provider_id not in providers:
                raise RegistryError("model has unregistered provider")
            provider = providers[provider_id]
            adapter_name = _identifier(value.get("adapter", provider.adapter), "model adapter")
            if adapter_name not in registry.adapters:
                raise RegistryError("unregistered model adapter")
            model_adapter = registry.adapters[adapter_name]
            if adapter_name != provider.adapter and not isinstance(
                model_adapter, _FamilyAdapterBridge
            ):
                raise RegistryError("model adapter override requires a fixed family contract")
            model_id = value["model"]
            if catalog_model_id_reason(model_id) is not None:
                raise RegistryError("invalid model id")
            assert isinstance(model_id, str)
            model_key = (provider_id, model_id)
            replacing = value.get("replace_builtin", False)
            if type(replacing) is not bool:
                raise RegistryError("invalid builtin replacement flag")
            if model_key in supplied or (
                model_key in models
                and not (
                    replacing
                    and models[model_key].adapter.startswith("builtin:")
                    and isinstance(model_adapter, _FamilyAdapterBridge)
                )
            ):
                raise RegistryError("duplicate provider/model")
            if replacing and model_key not in registry.models:
                raise RegistryError("replacement requires an existing builtin model")
            supplied.add(model_key)
            capability = _identifier(value["capability"], "capability")
            capabilities = _strings(value.get("capabilities", [capability]), "capabilities")
            bindings = value.get("family_adapters", {})
            if not isinstance(bindings, dict) or bindings.keys() - set(capabilities):
                raise RegistryError("invalid family adapter bindings")
            for binding in bindings.values():
                if not isinstance(binding, str) or not isinstance(
                    registry.adapters.get(binding), _FamilyAdapterBridge
                ):
                    raise RegistryError("family binding requires a fixed protocol contract")
            if capability not in capabilities or any(
                registry.adapter_support(bindings.get(family, adapter_name), family) is False
                for family in capabilities
            ):
                raise RegistryError("unsupported adapter family")
            if (
                adapter_name in {"openai_chat", "gemini_generate_content", "cloudflare_workers_ai"}
                and capability != "text_generation"
            ):
                raise RegistryError("unsupported adapter capability")
            from .families import FamilyRegistry

            token_model = (
                any(FamilyRegistry.builtin().uses_output_tokens(family) for family in capabilities)
                if isinstance(model_adapter, _FamilyAdapterBridge)
                else True
            )
            for name in ("context_tokens", "max_output_tokens"):
                if (
                    type(value[name]) is not int
                    or not (1 if token_model else 0) <= value[name] <= 100_000_000
                ):
                    raise RegistryError("invalid model token limits")
            if value["max_output_tokens"] > value["context_tokens"]:
                raise RegistryError("output limit exceeds context")
            features = _strings(value.get("features", [capability, "text"]), "features")
            supported_features = {"text_generation", "text"}
            if adapter_name == "openai_chat":
                supported_features.add("json_output")
            if (
                adapter_name in {"openai_chat", "gemini_generate_content", "cloudflare_workers_ai"}
                and not set(features) <= supported_features
            ):
                raise RegistryError("unsupported adapter features")
            if isinstance(model_adapter, _FamilyAdapterBridge) and not set(features) <= set().union(
                model_adapter.supported_features,
                *(
                    registry.adapter_features(binding) or frozenset()
                    for binding in bindings.values()
                ),
            ):
                raise RegistryError("unsupported adapter features")
            inputs = _strings(value.get("input_parameters", ["input"]), "input parameters")
            outputs = _strings(
                value.get("output_parameters", ["max_output_tokens"]), "output parameters"
            )
            if adapter_name in {
                "openai_chat",
                "gemini_generate_content",
                "cloudflare_workers_ai",
            } and (inputs != ("input",) or outputs != ("max_output_tokens",)):
                raise RegistryError("unsupported adapter parameters")
            metadata = {
                name: value.get(name, "")
                for name in ("author", "host", "use_restrictions", "source", "verified_at")
            }
            metadata["free_kind"] = value.get("free_kind", "administrator_declared_unknown")
            if any(not isinstance(item, str) for item in metadata.values()):
                raise RegistryError("invalid model metadata")
            model_origin = provider.origin
            model_endpoint = _model_endpoint(provider, model_id)
            if isinstance(model_adapter, _FamilyAdapterBridge):
                implementation = model_adapter.implementation
                template = implementation.endpoints.get(provider_id)
                if template is None:
                    raise RegistryError("family adapter does not support provider")
                model_origin = "https://" + urlsplit(template).netloc
                prototype = ModelSpec(
                    provider=provider_id,
                    model=model_id,
                    context_tokens=value["context_tokens"],
                    max_output_tokens=value["max_output_tokens"],
                    capability=capability,
                    origin=model_origin,
                    endpoint_template=template,
                    adapter=adapter_name,
                    capabilities=capabilities,
                    **metadata,
                )
                model_endpoint = implementation.endpoint_for(prototype, capability, "{account_id}")
            models[(provider_id, model_id)] = ModelSpec(
                provider=provider_id,
                model=model_id,
                context_tokens=value["context_tokens"],
                max_output_tokens=value["max_output_tokens"],
                capability=value["capability"],
                origin=model_origin,
                endpoint_template=model_endpoint,
                adapter=adapter_name,
                features=features,
                input_parameters=inputs,
                output_parameters=outputs,
                capabilities=capabilities,
                family_adapters=tuple(sorted(bindings.items())),
                **metadata,
            )
            for family, binding in bindings.items():
                bound_adapter = registry.adapters[binding]
                assert isinstance(bound_adapter, _FamilyAdapterBridge)
                implementation = bound_adapter.implementation
                template = implementation.endpoints.get(provider_id)
                if template is None:
                    raise RegistryError("family binding does not support provider")
                bound_spec = replace(
                    models[model_key],
                    adapter=binding,
                    origin="https://" + urlsplit(template).netloc,
                )
                implementation.endpoint_for(bound_spec, family, "{account_id}")
        if set(providers) - {p for p, _ in supplied}:
            raise RegistryError("provider has no registered models")
        return cls(models, adapters=adapters)

    def resolve(self, model_id: str, provider: str | None = None) -> ModelSpec:
        if provider is not None:
            model = self.models.get((provider, model_id))
            if model is None:
                raise RegistryError("unrecognized provider/model")
            return model
        matches = [spec for (_, name), spec in self.models.items() if name == model_id]
        if not matches:
            raise RegistryError("unrecognized model")
        if len(matches) != 1:
            raise RegistryError("ambiguous model: provider constraint required")
        return matches[0]

    def _check(self, spec: ModelSpec) -> None:
        if self.models.get((spec.provider, spec.model)) != spec:
            raise RegistryError("unregistered model specification")

    def _check_endpoint(self, spec: ModelSpec, url: str) -> None:
        self._check(spec)
        _fixed_endpoint(spec, url)

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
        *,
        response_format: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        self._check(spec)
        url, headers, payload = self.adapters[spec.adapter].request(
            spec, account_id, secret, content, max_output_tokens, source_language, target_language
        )
        self._check_endpoint(spec, url)
        if response_format is not None:
            if (
                spec.adapter != "openai_chat"
                or "json_output" not in spec.features
                or response_format != {"type": "json_object"}
            ):
                raise RegistryError("unsupported response format")
            payload["response_format"] = dict(response_format)
        return url, headers, payload

    def admit(self, spec: ModelSpec, task: Mapping[str, object]) -> None:
        """Run only the selected adapter's input policy; no credentials or I/O."""
        self._check(spec)
        if not isinstance(task.get("capability"), str) or not self.supports_family(
            spec, str(task["capability"])
        ):
            raise RegistryError("model/capability mismatch")
        if isinstance(task.get("input"), dict):
            implementation, bound_spec = self._typed_binding(spec, str(task["capability"]))
            implementation.admit_task(bound_spec, dict(task))
            return
        hook = getattr(self.adapters[spec.adapter], "admit", None)
        if hook is not None:
            hook(spec, task)

    def _typed_binding(self, spec: ModelSpec, capability: str) -> tuple[Any, ModelSpec]:
        self._check(spec)
        adapter_id = dict(spec.family_adapters).get(capability, spec.adapter)
        adapter = self.adapters[adapter_id]
        if isinstance(adapter, _FamilyAdapterBridge):
            implementation = adapter.implementation
            template = implementation.endpoints.get(spec.provider)
            if template is None:
                raise RegistryError("family adapter does not support provider")
            bound_spec = replace(
                spec, adapter=adapter_id, origin="https://" + urlsplit(template).netloc
            )
            return implementation, bound_spec
        # These are protocol aliases, never model-name or price guesses.
        aliases = {
            "builtin:nvidia": "openai_inference",
            "builtin:groq": "openai_inference",
            "builtin:mistral": "openai_inference",
            "builtin:openrouter": "openai_inference",
            "openai_chat": "openai_inference",
            "builtin:google": "gemini_inference",
            "gemini_generate_content": "gemini_inference",
            "builtin:cloudflare": "cloudflare_text",
            "cloudflare_workers_ai": "cloudflare_text",
            "builtin:ocrspace": "ocrspace_inference",
        }
        name = aliases.get(adapter_id)
        selected = self.adapters.get(name or "")
        if not isinstance(selected, _FamilyAdapterBridge):
            raise RegistryError("adapter has no structured protocol contract")
        return selected.implementation, spec

    def request_task(
        self, spec: ModelSpec, account_id: str, secret: str, data: dict[str, Any]
    ) -> FamilyRequest:
        if not _valid_secret(secret):
            raise RegistryError("invalid provider credential")
        self.admit(spec, data)
        implementation, bound_spec = self._typed_binding(spec, data["capability"])
        request = implementation.request_task(bound_spec, account_id, secret, data)
        if not isinstance(request, FamilyRequest) or request.url != implementation.endpoint_for(
            bound_spec, data["capability"], account_id
        ):
            raise RegistryError("unapproved family request endpoint")
        _url(request.url)
        validate_family_request(request)
        return request

    def interpret_task(
        self, spec: ModelSpec, capability: str, status: int, headers: dict[str, str], raw: bytes
    ) -> FamilyResponse:
        implementation, bound_spec = self._typed_binding(spec, capability)
        response = implementation.interpret_task(bound_spec, capability, status, headers, raw)
        if not isinstance(response, FamilyResponse):
            raise RegistryError("invalid family adapter result")
        return response

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        self._check_endpoint(spec, url)
        return self.adapters[spec.adapter].transport(spec, url, headers, payload, timeout)

    def interpret(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]:
        self._check(spec)
        return self.adapters[spec.adapter].interpret(spec, status, raw)

    def quota_rejection(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        sensitive: tuple[str, ...] = (),
    ) -> bool:
        """True only for a tested adapter's explicit proof of non-execution."""
        self._check(spec)
        if _response_reflects(raw, headers, sensitive):
            return False
        hook = getattr(self.adapters[spec.adapter], "quota_rejection", None)
        if hook is None:
            return False
        result = hook(spec, status, raw, headers, sensitive)
        if type(result) is not bool:
            raise RegistryError("invalid adapter quota rejection result")
        return result

    def quota_observations(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        now: datetime,
        *,
        as_of: datetime | None = None,
    ) -> list[dict[str, object]]:
        """No provider-name inference; caller validates evidence and private values."""
        self._check(spec)
        hook = getattr(self.adapters[spec.adapter], "quota_observations", None)
        if hook is None:
            return []
        result = hook(spec, status, raw, headers, now, as_of=as_of)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise RegistryError("invalid adapter quota observation result")
        return [dict(item) for item in result]


class OpenAIChatAdapter:
    """Packaged standard chat adapter; extensions cannot replace this implementation."""

    supported_capabilities = frozenset({"text_generation"})
    supported_features = frozenset({"text_generation", "text", "json_output"})

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        if (
            spec.adapter != "openai_chat"
            or not isinstance(secret, str)
            or not secret
            or any(ord(c) <= 32 or ord(c) >= 127 for c in secret)
            or not isinstance(content, str)
            or not content.strip()
            or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= spec.max_output_tokens
            or source_language is not None
            or target_language is not None
        ):
            raise RegistryError("unsupported adapter request")
        return (
            spec.endpoint_template,
            {"Authorization": "Bearer " + secret},
            {
                "model": spec.model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": max_output_tokens,
                "stream": False,
            },
        )

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        required = {"model", "messages", "max_tokens", "stream"}
        messages = payload.get("messages")
        output = payload.get("max_tokens")
        response_format = payload.get("response_format")
        if (
            spec.adapter != "openai_chat"
            or set(headers) != {"Authorization"}
            or not isinstance(headers["Authorization"], str)
            or not headers["Authorization"].startswith("Bearer ")
            or any(ord(c) <= 31 or ord(c) >= 127 for c in headers["Authorization"])
            or payload.get("model") != spec.model
            or payload.get("stream") is not False
            or not required <= payload.keys()
            or payload.keys() - required - {"response_format"}
            or type(output) is not int
            or not 1 <= output <= spec.max_output_tokens
            or not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
            or set(messages[0]) != {"role", "content"}
            or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
            or not messages[0]["content"].strip()
            or (
                "response_format" in payload
                and (
                    "json_output" not in spec.features or response_format != {"type": "json_object"}
                )
            )
            or not isinstance(timeout, (float, int))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise RegistryError("unsupported adapter transport")
        return _json_http(spec, url, headers, payload, timeout)

    def interpret(
        self, spec: ModelSpec, status: int, raw: bytes
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]:
        if spec.adapter != "openai_chat":
            raise RegistryError("unsupported adapter")
        try:
            data = json.loads(raw, object_pairs_hook=_unique_json)
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        answer = None
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if (
                isinstance(message, dict)
                and isinstance(message.get("content"), str)
                and message["content"].strip()
            ):
                answer = message["content"]
        input_tokens = output_tokens = None
        usage = data.get("usage")
        if isinstance(usage, dict):
            inputs = usage.get("prompt_tokens")
            outputs = usage.get("completion_tokens")
            total = usage.get("total_tokens")
            if (
                type(inputs) is int
                and inputs >= 0
                and type(outputs) is int
                and outputs >= 0
                and type(total) is int
                and total == inputs + outputs
            ):
                input_tokens, output_tokens = inputs, outputs
        request_id = data.get("id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", request_id
        ):
            request_id = None
        return (answer if status == 200 else None, input_tokens, output_tokens, None, request_id)


class GeminiGenerateContentAdapter:
    """Existing generateContent text schema at a model-specific fixed endpoint."""

    supported_capabilities = frozenset({"text_generation"})
    supported_features = frozenset({"text_generation", "text"})

    def candidate_endpoint(self, value: str, model: str) -> str:
        expected = "https://generativelanguage.googleapis.com" + GEMINI_PATH
        if value != expected.replace("{model}", model):
            raise RegistryError("candidate endpoint differs from its protocol contract")
        return expected

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        _text_request(spec, secret, content, max_output_tokens, source_language, target_language)
        return (
            spec.endpoint_template,
            {"x-goog-api-key": secret},
            {
                "contents": [{"parts": [{"text": content}]}],
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            },
        )

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        config, contents = payload.get("generationConfig"), payload.get("contents")
        if (
            spec.adapter != "gemini_generate_content"
            or set(headers) != {"x-goog-api-key"}
            or not _valid_secret(headers["x-goog-api-key"])
            or set(payload) != {"contents", "generationConfig"}
            or not isinstance(config, dict)
            or set(config) != {"maxOutputTokens"}
            or type(config["maxOutputTokens"]) is not int
            or not 1 <= config["maxOutputTokens"] <= spec.max_output_tokens
            or not isinstance(contents, list)
            or len(contents) != 1
            or not isinstance(contents[0], dict)
            or set(contents[0]) != {"parts"}
        ):
            raise RegistryError("unsupported adapter transport")
        parts = contents[0]["parts"]
        if (
            not isinstance(parts, list)
            or len(parts) != 1
            or not isinstance(parts[0], dict)
            or set(parts[0]) != {"text"}
            or not isinstance(parts[0]["text"], str)
            or not parts[0]["text"].strip()
        ):
            raise RegistryError("unsupported adapter transport")
        return _json_http(spec, url, headers, payload, timeout)

    def interpret(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]:
        from .gateway_providers import interpret

        return interpret("google", status, raw, model_id=spec.model)


class CloudflareWorkersAIAdapter:
    """Existing Workers AI prompt schema, with only its validated account slot."""

    supported_capabilities = frozenset({"text_generation"})
    supported_features = frozenset({"text_generation", "text"})

    def candidate_endpoint(self, value: str, model: str) -> str:
        expected = "https://api.cloudflare.com" + CLOUDFLARE_PATH
        if value != expected.replace("{model}", model):
            raise RegistryError("candidate endpoint differs from its protocol contract")
        return expected

    def request(
        self,
        spec: ModelSpec,
        account_id: str,
        secret: str,
        content: str,
        max_output_tokens: int,
        source_language: str | None,
        target_language: str | None,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        _text_request(spec, secret, content, max_output_tokens, source_language, target_language)
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise RegistryError("invalid Cloudflare account id")
        return (
            endpoint(spec, account_id),
            {"Authorization": "Bearer " + secret},
            {
                "prompt": content,
                "max_tokens": max_output_tokens,
            },
        )

    def transport(
        self,
        spec: ModelSpec,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        prompt, output = payload.get("prompt"), payload.get("max_tokens")
        auth = headers.get("Authorization")
        if (
            spec.adapter != "cloudflare_workers_ai"
            or set(headers) != {"Authorization"}
            or not isinstance(auth, str)
            or not auth.startswith("Bearer ")
            or not _valid_secret(auth.removeprefix("Bearer "))
            or set(payload) != {"prompt", "max_tokens"}
            or not isinstance(prompt, str)
            or not prompt.strip()
            or type(output) is not int
            or not 1 <= output <= spec.max_output_tokens
        ):
            raise RegistryError("unsupported adapter transport")
        return _json_http(spec, url, headers, payload, timeout)

    def interpret(
        self,
        spec: ModelSpec,
        status: int,
        raw: bytes,
    ) -> tuple[str | None, int | None, int | None, int | None, str | None]:
        from .gateway_providers import interpret

        return interpret("cloudflare", status, raw, model_id=spec.model)


def _json_http(
    spec: ModelSpec,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    _fixed_endpoint(spec, url)
    if (
        not isinstance(timeout, (float, int))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise RegistryError("invalid transport timeout")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    from .client import _NoRedirect
    from .gateway_providers import ProviderPhaseTimeout

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except TimeoutError as exc:
        raise ProviderPhaseTimeout("timeout_before_headers") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderPhaseTimeout("timeout_before_headers") from exc
        raise
    with response:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except TimeoutError as exc:
            raise ProviderPhaseTimeout("timeout_response_body") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RegistryError("provider response too large")
        return response.status, dict(response.headers), raw
