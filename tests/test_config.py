"""Configuration validation and persistence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from sub_family.config.loader import (
    default_family,
    load_config,
    render_config_yaml,
    resolve_api_key,
    resolve_db_path,
    save_config,
    write_example_config,
)
from sub_family.config.models import CostTier, FamilyConfig, MemberConfig, TrustLevel
from sub_family.errors import ConfigError, MemberNotFound


def test_default_family_is_valid() -> None:
    config = default_family()
    assert set(config.members) == {"big_brother", "lilbro"}
    assert config.members["big_brother"].trust is TrustLevel.UNTRUSTED_CLOUD
    assert config.members["lilbro"].has_vision
    assert config.sessions.enabled


def test_config_is_strict_about_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "family.yaml"
    path.write_text(
        "version: 1\nmembers:\n  a:\n    base_url: http://localhost:1\n"
        "    model: m\n    max_clone: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "max_clone" in str(error.value)


def test_member_names_must_be_safe() -> None:
    with pytest.raises(ValueError):
        FamilyConfig.model_validate(
            {
                "version": 1,
                "members": {
                    "Bad Name": {"base_url": "http://localhost:1", "model": "m"},
                },
            }
        )


def test_base_url_rejects_credentials() -> None:
    with pytest.raises(ValueError):
        MemberConfig(base_url="https://user:pass@example.com", model="m")


def test_base_url_requires_http() -> None:
    with pytest.raises(ValueError):
        MemberConfig(base_url="localhost:11434", model="m")


def test_base_url_rejects_whitespace() -> None:
    with pytest.raises(ValueError):
        MemberConfig(base_url="http://exa mple.com", model="m")


def test_yaml_roundtrip_preserves_secret_and_masks_it(tmp_path: Path) -> None:
    config = FamilyConfig.model_validate(
        {
            "version": 1,
            "members": {
                "a": {
                    "base_url": "https://example.com",
                    "model": "m",
                    "api_key": "super-secret-key",
                    "trust": "safe_local",
                }
            },
        }
    )
    path = tmp_path / "family.yaml"
    save_config(config, path)
    text = path.read_text(encoding="utf-8")
    assert "super-secret-key" in text
    loaded = load_config(path)
    assert loaded.members["a"].api_key is not None
    assert loaded.members["a"].api_key.get_secret_value() == "super-secret-key"
    assert loaded.masked_dump()["members"]["a"]["api_key"] == "***"
    assert "super-secret-key" not in yaml.safe_dump(loaded.masked_dump())


def test_save_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "family.yaml"
    write_example_config(path)
    original = path.read_text(encoding="utf-8")
    config = default_family()
    save_config(config, path)
    backup = path.with_suffix(".yaml.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_save_sets_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "family.yaml"
    save_config(default_family(), path)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_example_config_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "family.yaml"
    write_example_config(path)
    with pytest.raises(ConfigError):
        write_example_config(path)
    write_example_config(path, force=True)


def test_missing_config_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path / "nope.yaml")
    assert "sub-family init" in str(error.value)


def test_empty_and_non_mapping_configs(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(empty)
    listing = tmp_path / "list.yaml"
    listing.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(listing)


def test_invalid_yaml_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "family.yaml"
    path.write_text("version: 1\nmembers: [\n", encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "line" in str(error.value)


def test_member_lookup_is_case_insensitive() -> None:
    config = default_family()
    name, member = config.member("BIG_BROTHER")
    assert name == "big_brother"
    assert member.model == "deepseek-chat"
    with pytest.raises(MemberNotFound) as error:
        config.member("nobody")
    assert "big_brother" in str(error.value)


def test_capabilities_are_normalised() -> None:
    member = MemberConfig(
        base_url="http://localhost:1",
        model="m",
        capabilities=["Vision", "CODE", "vision", " fast "],
    )
    assert member.capabilities == ["vision", "code", "fast"]
    assert member.has_vision
    assert member.has_capability("FAST")


def test_resolve_api_key_prefers_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "from-env")
    member = MemberConfig(
        base_url="http://localhost:1",
        model="m",
        api_key="literal",
        api_key_env="TEST_KEY",
    )
    assert resolve_api_key(member) == "literal"


def test_resolve_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "from-env")
    member = MemberConfig(base_url="http://localhost:1", model="m", api_key_env="TEST_KEY")
    assert resolve_api_key(member) == "from-env"


def test_resolve_api_key_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_KEY", raising=False)
    member = MemberConfig(base_url="http://localhost:1", model="m", api_key_env="TEST_KEY")
    with pytest.raises(ConfigError) as error:
        resolve_api_key(member)
    assert "TEST_KEY" in str(error.value)


def test_resolve_db_path_relative_to_config(tmp_path: Path) -> None:
    config = FamilyConfig.model_validate(
        {"version": 1, "sessions": {"db_path": "state/sessions.db"}}
    )
    assert (
        resolve_db_path(config, tmp_path / "family.yaml")
        == (tmp_path / "state" / "sessions.db").resolve()
    )


def test_render_config_yaml_roundtrips() -> None:
    config = default_family()
    text = render_config_yaml(config)
    raw = yaml.safe_load(text)
    assert FamilyConfig.model_validate(raw) == config


def test_cost_inference_and_explicit_values() -> None:
    local = MemberConfig(base_url="http://localhost:1", model="m")
    assert local.effective_cost is CostTier.ELECTRICITY
    assert local.cost_is_inferred
    remote = MemberConfig(
        base_url="https://example.com", model="m", trust=TrustLevel.UNTRUSTED_CLOUD
    )
    assert remote.effective_cost is CostTier.PAID
    free = MemberConfig(base_url="https://example.com", model="m", cost=CostTier.FREE)
    assert free.effective_cost is CostTier.FREE
    assert not free.cost_is_inferred
    with pytest.raises(ValueError):
        MemberConfig.model_validate(
            {"base_url": "http://localhost:1", "model": "m", "cost": "expensive"}
        )


def test_trust_level_properties() -> None:
    assert not TrustLevel.SAFE_LOCAL.is_remote
    assert TrustLevel.PRIVATE_NETWORK.is_remote
    assert TrustLevel.UNTRUSTED_CLOUD.label == "cloud"


def test_masked_dump_hides_sensitive_headers_and_extra_body() -> None:
    config = FamilyConfig.model_validate(
        {
            "version": 1,
            "members": {
                "a": {
                    "base_url": "https://example.com",
                    "model": "m",
                    "headers": {"X-API-Key": "header-secret", "X-Trace": "visible"},
                    "extra_body": {"client_secret": "body-secret", "top_p": 0.5},
                }
            },
        }
    )
    masked = config.masked_dump()["members"]["a"]
    assert masked["headers"]["X-API-Key"] == "***"
    assert masked["headers"]["X-Trace"] == "visible"
    assert masked["extra_body"]["client_secret"] == "***"
    assert masked["extra_body"]["top_p"] == 0.5


def test_shipped_example_matches_template() -> None:
    from sub_family.config.template import EXAMPLE_YAML

    example = Path(__file__).resolve().parents[1] / "examples" / "family.example.yaml"
    assert example.exists()
    assert example.read_text(encoding="utf-8") == EXAMPLE_YAML
