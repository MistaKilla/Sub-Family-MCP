"""Session persistence, isolation, and sliding-window trimming."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sub_family.config.models import SessionsConfig
from sub_family.errors import SessionError
from sub_family.sessions.store import SessionStore, preview_content, validate_session_id


def make_store(path: Path, **overrides: object) -> SessionStore:
    data: dict[str, object] = {
        "max_messages": 40,
        "max_chars": 80_000,
        "ttl_days": 30,
    }
    data.update(overrides)
    return SessionStore(path, SessionsConfig.model_validate(data))


async def test_append_and_read(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db")
    await store.open()
    try:
        result = await store.append_exchange(
            "big_brother", "s1", "hello", "world", prompt_tokens=3, completion_tokens=2
        )
        assert result.message_count == 2
        messages = await store.get_messages("big_brother", "s1")
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[0].content == "hello"
        assert messages[1].content == "world"
        assert messages[0].prompt_tokens == 3
        assert messages[1].completion_tokens == 2
    finally:
        await store.close()


async def test_sessions_are_isolated(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db")
    await store.open()
    try:
        await store.append_exchange("m", "a", "first", "one")
        await store.append_exchange("m", "b", "second", "two")
        await store.append_exchange("other", "a", "third", "three")
        assert (await store.get_messages("m", "a"))[0].content == "first"
        assert (await store.get_messages("m", "b"))[0].content == "second"
        assert (await store.get_messages("other", "a"))[0].content == "third"
        assert len(await store.list_sessions()) == 3
        assert len(await store.list_sessions("m")) == 2
    finally:
        await store.close()


async def test_message_window_trimming(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db", max_messages=4)
    await store.open()
    try:
        await store.append_exchange("m", "s", "q1", "a1")
        await store.append_exchange("m", "s", "q2", "a2")
        result = await store.append_exchange("m", "s", "q3", "a3")
        assert result.trimmed == 2
        messages = await store.get_messages("m", "s")
        assert [message.content for message in messages] == ["q2", "a2", "q3", "a3"]
        assert messages[0].role == "user"
    finally:
        await store.close()


async def test_char_window_trimming_keeps_newest(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db", max_chars=1000)
    await store.open()
    try:
        await store.append_exchange("m", "s", "x" * 400, "y" * 400)
        result = await store.append_exchange("m", "s", "z" * 400, "w" * 400)
        assert result.trimmed == 2
        messages = await store.get_messages("m", "s")
        assert len(messages) == 2
        assert messages[0].content == "z" * 400
    finally:
        await store.close()


async def test_history_limit(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db")
    await store.open()
    try:
        for index in range(5):
            await store.append_exchange("m", "s", f"q{index}", f"a{index}")
        messages = await store.get_history("m", "s", limit=3)
        assert [message.content for message in messages] == ["a3", "q4", "a4"]
    finally:
        await store.close()


async def test_reset_one_and_all(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db")
    await store.open()
    try:
        await store.append_exchange("m", "a", "q", "a")
        await store.append_exchange("m", "b", "q", "a")
        assert await store.reset_session("m", "a") == 2
        assert await store.get_messages("m", "a") == []
        assert len(await store.get_messages("m", "b")) == 2
        assert await store.reset_member_sessions("m") == 2
        assert await store.list_sessions("m") == []
    finally:
        await store.close()


async def test_prune_removes_old_sessions(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = make_store(path, ttl_days=1)
    await store.open()
    try:
        await store.append_exchange("m", "old", "q", "a")
        await store.append_exchange("m", "new", "q", "a")
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE sessions SET updated_at = '2000-01-01T00:00:00+00:00' "
                "WHERE session_id = 'old'"
            )
            connection.commit()
        finally:
            connection.close()
        assert await store.prune(1) == 1
        assert [info.session_id for info in await store.list_sessions()] == ["new"]
    finally:
        await store.close()


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    first = make_store(path)
    await first.open()
    await first.append_exchange("m", "s", "q", "a")
    await first.close()

    second = make_store(path)
    await second.open()
    try:
        messages = await second.get_messages("m", "s")
        assert len(messages) == 2
    finally:
        await second.close()


def test_session_id_validation() -> None:
    assert validate_session_id(" db-refactor.v2 ") == "db-refactor.v2"
    for bad in ["", "../etc/passwd", "a" * 65, "!bad"]:
        with pytest.raises(SessionError):
            validate_session_id(bad)


def test_preview_content_variants() -> None:
    assert preview_content("hello   world") == "hello world"
    assert (
        preview_content([{"type": "text", "text": "look"}, {"type": "image_url"}]) == "look [image]"
    )
    assert preview_content("x" * 500).endswith("\u2026")


async def test_operations_require_open(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.db")
    with pytest.raises(SessionError):
        await store.get_messages("m", "s")
