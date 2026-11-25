"""Cross-platform locations for Sub-Family configuration and state.

The layout follows the XDG base-directory convention on POSIX systems so that
users can find and edit files easily, and the standard roaming/local app-data
folders on Windows. Every location can be overridden with an environment
variable, which keeps the code testable and lets advanced users keep a family
config inside a project repository.

Environment variables
---------------------
``SUB_FAMILY_CONFIG``
    Full path to ``family.yaml``.
``SUB_FAMILY_CONFIG_DIR``
    Directory that holds ``family.yaml`` and ``.env``.
``SUB_FAMILY_DATA_DIR``
    Directory for ``sessions.db`` and other mutable state.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

CONFIG_FILE_NAME = "family.yaml"
ENV_FILE_NAME = ".env"
DB_FILE_NAME = "sessions.db"


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    """Return the directory containing the family configuration."""
    override = os.environ.get("SUB_FAMILY_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return Path(base) / "sub-family"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else _home() / ".config") / "sub-family"


def config_path() -> Path:
    """Return the path of ``family.yaml``."""
    override = os.environ.get("SUB_FAMILY_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / CONFIG_FILE_NAME


def env_file_path() -> Path:
    """Return the path of the optional ``.env`` file next to the config."""
    return config_path().parent / ENV_FILE_NAME


def data_dir() -> Path:
    """Return the directory used for mutable state such as the session DB."""
    override = os.environ.get("SUB_FAMILY_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / "sub-family"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base else _home() / ".local" / "share") / "sub-family"


def sessions_db_path() -> Path:
    """Return the default SQLite database path."""
    return data_dir() / DB_FILE_NAME


def ensure_config_dir() -> Path:
    """Create the configuration directory with owner-only permissions."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(directory, 0o700)
    return directory


def ensure_data_dir() -> Path:
    """Create the data directory with owner-only permissions."""
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(directory, 0o700)
    return directory


def display_path(path: Path) -> str:
    """Render a path with ``~`` for the home directory, for human output."""
    expanded = path.expanduser()
    try:
        relative = expanded.relative_to(_home())
    except ValueError:
        return str(expanded)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX permissions; silently ignore unsupported platforms."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        path.chmod(mode)
