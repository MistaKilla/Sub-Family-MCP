"""Exception hierarchy for Sub-Family.

All expected failures raised by the library derive from :class:`SubFamilyError`.
The MCP layer converts them into tool errors with actionable messages, while the
CLI prints them without a traceback.
"""

from __future__ import annotations

from enum import StrEnum


class SubFamilyError(Exception):
    """Base class for every expected Sub-Family failure."""


class ConfigError(SubFamilyError):
    """The configuration file is missing, malformed, or semantically invalid."""


class MemberNotFound(ConfigError):
    """A requested family member does not exist."""


class MemberDisabled(SubFamilyError):
    """A requested family member is disabled in the configuration."""


class PolicyError(SubFamilyError):
    """A request was blocked by the security policy (secrets, images, size)."""


class ImageError(PolicyError):
    """An image could not be loaded, decoded, or is too large."""


class SessionError(SubFamilyError):
    """A session id or session operation is invalid."""


class ErrorKind(StrEnum):
    """Stable categories for provider failures, used by callers and tests."""

    AUTH = "auth"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    BAD_RESPONSE = "bad_response"
    CONFIG = "config"


class ProviderError(SubFamilyError):
    """A model endpoint failed in an expected, classified way.

    Parameters
    ----------
    message:
        Human-readable, secret-free explanation suitable for an LLM agent.
    kind:
        Stable category for programmatic handling.
    status:
        HTTP status code when the failure came from a response.
    retryable:
        Whether an identical request has a reasonable chance of succeeding.
    detail:
        Optional short response-body excerpt (never credentials).
    """

    message: str
    kind: ErrorKind
    status: int | None
    retryable: bool
    detail: str | None
    retry_after: float | None

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind = ErrorKind.SERVER,
        status: int | None = None,
        retryable: bool = False,
        detail: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.detail = detail
        self.retry_after = retry_after

    def __str__(self) -> str:
        # Messages already mention the HTTP status where it is relevant.
        return self.message


class ToolInputError(SubFamilyError):
    """Invalid arguments passed to an MCP tool or CLI command."""
