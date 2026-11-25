"""Cheap, dependency-free token estimation.

Exact tokenisation is provider-specific; a four-characters-per-token heuristic
is good enough for context-window guardrails and UI hints. It intentionally
over-estimates code (which tokenises worse than prose) but never under-estimates
by much, which is the safe direction for a limit check.
"""

from __future__ import annotations

import math
from typing import Any


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a plain string."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_content_tokens(content: Any) -> int:
    """Estimate tokens for a string or an OpenAI-style content-part list."""
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += estimate_text_tokens(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    total += 1_000  # rough per-image budget for a resized screenshot
        return total or 1
    return estimate_text_tokens(str(content))


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens for a Chat Completions-style message list."""
    total = 0
    for message in messages:
        total += estimate_content_tokens(message.get("content", ""))
        total += 4  # role and framing overhead
    return total
