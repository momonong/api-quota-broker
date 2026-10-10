"""Direct official API client: one provider attempt per dispatch authorization."""

import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

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
