"""Load, validate, and atomically persist ``family.yaml``."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from sub_family import paths
from sub_family.config.models import FamilyConfig, MemberConfig, TrustLevel
from sub_family.config.template import EXAMPLE_YAML
from sub_family.errors import ConfigError

CONFIG_HEADER = """\
# Sub-Family family registry
# Documentation: https://github.com/MistaKilla/Sub-Family-MCP
# Tip: `sub-family ui` edits this file interactively; hand-editing is supported.
"""


def load_config(path: Path | None = None) -> FamilyConfig:
    """Load and validate a family configuration.

    Parameters
    ----------
    path:
        Explicit YAML path. Defaults to ``SUB_FAMILY_CONFIG`` or the platform
        config location (``~/.config/sub-family/family.yaml`` on POSIX).

    Raises
    ------
    ConfigError
        If the file is missing, unreadable, not valid YAML, or fails validation.
    """
    config_file = (path or paths.config_path()).expanduser()
    if not config_file.exists():
        raise ConfigError(
            f"configuration not found at {config_file}.\n"
            "Create a starter config with: sub-family init"
        )
    try:
        text = config_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {config_file}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"invalid YAML in {config_file}{location}: {problem}") from exc

    if raw is None:
        raise ConfigError(f"configuration file {config_file} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"configuration file {config_file} must contain a YAML mapping at the top level"
        )

    try:
        return FamilyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(config_file, exc)) from exc


def _format_validation_error(config_file: Path, exc: ValidationError) -> str:
    lines = [f"invalid configuration in {config_file}:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def default_family() -> FamilyConfig:
    """Return the parsed starter family (big_brother + lilbro)."""
    raw = yaml.safe_load(EXAMPLE_YAML)
    return FamilyConfig.model_validate(raw)


def render_config_yaml(config: FamilyConfig, *, header: bool = True) -> str:
    """Serialise a config as human-friendly YAML, preserving field order."""
    data = config.model_dump(mode="json", exclude_none=True)
    body = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return f"{CONFIG_HEADER}\n{body}" if header else body


def save_config(
    config: FamilyConfig,
    path: Path | None = None,
    *,
    backup: bool = True,
) -> Path:
    """Atomically write a config, keeping a ``.bak`` copy of the previous file.

    The file is created with owner-only permissions because users may keep a
    literal API key in it even though environment variables are recommended.
    """
    config_file = (path or paths.config_path()).expanduser()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    text = render_config_yaml(config)

    if backup and config_file.exists():
        backup_path = config_file.with_suffix(config_file.suffix + ".bak")
        try:
            shutil.copy2(config_file, backup_path)
            _chmod_600(backup_path)
        except OSError:
            # A failed backup must not destroy the chance to save new settings.
            pass

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{config_file.name}.", suffix=".tmp", dir=config_file.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_600(temp_path)
        os.replace(temp_path, config_file)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigError(f"cannot write {config_file}: {exc}") from exc
    _chmod_600(config_file)
    return config_file


def write_example_config(path: Path | None = None, *, force: bool = False) -> Path:
    """Write the commented starter config, refusing to clobber by default."""
    config_file = (path or paths.config_path()).expanduser()
    if config_file.exists() and not force:
        raise ConfigError(
            f"{config_file} already exists; pass --force to overwrite it (a .bak backup is kept)"
        )
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if force and config_file.exists():
        backup = config_file.with_suffix(config_file.suffix + ".bak")
        with contextlib.suppress(OSError):
            shutil.copy2(config_file, backup)
    if os.name == "posix":
        descriptor = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(EXAMPLE_YAML)
    else:  # pragma: no cover - Windows has no POSIX mode bits
        config_file.write_text(EXAMPLE_YAML, encoding="utf-8")
    _chmod_600(config_file)
    return config_file


def load_environment(env_file: Path | None = None) -> None:
    """Load ``.env`` next to the config without overriding real env vars."""
    candidate = env_file or paths.env_file_path()
    if candidate.exists():
        load_dotenv(candidate, override=False)


def resolve_api_key(
    member: MemberConfig,
    *,
    environ: dict[str, str] | None = None,
    name: str | None = None,
) -> str | None:
    """Resolve the API key for a member, with an actionable error when missing.

    A literal ``api_key`` wins over ``api_key_env``. Local endpoints that need no
    authentication simply return ``None``.
    """
    if member.api_key is not None:
        return member.api_key.get_secret_value()
    if member.api_key_env:
        env = os.environ if environ is None else environ
        value = env.get(member.api_key_env, "").strip()
        if not value:
            raise ConfigError(
                f"member {(name or member.model)!r} expects the API key in ${member.api_key_env}, "
                f"but it is not set. Export it or add {member.api_key_env}=... to "
                f"{paths.env_file_path()}."
            )
        return value
    return None


def resolve_db_path(config: FamilyConfig, config_file: Path | None = None) -> Path:
    """Resolve the session database path relative to the config file."""
    configured = config.sessions.db_path
    if not configured:
        return paths.sessions_db_path()
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        return candidate
    base = (config_file or paths.config_path()).expanduser().parent
    return (base / candidate).resolve()


def redact_config_for_display(config: FamilyConfig) -> dict[str, Any]:
    """Return a config dump safe to print in a terminal or issue report."""
    return config.masked_dump()


def trust_choices() -> list[str]:
    """Values accepted by ``--trust`` and the TUI selector."""
    return [level.value for level in TrustLevel]


def _chmod_600(path: Path) -> None:
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        path.chmod(0o600)
