"""Direct official API client: one provider attempt per dispatch authorization."""

import json
import math
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.client import HTTPException
from urllib.parse import quote, urlsplit

from . import __version__
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
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"api-quota-broker/{__version__}",
            **headers,
        },
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
        if not isinstance(detail, dict):
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


class LegacyProjectClient:
    """Project-scoped HTTPS API; loopback HTTP is supported for local connections.

    Submit once and query the original job after uncertainty. TLS verification,
    disabled redirects and disabled ambient proxies keep bearer tokens on the
    configured origin. Provider credentials remain inside the Broker.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 15):
        try:
            if not isinstance(base_url, str) or any(ord(c) <= 32 for c in base_url):
                raise ValueError
            url = urlsplit(base_url)
            valid = (
                (
                    url.scheme == "https"
                    and bool(url.hostname)
                    or url.scheme == "http"
                    and url.hostname == "127.0.0.1"
                    and url.port is not None
                )
                and url.path.rstrip("/") in {"", "/quota", "/quota/api/v1"}
                and not url.path.endswith("//")
                and (url.port is None or 1 <= url.port <= 65535)
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
            raise ClientError("HTTPS or local loopback API endpoint and project token required")
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < timeout <= 185
        ):
            raise ClientError("timeout must be bounded")
        self._api = f"{url.scheme}://{url.netloc}/quota/api/v1"
        self._base = self._api + "/jobs"
        self._token, self._timeout = token, timeout

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", value):
            raise ClientError("stable request key required")
        return quote(value, safe="")

    def _request(
        self,
        key: str,
        suffix: str = "",
        body: dict | None = None,
        *,
        submit: bool = False,
        timeout: float | None = None,
    ) -> dict:
        escaped = self._key(key)
        url = self._base if submit else self._base + "/" + escaped + suffix
        try:
            result = self._http(
                url, body, self._timeout if timeout is None else min(timeout, self._timeout)
            )
            if not isinstance(result, dict):
                raise ClientError("invalid job response", detail={"error": "invalid_response"})
            return result
        except ClientError as exc:
            exc.detail = {**(exc.detail or {}), "request_key": key}
            raise
        except (OSError, TimeoutError, ValueError, HTTPException):
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
        options: dict[str, object] | None = None,
        requirements: dict[str, object] | None = None,
        execution_policy: str | None = None,
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
        if options is not None:
            task["options"] = options
        if requirements is not None:
            task["requirements"] = requirements
        if execution_policy is not None:
            task["execution_policy"] = execution_policy
        return self.submit(task)

    def _http(self, url: str, body: dict | None, timeout: float) -> object:
        return _json_http(url, body, {"Authorization": "Bearer " + self._token}, timeout)

    def _metadata(self, path: str, body: dict | None = None) -> object:
        try:
            return self._http(self._api + path, body, self._timeout)
        except (OSError, TimeoutError, ValueError, HTTPException):
            raise ClientError(
                "broker metadata unavailable", detail={"error": "transport_error"}
            ) from None

    def catalog(self) -> list[dict]:
        """Read configured models and separately labelled quota evidence."""
        result = self._metadata("/catalog")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ClientError("invalid catalog response", detail={"error": "invalid_response"})
        return result

    def explain(self, task: dict) -> dict:
        """Check the complete task without reserving quota or dispatching."""
        if not isinstance(task, dict):
            raise ClientError("task must be an object")
        result = self._metadata("/routes/explain", task)
        if not isinstance(result, dict):
            raise ClientError("invalid plan response", detail={"error": "invalid_response"})
        return result

    @staticmethod
    def _wait_bounds(wait_timeout: float, poll_interval: float) -> None:
        if (
            type(wait_timeout) not in {int, float}
            or not math.isfinite(wait_timeout)
            or not 0 <= wait_timeout <= 3600
            or type(poll_interval) not in {int, float}
            or not math.isfinite(poll_interval)
            or not 0 < poll_interval <= 60
        ):
            raise ClientError("wait timeout and polling interval must be bounded")

    def wait(
        self,
        request_key: str,
        *,
        wait_timeout: float = 120,
        poll_interval: float = 1,
    ) -> dict:
        """Wait for the original job; a local deadline never cancels or resubmits it."""
        self._key(request_key)
        self._wait_bounds(wait_timeout, poll_interval)
        deadline = time.monotonic() + wait_timeout
        state = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError(
                    "broker wait deadline reached",
                    detail={
                        "error": "wait_timeout",
                        "request_key": request_key,
                        "state": state,
                        "action": "query_original_job",
                    },
                )
            status = self._request(request_key, timeout=remaining)
            state = status.get("state")
            if status.get("result_available") is True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
                outcome = self._request(request_key, "/result", timeout=remaining)
                state = outcome.get("state")
                if state in {"completed", "completed_usage_unknown"}:
                    return outcome
                raise ClientError(
                    "broker job did not complete",
                    detail={
                        "error": "job_unknown" if state == "unknown" else "job_failed",
                        "request_key": request_key,
                        "state": state,
                        "error_code": outcome.get("error_code"),
                        "action": "query_original_job",
                    },
                )
            pending_result = status.get("result_state") in {"pending", "storage_failed"}
            if (
                state in {"completed", "failed", "expired", "cancelled", "unknown"}
                and not pending_result
            ):
                raise ClientError(
                    "broker job has no available result",
                    detail={
                        "error": "job_unknown" if state == "unknown" else "job_failed",
                        "request_key": request_key,
                        "state": state,
                        "error_code": status.get("error_code"),
                        "action": "query_original_job",
                    },
                )
            if state not in {"queued", "waiting", "running"} and not pending_result:
                raise ClientError(
                    "invalid job state",
                    detail={"error": "invalid_response", "request_key": request_key},
                )
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

    def run(self, task: dict, *, wait_timeout: float = 120, poll_interval: float = 1) -> dict:
        """Submit once, then return the retained outcome or a resumable ClientError."""
        self._wait_bounds(wait_timeout, poll_interval)
        self.submit(task)
        return self.wait(
            task["request_key"], wait_timeout=wait_timeout, poll_interval=poll_interval
        )

    def status(self, request_key: str) -> dict:
        return self._request(request_key)

    def result(self, request_key: str) -> dict:
        return self._request(request_key, "/result")

    def cancel(self, request_key: str) -> dict:
        return self._request(request_key, "/cancel", {})


class ProjectClient(LegacyProjectClient):
    """Encrypted project API. No token or business data is sent before trust validation."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15,
        security_profile: str = "encrypted",
        trust_profile: object = None,
        trust_state: object = None,
        ssl_context: object = None,
    ):
        from .secure_client import SecureTransport

        super().__init__(base_url, token, timeout=timeout)
        if security_profile != "encrypted" or not self._api.startswith("https://"):
            raise ClientError("encrypted HTTPS profile required")
        self._secure = SecureTransport(self._api, trust_profile, trust_state, ssl_context)

    def _http(self, url: str, body: dict | None, timeout: float) -> object:
        return self._secure.request(url, body, self._token, timeout)


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
    base_url: str = "https://momonong.me/quota/api/v1",
    request_key: str | None = None,
    capability: str = "text_generation",
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int = 256,
    options: dict[str, object] | None = None,
    requirements: dict[str, object] | None = None,
    timeout: float = 30.0,
    wait_timeout: float = 120.0,
    poll_interval: float = 1.0,
    execution_policy: str | None = None,
    trust_profile: object = None,
    trust_state: object = None,
    ssl_context: object = None,
) -> dict[str, object]:
    """Submit once and obtain a retained result through the shared project API.

    If `provider` / `model` are omitted, the Broker automatically selects the
    best eligible free route with available capacity.
    """
    if request_key is None:
        raise ClientError("stable request key required; persist it before calling")
    client = ProjectClient(
        base_url,
        token,
        timeout=timeout,
        trust_profile=trust_profile,
        trust_state=trust_state,
        ssl_context=ssl_context,
    )
    client._key(request_key)
    if isinstance(prompt_or_input, str) and capability == "text_generation":
        prompt_or_input = {"messages": [{"role": "user", "content": prompt_or_input}]}
    task: dict[str, object] = {
        "request_key": request_key,
        "capability": capability,
        "input": prompt_or_input,
    }
    if capability in {"text_generation", "translation", "vision", "code_completion"}:
        task["max_output_tokens"] = max_output_tokens
    elif capability == "ocr" and isinstance(prompt_or_input, str):
        task["max_output_tokens"] = 1
    if provider is not None:
        task["provider"] = provider
    if model is not None:
        task["model"] = model
    if options is not None:
        task["options"] = options
    if requirements is not None:
        task["requirements"] = requirements
    if execution_policy is not None:
        task["execution_policy"] = execution_policy
    return client.run(task, wait_timeout=wait_timeout, poll_interval=poll_interval)
