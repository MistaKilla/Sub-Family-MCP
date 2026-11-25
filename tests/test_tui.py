"""Headless smoke tests for the Textual family editor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, Switch

from sub_family.config.loader import load_config
from sub_family.config.models import FamilyConfig, MemberConfig, TrustLevel
from sub_family.errors import ConfigError
from sub_family.providers.base import ProviderHealth
from sub_family.tui import app as tui_app
from sub_family.tui.app import (
    ConfirmScreen,
    FamilyEditor,
    MemberFormScreen,
    format_probe_result,
    parse_member_form,
)


def _family() -> FamilyConfig:
    return FamilyConfig(
        members={
            "lilbro": MemberConfig(
                base_url="http://127.0.0.1:11434",
                model="qwen-test",
                trust=TrustLevel.SAFE_LOCAL,
                capabilities=["chat", "vision"],
                max_clones=2,
            ),
            "big_brother": MemberConfig(
                base_url="https://api.example.com",
                model="big-test",
                api_key_env="TEST_API_KEY",
                trust=TrustLevel.UNTRUSTED_CLOUD,
                capabilities=["chat", "reasoning", "code"],
                max_clones=3,
            ),
        }
    )


def _cell(row: list[Any], index: int) -> str:
    return str(row[index])


def _set_input(screen: MemberFormScreen, selector: str, value: str) -> None:
    screen.query_one(selector, Input).value = value


async def _open_add(pilot: Any, app: FamilyEditor) -> MemberFormScreen:
    await pilot.press("a")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, MemberFormScreen)
    return screen


async def test_main_screen_lists_members(tmp_path: Path) -> None:
    config_path = tmp_path / "family.yaml"
    app = FamilyEditor(_family(), config_path)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#members", DataTable)
        assert table.row_count == 2
        assert [str(column.label) for column in table.columns.values()] == [
            "Name",
            "Model",
            "Trust",
            "Capabilities",
            "Clones",
            "State",
        ]
        first = table.get_row_at(0)
        assert _cell(first, 0) == "lilbro"
        assert _cell(first, 1) == "qwen-test"
        assert _cell(first, 2) == "safe_local"
        assert _cell(first, 3) == "chat, vision"
        assert _cell(first, 4) == "2"
        assert _cell(first, 5) == "enabled"
        assert _cell(table.get_row_at(1), 0) == "big_brother"

        status = str(app.query_one("#status", Static).render())
        assert str(config_path) in status
        assert "saved" in status
        assert not app.query_one("#empty", Static).display
        assert app.dirty is False


async def test_add_member_then_save(tmp_path: Path) -> None:
    config_path = tmp_path / "family.yaml"
    app = FamilyEditor(_family(), config_path)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        screen = await _open_add(pilot, app)
        _set_input(screen, "#name", "medium")
        _set_input(screen, "#description", "Balanced helper")
        _set_input(screen, "#base_url", "http://127.0.0.1:8000")
        _set_input(screen, "#model", "mid-model")
        screen.query_one("#trust", Select).value = TrustLevel.PRIVATE_NETWORK.value
        screen.query_one("#cap-vision", Checkbox).value = True
        screen.query_one("#extra_capabilities", Input).value = "json_mode"
        _set_input(screen, "#max_clones", "4")
        _set_input(screen, "#timeout_s", "45")
        screen.query_one("#form-save", Button).press()
        await pilot.pause()

        assert not isinstance(app.screen, MemberFormScreen)
        assert "medium" in app.config.members
        added = app.config.members["medium"]
        assert added.model == "mid-model"
        assert added.trust is TrustLevel.PRIVATE_NETWORK
        assert set(added.capabilities) == {"chat", "vision", "json_mode"}
        assert added.max_clones == 4
        assert added.timeout_s == 45.0
        assert app.dirty is True
        assert "unsaved" in str(app.query_one("#status", Static).render())
        assert app.query_one("#members", DataTable).row_count == 3

        # Save via the keyboard binding and verify the YAML on disk.
        await pilot.press("s")
        await pilot.pause()
        assert app.dirty is False
        assert config_path.exists()
        saved_text = config_path.read_text(encoding="utf-8")
        assert "medium" in saved_text
        assert "mid-model" in saved_text
        reloaded = load_config(config_path)
        assert reloaded.members["medium"].capabilities == ["chat", "vision", "json_mode"]
        assert reloaded.members["medium"].timeout_s == 45.0


async def test_add_form_rejects_invalid_name_and_base_url(tmp_path: Path) -> None:
    app = FamilyEditor(_family(), tmp_path / "family.yaml")

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        screen = await _open_add(pilot, app)
        _set_input(screen, "#name", "Bad Name")
        _set_input(screen, "#base_url", "not-a-url")
        _set_input(screen, "#model", "m")
        screen.query_one("#form-save", Button).press()
        await pilot.pause()

        assert app.screen is screen
        error = str(screen.query_one("#form-error", Static).render())
        assert "Name must" in error
        assert "Bad Name" not in app.config.members
        assert app.dirty is False

        _set_input(screen, "#name", "valid_name")
        screen.query_one("#form-save", Button).press()
        await pilot.pause()

        assert app.screen is screen
        error = str(screen.query_one("#form-error", Static).render())
        assert "base_url" in error
        assert "valid_name" not in app.config.members
        assert not (tmp_path / "family.yaml").exists()

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MemberFormScreen)
        assert list(app.config.members) == ["lilbro", "big_brother"]


async def test_delete_requires_confirmation_and_mentions_sessions(tmp_path: Path) -> None:
    app = FamilyEditor(_family(), tmp_path / "family.yaml")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        message = str(confirm.query_one("#confirm-message", Static).render())
        assert "sessions are not deleted" in message

        # Cancel first: nothing changes.
        await pilot.press("n")
        await pilot.pause()
        assert "lilbro" in app.config.members
        assert app.dirty is False

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()

        assert "lilbro" not in app.config.members
        assert app.query_one("#members", DataTable).row_count == 1
        assert app.dirty is True


async def test_edit_preserves_fields_not_in_the_form(tmp_path: Path) -> None:
    preserved = MemberConfig(
        base_url="http://127.0.0.1:1234",
        model="preserved-model",
        api_key=SecretStr("super-secret"),
        api_key_header="X-Api-Key",
        api_key_prefix="",
        chat_path="/v1/chat/completions",
        headers={"X-Custom": "yes"},
        extra_body={"top_k": 5},
        max_tokens=512,
        temperature=0.2,
        context_window=8192,
    )
    app = FamilyEditor(FamilyConfig(members={"preserved": preserved}), tmp_path / "family.yaml")

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MemberFormScreen)
        assert screen.query_one("#name", Input).disabled is True
        assert screen.query_one("#name", Input).value == "preserved"
        assert screen.query_one("#model", Input).value == "preserved-model"

        _set_input(screen, "#description", "Edited in the TUI")
        screen.query_one("#form-save", Button).press()
        await pilot.pause()

        edited = app.config.members["preserved"]
        assert edited.description == "Edited in the TUI"
        assert edited.chat_path == "/v1/chat/completions"
        assert edited.headers == {"X-Custom": "yes"}
        assert edited.extra_body == {"top_k": 5}
        assert edited.api_key_header == "X-Api-Key"
        assert edited.api_key_prefix == ""
        assert edited.max_tokens == 512
        assert edited.temperature == 0.2
        assert edited.context_window == 8192
        assert edited.api_key is not None
        assert edited.api_key.get_secret_value() == "super-secret"

        # The literal key must also survive the full save/reload round trip.
        await pilot.press("s")
        await pilot.pause()
        reloaded = load_config(tmp_path / "family.yaml")
        stored = reloaded.members["preserved"].api_key
        assert stored is not None
        assert stored.get_secret_value() == "super-secret"


async def test_edit_can_clear_stored_api_key(tmp_path: Path) -> None:
    member = MemberConfig(
        base_url="http://127.0.0.1:9999",
        model="keyed-model",
        api_key=SecretStr("remove-me"),
    )
    config_path = tmp_path / "family.yaml"
    app = FamilyEditor(FamilyConfig(members={"keyed": member}), config_path)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MemberFormScreen)
        clear_switch = screen.query_one("#clear_api_key", Switch)
        assert clear_switch.value is False
        clear_switch.value = True
        screen.query_one("#form-save", Button).press()
        await pilot.pause()

        assert app.config.members["keyed"].api_key is None
        assert app.dirty is True

        await pilot.press("s")
        await pilot.pause()
        reloaded = load_config(config_path)
        assert reloaded.members["keyed"].api_key is None
        assert "remove-me" not in config_path.read_text(encoding="utf-8")


async def test_test_connection_reports_missing_key_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(name: str, member: MemberConfig, **_: Any) -> ProviderHealth:
        raise ConfigError("member 'x' expects the API key in $TEST_API_KEY, but it is not set")

    monkeypatch.setattr(tui_app, "probe_endpoint", fake_probe)
    app = FamilyEditor(_family(), tmp_path / "family.yaml")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "missing credentials" in app._notice
        assert "TEST_API_KEY" in app._notice


def test_parse_member_form_helpers() -> None:
    base = MemberConfig(
        base_url="http://localhost:1",
        model="m",
        headers={"X-Test": "1"},
        extra_body={"seed": 7},
        system_prompt="Be brief.",
    )
    values = {
        "name": "helper",
        "description": "d",
        "base_url": "http://localhost:2/",
        "model": "m2",
        "trust": "safe_local",
        "capabilities": ["chat", "tools"],
        "extra_capabilities": "fast, tools",
        "max_clones": "",
        "timeout_s": "",
        "retries": "",
        "api_key_env": "",
        "api_key": "",
        "system_prompt": "New prompt",
        "enabled": False,
        "verify_tls": False,
    }
    name, member = parse_member_form(values, base=base)
    assert name == "helper"
    assert member.base_url == "http://localhost:2"
    assert member.capabilities == ["chat", "tools", "fast"]
    assert member.max_clones == base.max_clones
    assert member.timeout_s == base.timeout_s
    assert member.retries == base.retries
    assert member.headers == {"X-Test": "1"}
    assert member.extra_body == {"seed": 7}
    assert member.enabled is False
    assert member.verify_tls is False

    with pytest.raises(tui_app.FormError):
        parse_member_form({**values, "name": "UPPER"})
    with pytest.raises(tui_app.FormError):
        parse_member_form({**values, "base_url": "ftp://nope"})
    with pytest.raises(tui_app.FormError):
        parse_member_form({**values, "max_clones": "many"})
    with pytest.raises(tui_app.FormError):
        parse_member_form({**values, "name": "lilbro"}, existing_names={"lilbro"})

    # The clear switch wins even if a replacement key was typed by mistake.
    _, cleared = parse_member_form(
        {**values, "api_key": "replacement", "clear_api_key": True}, base=base
    )
    assert cleared.api_key is None


def test_format_probe_result() -> None:
    assert format_probe_result(ProviderHealth(ok=True, latency_ms=12, model_available=True)) == (
        "12 ms · configured model available"
    )
    assert "not listed" in format_probe_result(
        ProviderHealth(ok=True, latency_ms=9, model_available=False)
    )
    assert "boom" in format_probe_result(ProviderHealth(ok=False, latency_ms=10, error="boom"))
