"""The orchestration layer behind every MCP tool and CLI command.

``FamilyService`` owns the validated config, provider clients, per-member
concurrency limits, the session store, and the secret guard. The MCP server and
the console front-end are thin shells over this class, which keeps behaviour
identical no matter how a request is triggered and makes the service directly
testable without a transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from sub_family import paths
from sub_family.config.loader import resolve_api_key, resolve_db_path
from sub_family.config.models import FamilyConfig, MemberConfig
from sub_family.errors import (
    ConfigError,
    MemberDisabled,
    PolicyError,
    SessionError,
    SubFamilyError,
    ToolInputError,
)
from sub_family.logging_config import get_logger
from sub_family.providers.base import ChatProvider, ProviderHealth
from sub_family.providers.media import load_images
from sub_family.providers.openai_compat import OpenAICompatProvider
from sub_family.security.guard import Guard
from sub_family.sessions.store import SessionStore, StoredMessage, validate_session_id
from sub_family.tokens import estimate_message_tokens

_log = get_logger("service")

ClientFactory = Callable[[MemberConfig], httpx.AsyncClient]
ProviderFactory = Callable[[str, MemberConfig, str | None, httpx.AsyncClient], ChatProvider]


@dataclass(slots=True)
class AskResult:
    """Successful single-member response."""

    member: str
    model: str
    trust: str
    content: str
    session_id: str | None
    usage: dict[str, int | None]
    elapsed_ms: int
    finish_reason: str | None = None
    reasoning: str | None = None
    warnings: list[str] = field(default_factory=list)
    redactions: list[dict[str, Any]] = field(default_factory=list)
    history_messages: int = 0
    history_trimmed: int = 0
    images: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise for MCP tool output."""
        return {
            "ok": True,
            "member": self.member,
            "model": self.model,
            "trust": self.trust,
            "content": self.content,
            "session_id": self.session_id,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "finish_reason": self.finish_reason,
            "reasoning": self.reasoning,
            "warnings": self.warnings,
            "redactions": self.redactions,
            "history_messages": self.history_messages,
            "history_trimmed": self.history_trimmed,
            "images": self.images,
        }


