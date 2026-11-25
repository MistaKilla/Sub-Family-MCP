"""OpenAI-compatible provider behaviour against mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from sub_family.config.models import MemberConfig
from sub_family.errors import ErrorKind, ProviderError
from sub_family.providers.openai_compat import OpenAICompatProvider


def make_member(**overrides: object) -> MemberConfig:
    data: dict[str, object] = {
        "base_url": "https://api.example.com",
        "model": "model-a",
        "api_key_env": "TEST_KEY",
    }
    data.update(overrides)
    return MemberConfig.model_validate(data)


def make_provider(
    member: MemberConfig | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    retries: int | None = None,
) -> OpenAICompatProvider:
    member = member or make_member()
    if retries is not None:
        member = member.model_copy(update={"retries": retries})
    return OpenAICompatProvider(
        "big_brother",
        member,
        api_key="test-key",
        client=client or httpx.AsyncClient(),
        sleep=_no_sleep,
    )


async def _no_sleep(_: float) -> None:
    return None


def test_chat_url_variants() -> None:
    cases = {
        "https://api.example.com": "https://api.example.com/v1/chat/completions",
        "https://api.example.com/": "https://api.example.com/v1/chat/completions",
        "https://api.example.com/v1": "https://api.example.com/v1/chat/completions",
        "https://api.example.com/v1/": "https://api.example.com/v1/chat/completions",
        "http://192.168.1.5:11434": "http://192.168.1.5:11434/v1/chat/completions",
        "https://api.example.com/chat/completions": ("https://api.example.com/chat/completions"),
    }
    for base_url, expected in cases.items():
        provider = make_provider(make_member(base_url=base_url))
        assert provider.chat_url == expected


def test_chat_path_override() -> None:
    provider = make_provider(
        make_member(chat_path="/custom/chat", base_url="https://api.example.com")
    )
    assert provider.chat_url == "https://api.example.com/custom/chat"


@respx.mock
async def test_successful_completion() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [
                    {
                        "message": {"content": "hello", "reasoning_content": "thinking"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        )
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        result = await provider.complete([{"role": "user", "content": "hi"}])
    assert route.called
    assert result.content == "hello"
    assert result.reasoning == "thinking"
    assert result.usage.total_tokens == 16
    assert result.model == "model-a"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer test-key"
    assert b'"model":"model-a"' in request.content


@respx.mock
async def test_content_parts_are_joined() -> None:
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "a"},
                                {"type": "text", "text": "b"},
                            ]
                        }
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        result = await provider.complete([])
    assert result.content == "ab"


@respx.mock
async def test_auth_error_is_not_retried() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=2)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.AUTH
    assert error.value.status == 401
    assert not error.value.retryable
    assert route.call_count == 1


@respx.mock
async def test_retry_after_rate_limit_then_success() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ]
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=2)
        result = await provider.complete([])
    assert result.content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_server_error_exhausts_retries() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=1)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.SERVER
    assert route.call_count == 2


@respx.mock
async def test_timeout_is_classified() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions")
    route.side_effect = httpx.ConnectTimeout("too slow")
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=0)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.TIMEOUT


@respx.mock
async def test_connection_error_is_classified() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions")
    route.side_effect = httpx.ConnectError("refused")
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=0)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.CONNECTION
    assert "unreachable" in str(error.value)


@respx.mock
async def test_missing_model_is_classified() -> None:
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(404, text="not found")
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=0)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.NOT_FOUND
    assert "base_url" in str(error.value)


@respx.mock
async def test_empty_response_is_a_bad_response() -> None:
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {}}]})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client, retries=0)
        with pytest.raises(ProviderError) as error:
            await provider.complete([])
    assert error.value.kind is ErrorKind.BAD_RESPONSE


@respx.mock
async def test_list_models_and_health() -> None:
    respx.get("https://api.example.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        models = await provider.list_models()
        health = await provider.health()
    assert models == ["model-a", "model-b"]
    assert health.ok
    assert health.model_available is True


@respx.mock
async def test_health_reports_missing_model() -> None:
    respx.get("https://api.example.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "other"}]})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        health = await provider.health()
    assert health.ok
    assert health.model_available is False


@respx.mock
async def test_health_reports_failure() -> None:
    respx.get("https://api.example.com/v1/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        health = await provider.health()
    assert not health.ok
    assert health.error
    assert "API key" in health.error


@respx.mock
async def test_extra_body_cannot_override_messages() -> None:
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    async with httpx.AsyncClient() as client:
        provider = make_provider(client=client)
        await provider.complete(
            [{"role": "user", "content": "hi"}],
            extra_body={"messages": "evil", "top_p": 0.5},
        )
    payload = route.calls.last.request.content
    assert b'"messages":' in payload
    assert b'"top_p":0.5' in payload
    assert b"evil" not in payload


def test_custom_auth_headers() -> None:
    member = make_member(
        api_key_header="X-API-Key",
        api_key_prefix="",
        headers={"X-Extra": "1"},
    )
    provider = make_provider(member)
    headers = provider._headers()
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Extra"] == "1"
