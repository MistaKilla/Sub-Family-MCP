"""Pydantic models for the ``family.yaml`` registry.

The schema is deliberately strict (``extra="forbid"``) so that a typo such as
``max_clone`` fails loudly instead of silently disabling concurrency limits.
All models are immutable after construction: edits go through ``model_copy`` and
are re-validated, which keeps the TUI and CLI honest.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from sub_family.errors import MemberNotFound

MEMBER_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
CAPABILITY_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_HEADER_RE: Final = re.compile(
    r"(?i)(authorization|api[-_]?key|token|secret|password|cookie|credential)"
)

KNOWN_CAPABILITIES: Final = (
    "chat",
    "vision",
    "code",
    "reasoning",
    "fast",
    "long_context",
    "tools",
)


class TrustLevel(StrEnum):
    """How much a member can be trusted with sensitive context.

    ``safe_local``
        Runs on hardware you control, inside your LAN. Nothing leaves the house.
    ``private_network``
        A remote endpoint you operate yourself (a VPS, a friend's farm). Still
        treated as remote for privacy purposes, but not as a third party.
    ``untrusted_cloud``
        A third-party API. Secrets are redacted before the prompt is sent and
        the response is marked as untrusted in tool output.
    """

    SAFE_LOCAL = "safe_local"
    PRIVATE_NETWORK = "private_network"
    UNTRUSTED_CLOUD = "untrusted_cloud"

    @property
    def is_remote(self) -> bool:
        """Whether prompts leave the local machine."""
        return self is not TrustLevel.SAFE_LOCAL

    @property
    def label(self) -> str:
        """Short human-readable label used in tables and tool output."""
        return {
            TrustLevel.SAFE_LOCAL: "local",
            TrustLevel.PRIVATE_NETWORK: "private",
            TrustLevel.UNTRUSTED_CLOUD: "cloud",
        }[self]


class CostTier(StrEnum):
    """How a member is paid for.

    ``free``
        No marginal cost: a free API tier or a subscription already paid for.
    ``paid``
        Metered per request/token (cloud API, paid gateway).
    ``electricity``
        Self-hosted hardware; the only marginal cost is power.

    Cost is independent of trust: a paid cloud model can still be the only
    place where private code may go, and a free local model still consumes
    electricity and GPU time.
    """

    FREE = "free"
    PAID = "paid"
    ELECTRICITY = "electricity"

    @property
    def label(self) -> str:
        """Short human-readable label."""
        return {
            CostTier.FREE: "free",
            CostTier.PAID: "paid",
            CostTier.ELECTRICITY: "electricity",
        }[self]


class MemberConfig(BaseModel):
    """One model endpoint in the family."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    base_url: str
    model: str
    chat_path: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer "
    headers: dict[str, str] = Field(default_factory=dict)
    system_prompt: str | None = None
    trust: TrustLevel = TrustLevel.SAFE_LOCAL
    cost: CostTier | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    max_clones: int = Field(default=1, ge=1, le=64)
    timeout_s: float = Field(default=120.0, gt=0, le=3600)
    retries: int = Field(default=2, ge=0, le=5)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    context_window: int | None = Field(default=None, ge=256, le=10_000_000)
    verify_tls: bool = True
    enabled: bool = True
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an absolute http:// or https:// URL")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials; use api_key_env instead")
        if any(character.isspace() for character in value):
            raise ValueError("base_url must not contain whitespace")
        return value

    @field_validator("chat_path")
    @classmethod
    def _validate_chat_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value.startswith("/") or "://" in value:
            raise ValueError("chat_path must be a path such as /v1/chat/completions")
        return value

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model must not be empty")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _validate_env_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not ENV_NAME_RE.match(value):
            raise ValueError(
                "api_key_env must be an environment variable name, e.g. DEEPSEEK_API_KEY"
            )
        return value

    @field_validator("capabilities")
    @classmethod
    def _normalise_capabilities(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for raw in value:
            cap = raw.strip().lower()
            if not CAPABILITY_RE.match(cap):
                raise ValueError(
                    f"invalid capability {raw!r}; use lowercase letters, digits, '_' or '-'"
                )
            seen.setdefault(cap, None)
        return list(seen)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for key, header_value in value.items():
            if not key or key.strip() != key or any(ch in key for ch in "\r\n:"):
                raise ValueError(f"invalid header name {key!r}")
            if any(ch in header_value for ch in "\r\n"):
                raise ValueError(f"header {key!r} contains a newline")
        return value

    @field_serializer("api_key")
    def _serialize_api_key(self, value: SecretStr | None) -> str | None:
        # YAML must round-trip the real key; display paths use ``masked_dump``.
        return value.get_secret_value() if value is not None else None

    def has_capability(self, capability: str) -> bool:
        """Case-insensitive capability check."""
        return capability.lower() in self.capabilities

    @property
    def has_vision(self) -> bool:
        """Whether the member accepts image input."""
        return self.has_capability("vision")

    @property
    def effective_cost(self) -> CostTier:
        """Return the explicit cost tier, or infer a conservative default.

        Local endpoints are assumed to cost electricity; remote endpoints are
        assumed to be paid. Set ``cost`` explicitly to override either default,
        for example for a free cloud tier.
        """
        if self.cost is not None:
            return self.cost
        return CostTier.ELECTRICITY if self.trust is TrustLevel.SAFE_LOCAL else CostTier.PAID

    @property
    def cost_is_inferred(self) -> bool:
        """Whether the effective cost came from the trust-based default."""
        return self.cost is None

    @property
    def auth_configured(self) -> bool:
        """Whether a literal key or an env-var reference is present."""
        return self.api_key is not None or self.api_key_env is not None

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """Build request headers, including custom ones and auth."""
        headers = {"Content-Type": "application/json", **self.headers}
        if api_key:
            headers[self.api_key_header] = f"{self.api_key_prefix}{api_key}"
        return headers


class SessionsConfig(BaseModel):
    """Persistence and sliding-window limits for virtual sessions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    db_path: str | None = None
    max_messages: int = Field(default=40, ge=2, le=1000)
    max_chars: int = Field(default=80_000, ge=1_000, le=2_000_000)
    ttl_days: int = Field(default=30, ge=0, le=3650)
    cleanup_on_start: bool = True


class SecurityConfig(BaseModel):
    """Trust-aware guardrails applied before a prompt leaves the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    redact_trust: set[TrustLevel] = Field(default_factory=lambda: {TrustLevel.UNTRUSTED_CLOUD})
    redact_secrets: bool = True
    blocked_patterns: list[str] = Field(default_factory=list)
    max_prompt_chars: int = Field(default=400_000, ge=1_000, le=5_000_000)
    max_image_mb: float = Field(default=20.0, gt=0, le=200)
    deny_images_to_remote: bool = False
    expose_reasoning: bool = False
    max_reasoning_chars: int = Field(default=4_000, ge=0, le=100_000)


class SwarmConfig(BaseModel):
    """Parallel fan-out limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tasks: int = Field(default=12, ge=1, le=64)
    max_result_chars: int = Field(default=12_000, ge=500, le=200_000)
    default_synthesize: bool = False


class FamilyConfig(BaseModel):
    """The whole ``family.yaml`` document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    members: dict[str, MemberConfig] = Field(default_factory=dict)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(
                f"unsupported config version {value}; this build understands version 1"
            )
        return value

    @model_validator(mode="after")
    def _check_members(self) -> FamilyConfig:
        for name in self.members:
            if not MEMBER_NAME_RE.match(name):
                raise ValueError(
                    f"invalid member name {name!r}: use lowercase letters, digits, '_' or '-', "
                    "starting with a letter (max 40 chars)"
                )
        if self.security.max_image_mb * 1024 * 1024 < 1024:
            raise ValueError("security.max_image_mb is too small")
        return self

    def member(self, name: str) -> tuple[str, MemberConfig]:
        """Resolve a member by case-insensitive name.

        Returns the canonical key and its configuration, or raises
        :class:`MemberNotFound` with the list of available names.
        """
        if name in self.members:
            return name, self.members[name]
        lowered = name.strip().lower()
        for key, member in self.members.items():
            if key.lower() == lowered:
                return key, member
        available = ", ".join(sorted(self.members)) or "(none configured)"
        raise MemberNotFound(f"unknown family member {name!r}. Available members: {available}")

    def enabled_members(self) -> dict[str, MemberConfig]:
        """Return only enabled members, preserving order."""
        return {name: member for name, member in self.members.items() if member.enabled}

    def masked_dump(self) -> dict[str, Any]:
        """Return a JSON-ready dump safe to print.

        Literal API keys, sensitive custom header values, and credential-like
        ``extra_body`` entries are replaced with ``***``. Environment variable
        names are kept because they are not secrets and help users diagnose
        configuration.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        for member in data.get("members", {}).values():
            if member.get("api_key"):
                member["api_key"] = "***"
            headers = member.get("headers")
            if isinstance(headers, dict):
                for header_name in list(headers):
                    if SENSITIVE_HEADER_RE.search(str(header_name)):
                        headers[header_name] = "***"
            extra = member.get("extra_body")
            if isinstance(extra, dict):
                for extra_key in list(extra):
                    if SENSITIVE_HEADER_RE.search(str(extra_key)):
                        extra[extra_key] = "***"
        return data
