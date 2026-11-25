"""Orchestration: ask, sessions, guard, swarm, and status."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import write_image

from sub_family.errors import (
    MemberDisabled,
    MemberNotFound,
    PolicyError,
    SessionError,
    ToolInputError,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload"


async def test_ask_stateless(service) -> None:
    svc, provider = service
    result = await svc.ask("lilbro", "hello")
    assert result.member == "lilbro"
    assert result.content == "fake answer:hello"
    assert result.session_id is None
    assert result.history_messages == 1
    assert result.usage["total_tokens"] == 15
    assert len(provider.calls) == 1


async def test_ask_with_session_replays_history(service) -> None:
    svc, provider = service
    first = await svc.ask("lilbro", "one", session_id="demo")
    assert first.history_messages == 2
    second = await svc.ask("lilbro", "two", session_id="demo")
    assert second.history_messages == 4
    assert len(provider.calls) == 2
    assert len(provider.calls[1]) == 3  # two stored + new user message
    assert provider.calls[1][0]["content"] == "one"
    assert provider.calls[1][1]["content"].startswith("fake answer:one")


async def test_sessions_are_isolated_in_service(service) -> None:
    svc, _ = service
    await svc.ask("lilbro", "alpha", session_id="a")
    await svc.ask("lilbro", "beta", session_id="b")
    sessions = await svc.list_sessions("lilbro")
    assert {item["session_id"] for item in sessions} == {"a", "b"}
    history = await svc.session_history("lilbro", "a")
    assert history[0]["content"] == "alpha"
    removed = await svc.reset_session("lilbro", "a")
    assert removed["messages"] == 2
    with pytest.raises(SessionError):
        await svc.session_history("lilbro", "a")
    cleared = await svc.reset_session("lilbro", "*")
    assert cleared["sessions"] == 1
    assert cleared["messages"] == 2


async def test_redaction_before_cloud_call(service) -> None:
    svc, provider = service
    svc._key_errors.clear()  # type: ignore[attr-defined]
    secret = "sk-abcdefghijklmnopqrstuvwx"
    result = await svc.ask("big_brother", f"deploy with {secret}")
    sent = provider.calls[0][-1]["content"]
    assert secret not in sent
    assert "REDACTED" in sent
    assert result.redactions
    assert any("redacted" in warning for warning in result.warnings)


async def test_system_prompt_is_merged_and_scrubbed(service) -> None:
    svc, provider = service
    result = await svc.ask("lilbro", "hello", system="You are terse. TOKEN=supersecretvalue123")
    del result
    assert provider.calls[0][0] == {
        "role": "system",
        "content": "You are terse. TOKEN=supersecretvalue123",
    }


async def test_images_require_vision(service) -> None:
    svc, _ = service
    with pytest.raises(ToolInputError) as error:
        await svc.ask("big_brother", "look", images=["shot.png"])
    assert "vision" in str(error.value)


async def test_image_is_encoded_for_vision_member(service, tmp_path: Path) -> None:
    svc, provider = service
    path = write_image(tmp_path / "shot.png", PNG_BYTES)
    result = await svc.ask("lilbro", "what is this?", images=[str(path)])
    assert result.images == 1
    content = provider.calls[0][-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_image_history_is_stored_as_text_reference(service, tmp_path: Path) -> None:
    svc, provider = service
    path = write_image(tmp_path / "shot.png", PNG_BYTES)
    await svc.ask("lilbro", "look", images=[str(path)], session_id="vision")
    await svc.ask("lilbro", "and now?", session_id="vision")
    history = provider.calls[1]
    user_messages = [message for message in history if message["role"] == "user"]
    assert isinstance(user_messages[0]["content"], str)
    assert "[attached image:" in user_messages[0]["content"]


async def test_empty_prompt_is_rejected(service) -> None:
    svc, _ = service
    with pytest.raises(ToolInputError):
        await svc.ask("lilbro", "   ")


async def test_sampling_overrides_are_validated(service) -> None:
    svc, _ = service
    with pytest.raises(ToolInputError):
        await svc.ask("lilbro", "hi", temperature=3)
    with pytest.raises(ToolInputError):
        await svc.ask("lilbro", "hi", max_tokens=0)


async def test_disabled_member_is_rejected(service) -> None:
    svc, _ = service
    svc.config = svc.config.model_copy(
        update={
            "members": {
                **svc.config.members,
                "lilbro": svc.config.members["lilbro"].model_copy(update={"enabled": False}),
            }
        }
    )
    with pytest.raises(MemberDisabled):
        await svc.ask("lilbro", "hello")


async def test_context_window_guard(service) -> None:
    svc, _ = service
    svc.config = svc.config.model_copy(
        update={
            "members": {
                **svc.config.members,
                "lilbro": svc.config.members["lilbro"].model_copy(update={"context_window": 256}),
            }
        }
    )
    with pytest.raises(ToolInputError) as error:
        await svc.ask("lilbro", "x" * 2000)
    assert "context_window" in str(error.value)


async def test_swarm_returns_all_branches(service) -> None:
    svc, provider = service
    result = await svc.swarm("lilbro", ["a", "b", "c"])
    assert result.ok_count == 3
    assert result.failed_count == 0
    assert [item.content for item in result.results] == [
        "fake answer:a",
        "fake answer:b",
        "fake answer:c",
    ]
    assert provider.max_active <= 2  # lilbro max_clones


async def test_swarm_is_partial_failure_tolerant(service) -> None:
    svc, provider = service
    provider.fail_on = "FAIL"
    result = await svc.swarm("lilbro", ["ok one", "FAIL this", "ok two"])
    assert result.ok_count == 2
    assert result.failed_count == 1
    failed = next(item for item in result.results if not item.ok)
    assert failed.error
    assert "synthetic" in failed.error
    assert result.to_dict()["summary"] == {"total": 3, "succeeded": 2, "failed": 1}


async def test_swarm_session_prefix_creates_isolated_sessions(service) -> None:
    svc, provider = service
    await svc.swarm("lilbro", ["a", "b"], session_prefix="audit")
    sessions = await svc.list_sessions("lilbro")
    assert {item["session_id"] for item in sessions} == {"audit-1", "audit-2"}
    await svc.swarm("lilbro", ["next"], session_prefix="audit")
    # Branch 1 continues its own history.
    user_messages = [m for m in provider.calls[-1] if m["role"] == "user"]
    assert len(user_messages) == 2


async def test_swarm_limits_and_validation(service) -> None:
    svc, _ = service
    with pytest.raises(ToolInputError):
        await svc.swarm("lilbro", [])
    with pytest.raises(ToolInputError):
        await svc.swarm("lilbro", ["a", " "])
    with pytest.raises(ToolInputError):
        await svc.swarm("lilbro", [f"task {i}" for i in range(13)])
    with pytest.raises(ToolInputError):
        await svc.swarm("lilbro", ["a", "b"], max_concurrency=3)


async def test_swarm_synthesis(service) -> None:
    svc, provider = service
    result = await svc.swarm("lilbro", ["a", "b"], synthesize=True, context="why")
    assert result.synthesis
    assert "why" in str(provider.calls[-1][-1]["content"])


async def test_status_reports_key_error_and_health(service) -> None:
    svc, _ = service
    statuses = {status.name: status for status in await svc.status()}
    assert statuses["lilbro"].ok
    assert statuses["big_brother"].ok is False
    assert "TEST_API_KEY" in (statuses["big_brother"].error or "")


async def test_status_uses_provider_health(service) -> None:
    svc, provider = service
    provider.health_models = ["other-model"]
    statuses = await svc.status("lilbro")
    assert statuses[0].ok
    assert statuses[0].model_available is False


async def test_list_members_exposes_no_secrets(service) -> None:
    svc, _ = service
    members = svc.list_members()
    serialised = str(members)
    assert "TEST_API_KEY" in serialised
    assert "sk-" not in serialised


async def test_policy_blocks_prompt(service) -> None:
    svc, _ = service
    svc.guard.config = svc.guard.config.model_copy(update={"blocked_patterns": ["TOP-SECRET"]})
    svc.guard = type(svc.guard)(svc.guard.config)
    with pytest.raises(PolicyError):
        await svc.ask("lilbro", "TOP-SECRET plan")


async def test_missing_member(service) -> None:
    svc, _ = service
    with pytest.raises(MemberNotFound) as error:
        await svc.ask("nobody", "hi")
    assert "Available members" in str(error.value)
