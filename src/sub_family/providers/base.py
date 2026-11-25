"""Provider-neutral result types and the adapter protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class Usage:
    """Token accounting as reported by the provider (may be partially absent)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        """JSON-friendly representation."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class ChatResult:
    """A single completion returned by a member."""

    content: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    reasoning: str | None = None
    elapsed_ms: int = 0


@dataclass(slots=True)
class ProviderHealth:
    """Result of a reachability probe (``GET /models`` by default)."""

    ok: bool
    latency_ms: int
    model_available: bool | None = None
    models: list[str] = field(default_factory=list)
    error: str | None = None
    status_code: int | None = None


class ChatProvider(Protocol):
    """Minimal interface the orchestrator needs from a backend."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        """Send a chat completion request."""
        ...

    async def health(self) -> ProviderHealth:
        """Probe the endpoint without spending generation tokens."""
        ...