@dataclass(slots=True)
class SwarmTaskResult:
    """One branch of a parallel fan-out."""

    index: int
    ok: bool
    content: str
    task_preview: str
    session_id: str | None = None
    error: str | None = None
    usage: dict[str, int | None] = field(default_factory=dict)
    elapsed_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialise for MCP tool output."""
        return {
            "index": self.index,
            "ok": self.ok,
            "task_preview": self.task_preview,
            "content": self.content,
            "error": self.error,
            "session_id": self.session_id,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class SwarmResult:
    """Aggregate result of ``ask_family_swarm``."""

    member: str
    trust: str
    results: list[SwarmTaskResult]
    elapsed_ms: int
    synthesis: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        """Number of successful branches."""
        return sum(1 for result in self.results if result.ok)

    @property
    def failed_count(self) -> int:
        """Number of failed branches."""
        return len(self.results) - self.ok_count

    def to_dict(self) -> dict[str, Any]:
        """Serialise for MCP tool output."""
        return {
            "ok": self.ok_count > 0,
            "member": self.member,
            "trust": self.trust,
            "summary": {
                "total": len(self.results),
                "succeeded": self.ok_count,
                "failed": self.failed_count,
            },
            "results": [result.to_dict() for result in self.results],
            "synthesis": self.synthesis,
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class MemberStatus:
    """Reachability snapshot for one member."""

    name: str
    enabled: bool
    ok: bool
    model: str
    trust: str
    capabilities: list[str]
    max_clones: int
    latency_ms: int = 0
    model_available: bool | None = None
    models: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for MCP tool output."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "ok": self.ok,
            "model": self.model,
            "trust": self.trust,
            "capabilities": self.capabilities,
            "max_clones": self.max_clones,
            "latency_ms": self.latency_ms,
            "model_available": self.model_available,
            "models": self.models,
            "error": self.error,
        }


class FamilyService:
    """Coordinates family members, sessions, and security policy."""

    def __init__(
        self,
        config: FamilyConfig,
        *,
        config_path: Path | None = None,
        store: SessionStore | None = None,
        guard: Guard | None = None,
        client_factory: ClientFactory | None = None,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.config = config
        self.config_path = (config_path or paths.config_path()).expanduser()
        self.guard = guard or Guard(config.security)
        self._store = store
        self._client_factory = client_factory or _default_client
        self._provider_factory = provider_factory or _default_provider
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._providers: dict[str, ChatProvider] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._key_errors: dict[str, str] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._started = False
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Open storage and build one HTTP client per member (idempotent)."""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            if self.config.sessions.enabled:
                if self._store is None:
                    self._store = SessionStore(
                        resolve_db_path(self.config, self.config_path),
                        self.config.sessions,
                    )
                await self._store.open()
                if self.config.sessions.cleanup_on_start and self.config.sessions.ttl_days:
                    removed = await self._store.prune(self.config.sessions.ttl_days)
                    if removed:
                        _log.info("pruned %d expired session(s)", removed)
            try:
                for name, member in self.config.members.items():
                    client = self._client_factory(member)
                    self._clients[name] = client
                    try:
                        api_key = resolve_api_key(member, name=name)
                    except ConfigError as exc:
                        self._key_errors[name] = str(exc)
                        api_key = None
                    self._providers[name] = self._provider_factory(name, member, api_key, client)
                    self._semaphores[name] = asyncio.Semaphore(member.max_clones)
            except Exception:
                await self._close_clients()
                raise
            self._started = True

    async def aclose(self) -> None:
        """Close HTTP clients and the session database."""
        async with self._start_lock:
            await self._close_clients()
            if self._store is not None:
                await self._store.close()
                self._store = None
            self._session_locks.clear()
            self._started = False

    async def _close_clients(self) -> None:
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()
        self._providers.clear()
        self._semaphores.clear()

    async def __aenter__(self) -> FamilyService:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- registry
    def list_members(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Return registry metadata without contacting any endpoint."""
        members: list[dict[str, Any]] = []
        for name, member in self.config.members.items():
            if not include_disabled and not member.enabled:
                continue
            members.append(
                {
                    "name": name,
                    "description": member.description,
                    "model": member.model,
                    "trust": member.trust.value,
                    "cost": member.effective_cost.value,
                    "cost_source": "inferred" if member.cost_is_inferred else "explicit",
                    "capabilities": member.capabilities,
                    "vision": member.has_vision,
                    "max_clones": member.max_clones,
                    "timeout_s": member.timeout_s,
                    "context_window": member.context_window,
                    "enabled": member.enabled,
                    "auth_configured": _auth_configured(member),
                    "api_key_env": member.api_key_env,
                    "base_url": member.base_url,
                }
            )
        return members

    def get_member_config(self, name: str) -> MemberConfig:
        """Resolve an enabled member's configuration."""
        _, member = self.config.member(name)
        if not member.enabled:
            raise MemberDisabled(f"member {name!r} is disabled in the config")
        return member

    # ------------------------------------------------------------------ ask
    async def ask(
        self,
        member_name: str,
        prompt: str,
        *,
        session_id: str | None = None,
        images: Sequence[str] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AskResult:
        """Send one prompt to one member, optionally using a persistent session."""
        await self.start()
        name, member = self.config.member(member_name)
        if not member.enabled:
            raise MemberDisabled(f"member {name!r} is disabled in the config")
        image_sources = list(images or [])
        prompt = prompt or ""
        if not prompt.strip() and not image_sources:
            raise ToolInputError("prompt must not be empty")
        if len(prompt) > self.config.security.max_prompt_chars:
            raise PolicyError(
                f"prompt is {len(prompt):,} characters, above security.max_prompt_chars "
                f"({self.config.security.max_prompt_chars:,})"
            )
        if image_sources and not member.has_vision:
            raise ToolInputError(
                f"member {name!r} is not configured for vision; add 'vision' to its "
                "capabilities or choose a vision member"
            )
        if image_sources and self.config.security.deny_images_to_remote and member.trust.is_remote:
            raise PolicyError(
                f"member {name!r} is remote and security.deny_images_to_remote is enabled"
            )
        if session_id is not None:
            if not self.config.sessions.enabled:
                raise ToolInputError("sessions are disabled in the config")
            session_id = validate_session_id(session_id)
        if temperature is not None and not 0.0 <= temperature <= 2.0:
            raise ToolInputError("temperature must be between 0 and 2")
        if max_tokens is not None and max_tokens < 1:
            raise ToolInputError("max_tokens must be a positive integer")

        warnings: list[str] = list(self.guard.check_images(member, count=len(image_sources)))
        prompt_scrub = self.guard.scrub(prompt, member)
        warnings.extend(prompt_scrub.warnings)
        findings: dict[str, int] = {
            finding.kind: finding.count for finding in prompt_scrub.findings
        }
        clean_prompt = prompt_scrub.text

        system_text = _join_system(member.system_prompt, system)
        if system_text:
            if len(system_text) > self.config.security.max_prompt_chars:
                raise PolicyError(
                    "system prompt is above security.max_prompt_chars; shorten it in the config"
                )
            system_scrub = self.guard.scrub(system_text, member, context="system prompt")
            warnings.extend(system_scrub.warnings)
            for finding in system_scrub.findings:
                findings[finding.kind] = findings.get(finding.kind, 0) + finding.count
            system_text = system_scrub.text

        provider = self._provider_for(name)
        client = self._clients[name]
        blobs = await load_images(
            image_sources,
            http=client,
            max_bytes=int(self.config.security.max_image_mb * 1024 * 1024),
        )

        user_content: str | list[dict[str, Any]]
        history_user_content: str
        if blobs:
            text = clean_prompt.strip() or "Describe the image(s)."
            user_content = [{"type": "text", "text": text}]
            user_content.extend(
                {"type": "image_url", "image_url": {"url": blob.data_url}} for blob in blobs
            )
            references = "\n".join(
                f"[attached image: {_image_reference(blob.source)}]" for blob in blobs
            )
            history_user_content = f"{text}\n{references}"
        else:
            user_content = clean_prompt
            history_user_content = clean_prompt

        lock = self._session_lock(name, session_id)
        async with lock:
            history = (
                await self._store.get_messages(name, session_id)
                if session_id and self._store is not None
                else []
            )
            messages: list[dict[str, Any]] = []
            if system_text:
                messages.append({"role": "system", "content": system_text})
            messages.extend(message.as_chat_message() for message in history)
            messages.append({"role": "user", "content": user_content})
            self._check_context_window(name, member, messages)

            resolved_temperature = temperature if temperature is not None else member.temperature
            resolved_max_tokens = max_tokens if max_tokens is not None else member.max_tokens
            started = time.perf_counter()
            async with self._semaphores[name]:
                result = await provider.complete(
                    messages,
                    temperature=resolved_temperature,
                    max_tokens=resolved_max_tokens,
                    extra_body=None,
                    timeout=member.timeout_s,
                )

            content = result.content
            if not content and result.reasoning:
                content = "(the model returned only a reasoning trace; enable "
                content += "security.expose_reasoning to view it)"
                warnings.append("model returned no final answer, only reasoning")
            if result.finish_reason == "length":
                warnings.append("output was truncated by the token limit (finish_reason=length)")

            trimmed = 0
            history_messages = len(history) + 1
            if session_id and self._store is not None:
                try:
                    appended = await self._store.append_exchange(
                        name,
                        session_id,
                        history_user_content,
                        content,
                        prompt_tokens=result.usage.prompt_tokens,
                        completion_tokens=result.usage.completion_tokens,
                    )
                except SessionError as exc:
                    _log.warning("failed to persist session %s/%s", name, session_id)
                    warnings.append(f"answer was not saved to session {session_id!r}: {exc}")
                else:
                    trimmed = appended.trimmed
                    history_messages = appended.message_count

        reasoning = None
        if (
            self.config.security.expose_reasoning
            and result.reasoning
            and self.config.security.max_reasoning_chars > 0
        ):
            limit = self.config.security.max_reasoning_chars
            reasoning = (
                result.reasoning
                if len(result.reasoning) <= limit
                else result.reasoning[: limit - 1] + "\u2026"
            )

        return AskResult(
            member=name,
            model=result.model,
            trust=member.trust.value,
            content=content,
            session_id=session_id,
            usage=result.usage.as_dict(),
            elapsed_ms=result.elapsed_ms or int((time.perf_counter() - started) * 1000),
            finish_reason=result.finish_reason,
            reasoning=reasoning,
            warnings=_dedupe(warnings),
            redactions=[{"kind": kind, "count": count} for kind, count in sorted(findings.items())],
            history_messages=history_messages,
            history_trimmed=trimmed,
            images=len(blobs),
        )

    def _check_context_window(
        self, name: str, member: MemberConfig, messages: list[dict[str, Any]]
    ) -> None:
        if not member.context_window:
            return
        estimated = estimate_message_tokens(messages)
        budget = int(member.context_window * 0.9)
        if estimated > budget:
            raise ToolInputError(
                f"estimated prompt size for {name!r} is ~{estimated:,} tokens, above 90% of "
                f"its context_window ({member.context_window:,}). Shorten the prompt, reset "
                "the session, or use a member with a larger context window."
            )

    # ---------------------------------------------------------------- swarm
    async def swarm(
        self,
        member_name: str,
        tasks: Sequence[str],
        *,
        session_prefix: str | None = None,
        max_concurrency: int | None = None,
        context: str | None = None,
        synthesize: bool | None = None,
    ) -> SwarmResult:
        """Run independent prompts against one member in parallel.

        One failing branch never fails the whole call: results carry per-task
        errors so the calling agent can retry or reassign just that slice.
        """
        await self.start()
        name, member = self.config.member(member_name)
        if not member.enabled:
            raise MemberDisabled(f"member {name!r} is disabled in the config")
        if not tasks:
            raise ToolInputError("tasks must contain at least one prompt")
        if len(tasks) > self.config.swarm.max_tasks:
            raise ToolInputError(
                f"too many tasks ({len(tasks)}); swarm.max_tasks is {self.config.swarm.max_tasks}"
            )
        cleaned: list[str] = []
        for index, task in enumerate(tasks, start=1):
            if not isinstance(task, str) or not task.strip():
                raise ToolInputError(f"task #{index} is empty")
            cleaned.append(task)
        if session_prefix is not None:
            session_prefix = validate_session_id(session_prefix)
            if len(session_prefix) > 48:
                raise ToolInputError("session_prefix must be at most 48 characters")
            if not self.config.sessions.enabled:
                raise ToolInputError("sessions are disabled; omit session_prefix")
        if max_concurrency is not None and not 1 <= max_concurrency <= len(cleaned):
            raise ToolInputError(
                f"max_concurrency must be between 1 and {len(cleaned)} (the number of tasks)"
            )
        # Never exceed the member's clone budget, even if the caller asks for more.
        effective_limit = min(member.max_clones, max_concurrency or len(cleaned))
        gate = asyncio.Semaphore(effective_limit) if max_concurrency else None
        self._provider_for(name)  # fail fast on missing credentials

        branch_warnings: list[str] = []

        async def run_one(index: int, task: str) -> SwarmTaskResult:
            session_id = f"{session_prefix}-{index + 1}" if session_prefix else None
            started = time.perf_counter()
            try:
                if gate is None:
                    outcome = await self.ask(name, task, session_id=session_id)
                else:
                    async with gate:
                        outcome = await self.ask(name, task, session_id=session_id)
            except SubFamilyError as exc:
                return SwarmTaskResult(
                    index=index,
                    ok=False,
                    content="",
                    task_preview=_preview(task),
                    session_id=session_id,
                    error=str(exc),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:  # pragma: no cover - defensive
                _log.exception("unexpected swarm branch failure")
                return SwarmTaskResult(
                    index=index,
                    ok=False,
                    content="",
                    task_preview=_preview(task),
                    session_id=session_id,
                    error=f"unexpected error: {type(exc).__name__}",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            branch_warnings.extend(outcome.warnings)
            content, truncated = _truncate(outcome.content, self.config.swarm.max_result_chars)
            return SwarmTaskResult(
                index=index,
                ok=True,
                content=content,
                task_preview=_preview(task),
                session_id=session_id,
                usage=outcome.usage,
                elapsed_ms=outcome.elapsed_ms,
                truncated=truncated,
            )

        started_all = time.perf_counter()
        results = await asyncio.gather(
            *(run_one(index, task) for index, task in enumerate(cleaned))
        )
        warnings: list[str] = list(branch_warnings)
        if any(result.truncated for result in results):
            warnings.append(
                f"some results were truncated to swarm.max_result_chars "
                f"({self.config.swarm.max_result_chars:,})"
            )
        should_synthesize = (
            self.config.swarm.default_synthesize if synthesize is None else synthesize
        )
        synthesis: str | None = None
        if should_synthesize:
            successes = [result for result in results if result.ok]
            if len(successes) >= 2:
                try:
                    synthesis = await self._synthesise(name, cleaned, results, context)
                except SubFamilyError as exc:
                    warnings.append(f"synthesis failed: {exc}")
            else:
                warnings.append("synthesis skipped: fewer than two successful branches")

        return SwarmResult(
            member=name,
            trust=member.trust.value,
            results=results,
            elapsed_ms=int((time.perf_counter() - started_all) * 1000),
            synthesis=synthesis,
            warnings=warnings,
        )

    async def _synthesise(
        self,
        name: str,
        tasks: Sequence[str],
        results: Sequence[SwarmTaskResult],
        context: str | None,
    ) -> str:
        successes = [result for result in results if result.ok]
        budget = 180_000
        per_result = min(
            self.config.swarm.max_result_chars,
            max(1_000, budget // max(1, len(successes))),
        )
        blocks: list[str] = []
        for result in successes:
            body, _ = _truncate(result.content, per_result)
            blocks.append(f"### Task {result.index}\n{tasks[result.index].strip()}\n\n{body}")
        intro = (
            "You are synthesising the outputs of parallel independent analyses produced "
            "by the same model. Merge them into one coherent answer: resolve conflicts, "
            "remove duplication, and preserve concrete file names, commands, and code. "
            "Do not invent results that are not present."
        )
        if context:
            intro += f"\n\nOverall context from the requesting agent:\n{context}"
        prompt = intro + "\n\n" + "\n\n".join(blocks)
        outcome = await self.ask(name, prompt, system="You produce precise technical syntheses.")
        synthesis, _ = _truncate(outcome.content, self.config.swarm.max_result_chars)
        return synthesis

    # --------------------------------------------------------------- status
    async def status(self, member: str | None = None) -> list[MemberStatus]:
        """Probe member endpoints; does not spend generation tokens."""
        await self.start()
        if member is not None:
            canonical, _ = self.config.member(member)
            names = [canonical]
        else:
            names = list(self.config.members)
        return list(await asyncio.gather(*(self._status_one(name) for name in names)))

    async def _status_one(self, name: str) -> MemberStatus:
        member = self.config.members[name]
        base = MemberStatus(
            name=name,
            enabled=member.enabled,
            ok=False,
            model=member.model,
            trust=member.trust.value,
            capabilities=member.capabilities,
            max_clones=member.max_clones,
        )
        if not member.enabled:
            base.error = "member is disabled"
            return base
        if name in self._key_errors:
            base.error = self._key_errors[name]
            return base
        provider = self._providers[name]
        try:
            health = await provider.health()
        except SubFamilyError as exc:  # pragma: no cover - providers classify internally
            base.error = str(exc)
            return base
        return _merge_health(base, health)

    # -------------------------------------------------------------- sessions
    async def list_sessions(self, member: str | None = None) -> list[dict[str, Any]]:
        """List stored session metadata."""
        await self.start()
        if not self.config.sessions.enabled or self._store is None:
            return []
        if member is not None:
            member = self.config.member(member)[0]
        sessions = await self._store.list_sessions(member)
        return [
            {
                "member": info.member,
                "session_id": info.session_id,
                "message_count": info.message_count,
                "created_at": info.created_at,
                "updated_at": info.updated_at,
                "last_preview": info.last_preview,
            }
            for info in sessions
        ]

    async def session_history(
        self, member: str, session_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return recent messages for one session (for debugging and CLI)."""
        await self.start()
        canonical, _ = self.config.member(member)
        session_id = validate_session_id(session_id)
        if not self.config.sessions.enabled or self._store is None:
            raise ToolInputError("sessions are disabled in the config")
        info = await self._store.get_session(canonical, session_id)
        if info is None:
            raise SessionError(f"session {session_id!r} for member {canonical!r} was not found")
        messages: list[StoredMessage] = await self._store.get_history(
            canonical, session_id, limit=max(1, min(limit, 200))
        )
        return [
            {
                "role": message.role,
                "content": _stringify_content(message.content),
                "created_at": message.created_at,
            }
            for message in messages
        ]

    async def reset_session(self, member: str, session_id: str) -> dict[str, int]:
        """Delete one session, or every session of a member when id is ``*``."""
        await self.start()
        canonical, _ = self.config.member(member)
        if not self.config.sessions.enabled or self._store is None:
            raise ToolInputError("sessions are disabled in the config")
        if session_id == "*":
            existing = await self._store.list_sessions(canonical)
            removed = await self._store.reset_member_sessions(canonical)
            self._drop_locks(canonical)
            return {"sessions": len(existing), "messages": removed}
        session_id = validate_session_id(session_id)
        removed = await self._store.reset_session(canonical, session_id)
        self._session_locks.pop((canonical, session_id), None)
        return {"sessions": 1 if removed else 0, "messages": removed}

    async def prune_sessions(self, ttl_days: int | None = None) -> int:
        """Delete expired sessions, returning the number of sessions removed."""
        await self.start()
        if not self.config.sessions.enabled or self._store is None:
            return 0
        days = self.config.sessions.ttl_days if ttl_days is None else ttl_days
        removed = await self._store.prune(days)
        if removed:
            self._session_locks.clear()
        return removed

    # -------------------------------------------------------------- helpers
    def _provider_for(self, name: str) -> ChatProvider:
        if name in self._key_errors:
            raise ConfigError(self._key_errors[name])
        try:
            return self._providers[name]
        except KeyError as exc:  # pragma: no cover - start() guarantees presence
            raise ConfigError(f"provider for member {name!r} is not initialised") from exc

    def _session_lock(self, member: str, session_id: str | None) -> Any:
        if session_id is None:
            return _NULL_LOCK
        key = (member, session_id)
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    def _drop_locks(self, member: str) -> None:
        for key in [key for key in self._session_locks if key[0] == member]:
            self._session_locks.pop(key, None)


class _NoopLock:
    """Async context manager used for stateless calls.

    A module-level ``asyncio.Lock`` cannot be used here because it binds to the
    event loop on first acquisition and would break when a new loop starts (as
    happens in tests). A no-op keeps the call path uniform without that hazard.
    """

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


_NULL_LOCK = _NoopLock()


def _default_client(member: MemberConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=member.verify_tls,
        timeout=None,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=max(4, member.max_clones * 2),
            max_keepalive_connections=max(2, member.max_clones),
        ),
    )


def _default_provider(
    name: str, member: MemberConfig, api_key: str | None, client: httpx.AsyncClient
) -> ChatProvider:
    return OpenAICompatProvider(name, member, api_key=api_key, client=client)


def _merge_health(base: MemberStatus, health: ProviderHealth) -> MemberStatus:
    base.ok = health.ok
    base.latency_ms = health.latency_ms
    base.model_available = health.model_available
    base.models = health.models[:50]
    base.error = health.error
    return base


def _auth_configured(member: MemberConfig) -> bool:
    if member.api_key is not None:
        return True
    if member.api_key_env:
        return bool(os.environ.get(member.api_key_env))
    return True  # endpoints without auth are valid (local Ollama, LM Studio)


def _join_system(config_prompt: str | None, call_prompt: str | None) -> str | None:
    parts = [part.strip() for part in (config_prompt, call_prompt) if part and part.strip()]
    return "\n\n".join(parts) if parts else None


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "\u2026", True


def _preview(text: str, limit: int = 120) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "\u2026"


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _image_reference(source: str) -> str:
    """Short, non-sensitive label stored in session history for an image."""
    if source.startswith("data:"):
        return "inline image"
    if source.lower().startswith(("http://", "https://")):
        parts = urlsplit(source)
        return f"{parts.scheme}://{parts.netloc}/..."
    return Path(source).name


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)
