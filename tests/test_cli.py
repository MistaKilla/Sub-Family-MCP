"""Console commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sub_family.cli import app

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SUB_FAMILY_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("SUB_FAMILY_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SUB_FAMILY_CONFIG", raising=False)
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    return config_dir


def test_init_list_validate(cli_env: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (cli_env / "family.yaml").exists()

    listing = runner.invoke(app, ["list", "--json"])
    assert listing.exit_code == 0, listing.output
    payload = json.loads(listing.stdout)
    assert {member["name"] for member in payload} == {"big_brother", "lilbro"}

    validate = runner.invoke(app, ["config", "validate"])
    assert validate.exit_code == 0
    assert "2 member(s)" in validate.output


def test_custom_config_path(cli_env: Path, tmp_path: Path) -> None:
    custom = tmp_path / "custom.yaml"
    result = runner.invoke(app, ["--config", str(custom), "init"])
    assert result.exit_code == 0, result.output
    assert custom.exists()
    listing = runner.invoke(app, ["--config", str(custom), "list"])
    assert listing.exit_code == 0
    assert "big_brother" in listing.output


def test_add_show_edit_remove(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    added = runner.invoke(
        app,
        [
            "members",
            "add",
            "farm_two",
            "--base-url",
            "http://192.168.1.51:11434",
            "--model",
            "qwen2.5-coder:7b",
            "--trust",
            "lan",
            "--capability",
            "code",
            "--capability",
            "fast",
            "--max-clones",
            "2",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "farm_two" in added.output

    show = runner.invoke(app, ["members", "show", "farm_two", "--json"])
    assert show.exit_code == 0, show.output
    payload = json.loads(show.stdout)
    assert payload["trust"] == "private_network"
    assert payload["capabilities"] == ["code", "fast"]

    edited = runner.invoke(
        app, ["members", "edit", "farm_two", "--timeout", "180", "--trust", "cloud"]
    )
    assert edited.exit_code == 0, edited.output
    show_again = runner.invoke(app, ["members", "show", "farm_two", "--json"])
    payload = json.loads(show_again.stdout)
    assert payload["timeout_s"] == 180
    assert payload["trust"] == "untrusted_cloud"

    removed = runner.invoke(app, ["members", "remove", "farm_two", "--yes"])
    assert removed.exit_code == 0, removed.output
    listing = runner.invoke(app, ["list"])
    assert "farm_two" not in listing.output


def test_add_duplicate_fails(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "members",
            "add",
            "lilbro",
            "--base-url",
            "http://localhost:1",
            "--model",
            "m",
        ],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_invalid_trust_is_reported(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "members",
            "add",
            "farm",
            "--base-url",
            "http://localhost:1",
            "--model",
            "m",
            "--trust",
            "nonsense",
        ],
    )
    assert result.exit_code == 1
    assert "unknown trust" in result.output


def test_config_show_masks_key(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "members",
            "add",
            "keyed",
            "--base-url",
            "https://example.com",
            "--model",
            "m",
            "--api-key",
            "super-secret-value",
        ],
    )
    assert result.exit_code == 0, result.output
    shown = runner.invoke(app, ["config", "show"])
    assert shown.exit_code == 0
    assert "super-secret-value" not in shown.output
    assert "***" in shown.output


def test_config_validate_rejects_broken_yaml(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    (cli_env / "family.yaml").write_text("version: 1\nmembers: [\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "invalid YAML" in result.output


def test_doctor_and_paths(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "Session database" in doctor.output
    paths = runner.invoke(app, ["paths", "--json"])
    assert paths.exit_code == 0
    payload = json.loads(paths.stdout)
    assert "config_file" in payload


def test_sessions_empty_and_missing(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    listing = runner.invoke(app, ["sessions", "list"])
    assert listing.exit_code == 0
    assert "No sessions stored" in listing.output
    missing = runner.invoke(app, ["sessions", "show", "lilbro", "nope"])
    assert missing.exit_code == 1
    assert "was not found" in missing.output


def test_disable_enable(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    disabled = runner.invoke(app, ["members", "disable", "lilbro"])
    assert disabled.exit_code == 0
    listing = runner.invoke(app, ["list", "--json"])
    payload = {item["name"]: item for item in json.loads(listing.stdout)}
    assert payload["lilbro"]["enabled"] is False
    runner.invoke(app, ["members", "enable", "lilbro"])
    listing = runner.invoke(app, ["list", "--json"])
    payload = {item["name"]: item for item in json.loads(listing.stdout)}
    assert payload["lilbro"]["enabled"] is True


def test_claude_snippet(cli_env: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["claude", "snippet", "--env", "DEEPSEEK_API_KEY"])
    assert result.exit_code == 0
    assert "claude mcp add" in result.output
    assert "DEEPSEEK_API_KEY" in result.output
    guide = runner.invoke(app, ["claude", "guide"])
    assert guide.exit_code == 0
    assert "Sub-Family delegation" in guide.output
