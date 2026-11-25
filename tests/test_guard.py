"""Secret redaction and trust policy."""

from __future__ import annotations

import pytest

from sub_family.config.models import MemberConfig, SecurityConfig, TrustLevel
from sub_family.errors import ConfigError, PolicyError
from sub_family.security.guard import Guard


def member(trust: TrustLevel = TrustLevel.UNTRUSTED_CLOUD) -> MemberConfig:
    return MemberConfig(base_url="https://example.com", model="m", trust=trust)


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        ("sk-abcdefghijklmnopqrstuvwx", "openai_key"),
        ("ghp_abcdefghijklmnopqrstuvwxyz012345", "github_token"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_key"),
        ("AIza" + "a" * 35, "google_key"),
        ("Bearer " + "a" * 30, "bearer_token"),
        ("postgres://user:supersecret@localhost/db", "url_credentials"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "private_key"),
    ],
)
def test_known_secret_shapes_are_redacted(secret: str, kind: str) -> None:
    guard = Guard(SecurityConfig())
    result = guard.scrub(f"please use {secret} now", member())
    assert secret not in result.text
    assert kind in result.kinds
    assert result.redactions >= 1
    assert result.warnings


def test_dotenv_block_is_redacted() -> None:
    guard = Guard(SecurityConfig())
    result = guard.scrub("DEEPSEEK_API_KEY=sk-live-1234567890abcdef\nDEBUG=true", member())
    assert "sk-live-1234567890abcdef" not in result.text
    assert "DEBUG=true" in result.text


def test_local_member_keeps_text_untouched() -> None:
    guard = Guard(SecurityConfig())
    secret = "sk-abcdefghijklmnopqrstuvwx"
    result = guard.scrub(f"key={secret}", member(TrustLevel.SAFE_LOCAL))
    assert secret in result.text
    assert result.redactions == 0


def test_redaction_can_be_disabled() -> None:
    guard = Guard(SecurityConfig(redact_secrets=False))
    secret = "sk-abcdefghijklmnopqrstuvwx"
    result = guard.scrub(secret, member())
    assert result.text == secret


def test_blocked_patterns_are_hard_blocks() -> None:
    guard = Guard(SecurityConfig(blocked_patterns=[r"PROJECT-INTERNAL-\d+"]))
    with pytest.raises(PolicyError):
        guard.scrub("discuss PROJECT-INTERNAL-42", member(TrustLevel.SAFE_LOCAL))


def test_blocked_patterns_apply_to_every_trust_level() -> None:
    guard = Guard(SecurityConfig(blocked_patterns=["forbidden"]))
    with pytest.raises(PolicyError):
        guard.scrub("forbidden text", member(TrustLevel.SAFE_LOCAL))


def test_invalid_blocked_pattern_is_config_error() -> None:
    with pytest.raises(ConfigError):
        Guard(SecurityConfig(blocked_patterns=["["]))


def test_images_to_cloud_are_warned() -> None:
    guard = Guard(SecurityConfig())
    warnings = guard.check_images(member(TrustLevel.UNTRUSTED_CLOUD), count=1)
    assert warnings
    assert "untrusted cloud" in warnings[0]


def test_images_can_be_denied_to_remote() -> None:
    guard = Guard(SecurityConfig(deny_images_to_remote=True))
    with pytest.raises(PolicyError):
        guard.check_images(member(TrustLevel.UNTRUSTED_CLOUD), count=1)
    assert guard.check_images(member(TrustLevel.SAFE_LOCAL), count=1) == []


def test_clean_text_has_no_warnings() -> None:
    guard = Guard(SecurityConfig())
    result = guard.scrub("refactor the billing module", member())
    assert result.text == "refactor the billing module"
    assert result.redactions == 0
    assert result.warnings == []
