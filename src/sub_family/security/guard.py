"""Secret redaction and policy checks applied before a prompt leaves the box.

The guard is intentionally conservative: it targets well-known credential
shapes and assignment patterns instead of trying to understand code. It is a
seatbelt, not a sandbox -- documentation says so explicitly. Hard blocks are
available through ``security.blocked_patterns`` for users who need them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from sub_family.config.models import MemberConfig, SecurityConfig, TrustLevel
from sub_family.errors import ConfigError, PolicyError

_PLACEHOLDER = "\u00abREDACTED:{kind}\u00bb"

#: (kind, pattern, replacement) triples. Order matters: private keys first so a
#: PEM block is removed as a whole before line-based patterns run.
_RULES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        _PLACEHOLDER.format(kind="private_key"),
    ),
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        _PLACEHOLDER.format(kind="api_key"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        _PLACEHOLDER.format(kind="github_token"),
    ),
    (
        "aws_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        _PLACEHOLDER.format(kind="aws_key"),
    ),
    (
        "google_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        _PLACEHOLDER.format(kind="google_key"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        _PLACEHOLDER.format(kind="slack_token"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        _PLACEHOLDER.format(kind="jwt"),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{20,}"),
        r"\1 " + _PLACEHOLDER.format(kind="token"),
    ),
    (
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]{1,64}):([^@\s/]{1,128})@"),
        r"\1:" + _PLACEHOLDER.format(kind="password") + "@",
    ),
    (
        "dotenv_assignment",
        re.compile(
            r"(?im)^[ \t]*([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY)"
            r"[A-Z0-9_]*)[ \t]*=[ \t]*([\"']?)([^\s\"'#]{8,})\2[ \t]*$"
        ),
        r"\1=" + _PLACEHOLDER.format(kind="secret"),
    ),
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|password)\b[ \t]*[:=][ \t]*([\"']?)"
            r"([A-Za-z0-9_\-./+=]{12,})\2"
        ),
        r"\1=" + _PLACEHOLDER.format(kind="secret"),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One category of redacted material."""

    kind: str
    count: int


@dataclass(slots=True)
class ScrubResult:
    """Result of applying the guard to a string."""

    text: str
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def redactions(self) -> int:
        """Total number of redacted spans."""
        return sum(finding.count for finding in self.findings)

    @property
    def kinds(self) -> list[str]:
        """Names of the redacted categories."""
        return [finding.kind for finding in self.findings]


class Guard:
    """Applies redaction rules according to a member's trust level."""

    def __init__(self, config: SecurityConfig) -> None:
        self.config = config
        compiled: list[re.Pattern[str]] = []
        for index, pattern in enumerate(config.blocked_patterns, start=1):
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                raise ConfigError(
                    f"security.blocked_patterns[{index}] is not a valid regular expression: {exc}"
                ) from exc
        self._blocked = compiled

    def check_blocked(self, text: str, *, context: str = "prompt") -> None:
        """Raise if the text matches a user-defined hard block."""
        for pattern in self._blocked:
            if pattern.search(text):
                raise PolicyError(
                    f"the {context} matches a blocked pattern from security.blocked_patterns "
                    f"({pattern.pattern!r}); refusing to send it"
                )

    def scrub(self, text: str, member: MemberConfig, *, context: str = "prompt") -> ScrubResult:
        """Redact secrets when the member's trust level requires it.

        Hard blocks are enforced for every member. Soft redaction applies only to
        trust levels listed in ``security.redact_trust``.
        """
        self.check_blocked(text, context=context)
        warnings: list[str] = []
        should_redact = self.config.redact_secrets and member.trust in self.config.redact_trust
        if not should_redact:
            return ScrubResult(text=text, warnings=warnings)

        findings: dict[str, int] = {}
        scrubbed = text
        for kind, pattern, replacement in _RULES:
            scrubbed, count = pattern.subn(replacement, scrubbed)
            if count:
                findings[kind] = findings.get(kind, 0) + count
        if findings:
            warnings.append(
                f"redacted {sum(findings.values())} secret-like span(s) before sending to "
                f"{member.trust.value}: {', '.join(sorted(findings))}"
            )
        return ScrubResult(
            text=scrubbed,
            findings=[Finding(kind=kind, count=count) for kind, count in findings.items()],
            warnings=warnings,
        )

    def check_images(self, member: MemberConfig, *, count: int) -> list[str]:
        """Validate image delivery policy and return user-facing warnings."""
        if count <= 0:
            return []
        if member.trust.is_remote and self.config.deny_images_to_remote:
            raise PolicyError(
                f"{member.trust.value} member cannot receive images because "
                "security.deny_images_to_remote is true"
            )
        if member.trust is TrustLevel.UNTRUSTED_CLOUD:
            return [
                "image(s) will be sent to an untrusted cloud endpoint; "
                "screenshots can contain secrets that text redaction cannot remove"
            ]
        if member.trust.is_remote:
            return ["image(s) will be sent to a remote private-network endpoint"]
        return []
