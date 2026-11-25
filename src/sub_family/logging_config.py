"""Logging setup that keeps stdout clean for the MCP stdio transport.

MCP servers communicate over stdout using JSON-RPC. Any stray ``print`` or log
handler writing to stdout corrupts the protocol, so every log record is sent to
stderr. Prompts and API keys are never logged by the library.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str | int = "WARNING") -> None:
    """Configure the root logger to write to stderr exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        if isinstance(level, str):
            logging.getLogger().setLevel(level.upper())
        else:
            logging.getLogger().setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(f"sub_family.{name}")
