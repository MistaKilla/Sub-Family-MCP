"""OpenAI Chat Completions adapter.

This is deliberately implemented against HTTP rather than a vendor SDK: every
local runtime and most gateways expose the same small surface, and a thin
``httpx`` client gives us precise control over retries, timeouts, per-member TLS
settings, and provider-specific ``extra_body`` parameters without tracking SDK
breaking changes.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from sub_family.config.models import MemberConfig
from sub_family.errors import ErrorKind, ProviderError
from sub_family.logging_config import get_logger
from sub_family.providers.base import ChatResult, ProviderHealth, Usage

_log = get_logger("provider")
_SECRET_PATTERN = re.compile(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}\b")


class OpenAICompatProvider:
    """Adapter for any endpoint speaking OpenAI's ``/chat/completions``."""

    def __init__(
        self,
        name: str,
        member: MemberConfig,
        *,
        api_key: str | None,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.member = member
        self.api_key = api_key
        self.client = client
        self._sleep = sleep
        self.chat_url = self._build_chat_url()
        self.models_url = self._build_models_url()

    # ------------------------------------------------------------------ URLs
    def _build_chat_url(self) -> str:
        if self.member.chat_path:
            return f"{self.member.base_url}{self.member.chat_path}"
        path = urlsplit(self.member.base_url).path.rstrip("/")
        if path.endswith("/chat/completions"):
            return self.member.base_url
        if path.endswith("/v1") or "/v1/" in path:
            return f"{self.member.base_url}/chat/completions"
        return f"{self.member.base_url}/v1/chat/completions"

    def _build_models_url(self) -> str:
        base = self.chat_url
        suffix = "/chat/completions"
        if base.endswith(suffix):
            return base[: -len(suffix)] + "/models"
        return f"{self.member.base_url}/v1/models"

    # -------------------------------------------------------------- requests
    def _headers(self) -> dict[str, str]:
        return self.member.auth_headers(self.api_key)

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.member.model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        extra: dict[str, Any] = dict(self.member.extra_body)
        if extra_body:
            extra.update(extra_body)
        for reserved in ("model", "messages", "stream"):
            extra.pop(reserved, None)
        if "max_completion_tokens" in extra:
            payload.pop("max_tokens", None)
        payload.update(extra)
        return payload

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        """POST a chat completion with bounded retries.

        Retries cover rate limits, transient server errors, and transport
        failures. Authentication, not-found, and malformed-request errors fail
        immediately because retrying cannot help.
        """
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        request_timeout = timeout if timeout is not None else self.member.timeout_s
        attempts = self.member.retries + 1
        last_error: ProviderError | None = None

        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = await self.client.post(
                    self.chat_url,
                    json=payload,
                    headers=self._headers(),
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    f"member '{self.name}' timed out after {request_timeout:.0f}s",
                    kind=ErrorKind.TIMEOUT,
                    retryable=True,
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ProviderError(
                    f"member '{self.name}' is unreachable at {self._safe_host()}: {exc}",
                    kind=ErrorKind.CONNECTION,
                    retryable=True,
                )
                last_error.__cause__ = exc
            else:
                if response.status_code < 400:
                    return self._parse_success(response, started)
                last_error = self._error_from_response(response)

            if attempt + 1 >= attempts or not last_error.retryable:
                raise last_error

            delay = self._retry_delay(attempt, last_error)
            _log.warning(
                "member %s attempt %d/%d failed (%s); retrying in %.1fs",
                self.name,
                attempt + 1,
                attempts,
                last_error.kind,
                delay,
            )
            await self._sleep(delay)

        raise last_error or ProviderError(  # pragma: no cover - loop always raises
            f"member '{self.name}' failed without a classified error",
            kind=ErrorKind.SERVER,
        )

    def _parse_success(self, response: httpx.Response, started: float) -> ChatResult:
        try:
            raw_data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                f"member '{self.name}' returned a non-JSON response",
                kind=ErrorKind.BAD_RESPONSE,
                detail=_clean_detail(response.text),
            ) from exc
        if not isinstance(raw_data, dict):
            raise ProviderError(
                f"member '{self.name}' returned an unexpected JSON shape",
                kind=ErrorKind.BAD_RESPONSE,
                detail=_clean_detail(response.text),
            )
        data: dict[str, Any] = raw_data
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                f"member '{self.name}' returned no choices",
                kind=ErrorKind.BAD_RESPONSE,
                detail=_clean_detail(response.text),
            )
        raw_choice = choices[0]
        choice: dict[str, Any] = raw_choice if isinstance(raw_choice, dict) else {}
        raw_message = choice.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        content = _normalise_content(message.get("content"))
        reasoning_value = message.get("reasoning_content") or message.get("reasoning")
        reasoning = reasoning_value if isinstance(reasoning_value, str) else None
        raw_usage = data.get("usage")
        usage_data: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        usage = Usage(
            prompt_tokens=_as_int(usage_data.get("prompt_tokens")),
            completion_tokens=_as_int(usage_data.get("completion_tokens")),
            total_tokens=_as_int(usage_data.get("total_tokens")),
        )
        raw_model = data.get("model")
        model = str(raw_model) if isinstance(raw_model, str) else self.member.model
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not content and not reasoning:
            finish = choice.get("finish_reason")
            raise ProviderError(
                f"member '{self.name}' returned an empty message"
                + (f" (finish_reason={finish})" if finish else ""),
                kind=ErrorKind.BAD_RESPONSE,
            )
        return ChatResult(
            content=content,
            model=model,
            usage=usage,
            finish_reason=choice.get("finish_reason")
            if isinstance(choice.get("finish_reason"), str)
            else None,
            reasoning=reasoning,
            elapsed_ms=elapsed_ms,
        )

    async def list_models(self, *, timeout: float = 15.0) -> list[str]:
        """Return model ids exposed by the endpoint."""
        try:
            response = await self.client.get(
                self.models_url, headers=self._headers(), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"member '{self.name}' timed out while listing models",
                kind=ErrorKind.TIMEOUT,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"member '{self.name}' is unreachable at {self._safe_host()}: {exc}",
                kind=ErrorKind.CONNECTION,
            ) from exc
        if response.status_code >= 400:
            raise self._error_from_response(response, operation="list models")
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                f"member '{self.name}' returned non-JSON data from /models",
                kind=ErrorKind.BAD_RESPONSE,
            ) from exc
        raw_models = data.get("data") if isinstance(data, dict) else data
        if not isinstance(raw_models, list):
            raise ProviderError(
                f"member '{self.name}' returned an unexpected /models payload",
                kind=ErrorKind.BAD_RESPONSE,
            )
        models: list[str] = []
        for entry in raw_models:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                models.append(entry["id"])
            elif isinstance(entry, str):
                models.append(entry)
        return models

    async def health(self) -> ProviderHealth:
        """Probe ``/models`` and report whether the configured model is present."""
        started = time.perf_counter()
        try:
            models = await self.list_models(timeout=min(15.0, self.member.timeout_s))
        except ProviderError as exc:
            return ProviderHealth(
                ok=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
                status_code=exc.status,
            )
        available = None
        if models:
            available = any(_model_matches(self.member.model, candidate) for candidate in models)
        return ProviderHealth(
            ok=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            model_available=available,
            models=models,
        )

    # ---------------------------------------------------------------- errors
    def _error_from_response(
        self, response: httpx.Response, *, operation: str = "chat completion"
    ) -> ProviderError:
        status = response.status_code
        detail = _clean_detail(response.text)
        message = _extract_error_message(response) or detail or "no response body"
        kind, retryable = _classify_status(status)
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if kind is ErrorKind.AUTH:
            text = (
                f"member '{self.name}' rejected the API key (HTTP {status}). "
                f"Check {self.member.api_key_env or 'api_key'}."
            )
        elif kind is ErrorKind.NOT_FOUND:
            text = (
                f"member '{self.name}' returned HTTP 404 for {operation}. "
                f"Check base_url ({self.member.base_url}) and model ({self.member.model})."
            )
        elif kind is ErrorKind.RATE_LIMIT:
            text = f"member '{self.name}' is rate limited (HTTP 429)."
        elif kind is ErrorKind.BAD_REQUEST:
            text = f"member '{self.name}' rejected the request (HTTP {status}): {message}"
        elif kind is ErrorKind.TIMEOUT:
            text = f"member '{self.name}' timed out (HTTP {status})."
        else:
            text = f"member '{self.name}' failed (HTTP {status}): {message}"
        return ProviderError(
            text,
            kind=kind,
            status=status,
            retryable=retryable,
            detail=detail,
            retry_after=retry_after,
        )

    def _retry_delay(self, attempt: int, error: ProviderError) -> float:
        if error.retry_after is not None:
            return min(error.retry_after, 30.0)
        base = min(0.5 * (2.0**attempt), 8.0)
        return float(base + random.uniform(0.0, 0.25))

    def _safe_host(self) -> str:
        parts = urlsplit(self.member.base_url)
        return f"{parts.scheme}://{parts.netloc}"


def _classify_status(status: int) -> tuple[ErrorKind, bool]:
    if status in {401, 403}:
        return ErrorKind.AUTH, False
    if status == 404:
        return ErrorKind.NOT_FOUND, False
    if status == 429:
        return ErrorKind.RATE_LIMIT, True
    if status in {408, 504}:
        return ErrorKind.TIMEOUT, True
    if 500 <= status < 600:
        return ErrorKind.SERVER, True
    if 400 <= status < 500:
        return ErrorKind.BAD_REQUEST, False
    return ErrorKind.SERVER, False


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _extract_error_message(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return _clean_detail(error["message"])
        if isinstance(error, str):
            return _clean_detail(error)
        if isinstance(data.get("message"), str):
            return _clean_detail(data["message"])
    return None


def _normalise_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return str(content)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _clean_detail(text: str, limit: int = 600) -> str:
    cleaned = _SECRET_PATTERN.sub("sk-***", text).replace("\n", " ").strip()
    return cleaned[:limit]


def _model_matches(configured: str, candidate: str) -> bool:
    if configured == candidate:
        return True
    return candidate in {f"{configured}:latest"} or configured in {f"{candidate}:latest"}
