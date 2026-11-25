"""Shared fixtures for Sub-Family tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from sub_family.config.models import FamilyConfig, MemberConfig, SessionsConfig
from sub_family.providers.base import ChatResult, ProviderHealth, Usage
from sub_family.service import FamilyService

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-image-payload"


@pytest.fixture
def local_member() -> MemberConfig:
    return MemberConfig(
        base_url="http://127.0.0.1:11434",
        model="qwen-test",
        trust="safe_local",
        capabilities=["chat", "vision"],
        max_clones=2,
    )


@pytest.fixture
def cloud_member() -> MemberConfig:
    return MemberConfig(
        base_url="https://api.example.com",
        model="big-test",
        api_key_env="TEST_API_KEY",
        trust="untrusted_cloud",
        capabilities=["chat", "reasoning", "code"],
        max_clones=3,
    )


@pytest.fixture
def family_config(
    tmp_path: Path, local_member: MemberConfig, cloud_member: MemberConfig
) -> FamilyConfig:
    return FamilyConfig(
        members={"lilbro": local_member, "big_brother": cloud_member},
        sessions=SessionsConfig(db_path=str(tmp_path / "sessions.db")),
    )


@dataclass
class FakeProvider:
    """Deterministic provider used by service and MCP tests."""

    content: str = "fake answer"
    fail_on: str | None = None
    delay: float = 0.0
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    health_ok: bool = True
    health_error: str | None = None
    health_models: list[str] = field(default_factory=lambda: ["qwen-test"])

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        self.calls.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            user_text = _last_user_text(messages)
            if self.fail_on and self.fail_on in user_text:
                from sub_family.errors import ErrorKind, ProviderError

                raise ProviderError(
                    "synthetic failure",
                    kind=ErrorKind.SERVER,
                    retryable=False,
                )
            if self.delay:
                await asyncio.sleep(self.delay)
            return ChatResult(
                content=f"{self.content}:{user_text}",
                model="fake-model",
                usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="stop",
                elapsed_ms=7,
            )
        finally:
            self.active -= 1

    async def health(self) -> ProviderHealth:
        if not self.health_ok:
            return ProviderHealth(
                ok=False, latency_ms=1, error=self.health_error or "down", status_code=500
            )
        return ProviderHealth(
            ok=True,
            latency_ms=3,
            model_available="qwen-test" in self.health_models,
            models=list(self.health_models),
        )


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


@pytest.fixture
async def service(
    family_config: FamilyConfig, tmp_path: Path
) -> AsyncIterator[tuple[FamilyService, FakeProvider]]:
    provider = FakeProvider()
    clients: list[httpx.AsyncClient] = []

    def client_factory(member: MemberConfig) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        )
        clients.append(client)
        return client

    def provider_factory(
        name: str, member: MemberConfig, key: str | None, client: httpx.AsyncClient
    ) -> FakeProvider:
        return provider

    svc = FamilyService(
        family_config,
        config_path=tmp_path / "family.yaml",
        client_factory=client_factory,
        provider_factory=provider_factory,  # type: ignore[arg-type]
    )
    await svc.start()
    try:
        yield svc, provider
    finally:
        await svc.aclose()
        for client in clients:
            await client.aclose()


def write_image(path: Path, data: bytes = PNG_BYTES) -> Path:
    path.write_bytes(data)
    return path


def json_content(result: Any) -> Any:
    """Extract the JSON payload from an MCP CallToolResult."""
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            return json.loads(text)
    raise AssertionError("tool result has no text content")


@pytest.fixture(autouse=True)
def _neutralise_test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the cloud fixture deterministic regardless of the host environment."""
    monkeypatch.delenv("TEST_API_KEY", raising=False)
