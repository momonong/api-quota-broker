"""Direct official API client: one provider attempt per dispatch authorization."""

import json
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from .catalog import MODELS, endpoint
from .retry import parse_retry_after


class ClientError(RuntimeError):
    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _json_http(url: str, data: dict | None, headers: dict, timeout: float = 15) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **headers},
        method="POST" if data is not None else "GET",
    )
    # Broker authentication stays on the selected local endpoint regardless of
    # ambient HTTP_PROXY/HTTPS_PROXY settings; redirects remain disabled.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc)
        except (json.JSONDecodeError, ValueError):
            detail = {"error": "http_error"}
        raise ClientError(f"broker HTTP {exc.code}: {detail.get('error')}", detail=detail) from exc


def _provider_http(
    url: str, headers: dict[str, str], payload: dict[str, object], timeout: float
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _check_official(plan: dict) -> str:
    model_id = plan.get("model")
    model = MODELS.get(model_id) if isinstance(model_id, str) else None
    if model is None or model.provider != plan.get("provider"):
        raise ClientError("unrecognized provider/model")
    url = plan.get("endpoint")
    if not isinstance(url, str):
        raise ClientError("missing endpoint")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != urlsplit(model.origin).netloc:
        raise ClientError("unapproved provider origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("unapproved provider URL")
    if model.provider == "google":
        if url != endpoint(model, ""):
            raise ClientError("unapproved Google endpoint")
    else:
        prefix = "https://api.cloudflare.com/client/v4/accounts/"
        suffix = "/ai/run/" + model.model
        if not url.startswith(prefix) or not url.endswith(suffix):
            raise ClientError("unapproved Cloudflare endpoint")
        account = url[len(prefix) : -len(suffix)]
        if url != endpoint(model, account):
            raise ClientError("unapproved Cloudflare account path")
    return url


ProviderTransport = Callable[
    [str, dict[str, str], dict[str, object], float], tuple[int, dict[str, str], bytes]
]


class ProjectClient:
    """Private project API. Submit once; query the same job after any uncertainty.

    Remote projects use an authenticated SSH tunnel to this loopback destination.
    Provider credentials and routing decisions stay inside the Broker.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 15):
        try:
            url = urlsplit(base_url)
            valid = (
                url.scheme == "http"
                and url.hostname == "127.0.0.1"
                and url.port is not None
                and url.path in {"", "/"}
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if (
            not valid
            or not isinstance(token, str)
            or not 32 <= len(token) <= 1024
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ClientError("private loopback endpoint and project token required")
        if type(timeout) not in {int, float} or not 0 < timeout <= 185:
            raise ClientError("timeout must be bounded")
        self._base = base_url.rstrip("/") + "/quota/api/v1/jobs"
        self._token, self._timeout = token, timeout

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9._~-]{1,120}", value
        ):
            raise ClientError("stable request key required")
        return quote(value, safe="")

    def _request(
        self, key: str, suffix: str = "", body: dict | None = None, *, submit: bool = False
    ) -> dict:
        escaped = self._key(key)
        url = self._base if submit else self._base + "/" + escaped + suffix
        try:
            return _json_http(url, body, {"Authorization": "Bearer " + self._token}, self._timeout)
        except (OSError, TimeoutError, ValueError):
            # No second POST, new request key, provider fallback, or silent retry.
            raise ClientError(
                "broker transport outcome unknown",
                detail={
                    "error": "transport_unknown",
                    "request_key": key,
                    "action": "query_original_job",
                },
            ) from None

    def submit(self, task: dict) -> dict:
        if not isinstance(task, dict):
            raise ClientError("task must be an object")
        key = task.get("request_key")
        if not isinstance(key, str):
            raise ClientError("stable request key required")
        self._key(key)
        return self._request(key, body=task, submit=True)

    def submit_text(
        self,
        request_key: str,
        text: str,
        *,
        max_output_tokens: int = 256,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ClientError("text input required")
        task = {
            "request_key": request_key,
            "capability": "text_generation",
            "input": {"messages": [{"role": "user", "content": text}]},
            "max_output_tokens": max_output_tokens,
        }
        if provider is not None:
            task["provider"] = provider
        if model is not None:
            task["model"] = model
        return self.submit(task)

    def status(self, request_key: str) -> dict:
        return self._request(request_key)

    def result(self, request_key: str) -> dict:
        return self._request(request_key, "/result")

    def cancel(self, request_key: str) -> dict:
        return self._request(request_key, "/cancel", {})


class DirectClient:
    def __init__(
        self,
        broker_url: str,
        broker_token: str | None = None,
        provider_transport: ProviderTransport = _provider_http,
    ) -> None:
        parsed = urlsplit(broker_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ClientError("broker must be local loopback HTTP")
        self.broker_url = broker_url.rstrip("/")
        self.broker_token = broker_token
        self.provider_transport = provider_transport

    def _broker(self, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.broker_token}"} if self.broker_token else {}
        return _json_http(self.broker_url + path, body, headers)

    def run_text(
        self,
        prompt: str,
        request_key: str,
        provider_secret: str,
        model: str | None = None,
        max_output_tokens: int = 128,
        neuron_bound: int | None = None,
        timeout: float = 30,
    ) -> dict:
        if not prompt or not provider_secret:
            raise ClientError("prompt and provider secret required")
        # Deliberately generous estimate, not a tokenizer or a provider guarantee.
        input_bound = len(prompt.encode("utf-8")) * 2 + 128
        plan = self._broker(
            "/v1/reservations",
            {
                "request_key": request_key,
                "capability": "text_generation",
                "model": model,
                "input_token_bound": input_bound,
                "max_output_tokens": max_output_tokens,
                "neuron_bound": neuron_bound,
            },
        )
        if plan["state"] != "reserved":
            raise ClientError(
                f"request already {plan['state']}; provider call will not be repeated"
            )
        url = _check_official(plan)
        dispatched = self._broker(f"/v1/reservations/{plan['reservation_id']}/dispatch", {})
        if dispatched["state"] != "dispatched":
            raise ClientError("dispatch not authorized")
        provider = plan["provider"]
        headers: dict[str, str]
        payload: dict[str, object]
        if provider == "google":
            headers = {"x-goog-api-key": provider_secret}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            }
        else:
            headers = {"Authorization": f"Bearer {provider_secret}"}
            payload = {"prompt": prompt, "max_tokens": max_output_tokens}
        report = {
            "reservation_id": plan["reservation_id"],
            "report_key": str(uuid.uuid4()),
            "state": "unknown",
        }
        try:
            status, response_headers, raw = self.provider_transport(url, headers, payload, timeout)
            try:
                response = json.loads(raw)
            except (ValueError, TypeError):
                response = {}
            if not isinstance(response, dict):
                response = {}
            if provider == "google":
                metadata = response.get("usageMetadata", {})
                prompt_tokens = metadata.get("promptTokenCount")
                if type(prompt_tokens) is int and prompt_tokens >= 0:
                    report["state"] = "completed" if status < 400 else "failed"
                    report["usage"] = {"requests": 1, "input_tokens": prompt_tokens}
                report["provider_request_id"] = response.get("responseId")
            else:
                # REST responses do not establish actual Neurons used.
                neurons = response.get("usage", {}).get("neurons")
                if type(neurons) is int and neurons >= 0:
                    report["state"] = "completed" if status < 400 else "failed"
                    report["usage"] = {"requests": 1, "neurons": neurons}
            if status >= 400:
                report["error_status"] = status
                if status == 429:
                    retry = response_headers.get("Retry-After") or response_headers.get(
                        "retry-after"
                    )
                    delay = parse_retry_after(retry, datetime.now(UTC))
                    if delay is not None:
                        report["retry_after_seconds"] = delay
            result = {
                "status": status,
                "response": response,
                "reservation_id": plan["reservation_id"],
            }
        except (OSError, TimeoutError) as exc:
            result = {
                "status": None,
                "response": None,
                "reservation_id": plan["reservation_id"],
                "error": type(exc).__name__,
            }
        # A failed report cannot authorize replay. Reconcile by request id later.
        self._broker("/v1/reports", report)
        result["state"] = report["state"]
        return result


def call_free_api(
    prompt_or_input: str | dict[str, object],
    *,
    token: str,
    base_url: str = "http://127.0.0.1:18588",
    request_key: str | None = None,
    capability: str = "text_generation",
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int = 256,
    options: dict[str, object] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Unified one-call helper for invoking free providers via the Shared Broker.

    If `provider` / `model` are omitted, the Broker automatically selects the
    best eligible free route with available capacity.
    """
    client = ProjectClient(base_url, token, timeout=timeout)
    key = request_key or f"req-{uuid.uuid4().hex}"
    if isinstance(prompt_or_input, str) and capability == "text_generation":
        return client.submit_text(
            key,
            prompt_or_input,
            max_output_tokens=max_output_tokens,
            provider=provider,
            model=model,
        )
    task: dict[str, object] = {
        "request_key": key,
        "capability": capability,
        "input": prompt_or_input,
    }
    if capability in {"text_generation", "translation", "vision"}:
        task["max_output_tokens"] = max_output_tokens
    elif capability == "ocr" and isinstance(prompt_or_input, str):
        task["max_output_tokens"] = 1
    if provider is not None:
        task["provider"] = provider
    if model is not None:
        task["model"] = model
    if options is not None:
        task["options"] = options
    return client.submit(task)

