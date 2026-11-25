"""MCP tool surface for Sub-Family.

Every tool is a thin, well-documented wrapper around :class:`FamilyService`.
The docstrings are not decoration: MCP clients (Claude Code in particular) send
them to the model as the tool description, so they carry the delegation policy,
the trust model, and the cost/concurrency implications of each call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sub_family import __version__, paths
from sub_family.config import load_config
from sub_family.config.loader import load_environment
from sub_family.errors import ConfigError, SubFamilyError
from sub_family.logging_config import configure_logging, get_logger
from sub_family.service import FamilyService

_log = get_logger("server")

SERVER_INSTRUCTIONS = """\
Sub-Family is a delegation layer: it gives you additional language models
("family members") with different capabilities, privacy levels, and hardware.

Operating rules:
1. Call `list_family` before delegating. Pick members by `capabilities`
   (vision, reasoning, code, fast, long_context) and by `trust`.
2. `ask_family_swarm` is for 3 or more independent subtasks. Fan them out
   instead of spawning resource-hungry local subagents; the model provider runs
   the branches in parallel, not this machine.
3. Use a stable `session_id` for iterative work with one member; omit it for
   one-shot questions. Sessions persist across restarts and are isolated per
   (member, session_id). Reset them when a task is finished.
4. Never send API keys, `.env` contents, credentials, or private business logic
   to members whose trust is `untrusted_cloud`. The server redacts known secret
   shapes as a seatbelt, not a guarantee; do not rely on it.
5. Images can only go to members with the `vision` capability. Prefer local
   vision members for screenshots that may contain secrets.
6. Member answers are advice, not verified facts. Check code and commands
   before applying them, and keep the final judgement on the primary agent.
7. Check `family_status` when a member errors or before a large fan-out; local
   machines can be offline and small GPUs have a clone limit of 1.
"""


def create_server(service: FamilyService, *, own_service: bool = False) -> MCPServer:
    """Build the MCP server around a configured service.

    Set ``own_service`` when the server should close the service (and its HTTP
    clients and session database) on shutdown; long-lived embedders that manage
    the service themselves leave it false.
    """

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            if own_service:
                await service.aclose()

    server = MCPServer(
        name="sub-family",
        title="Sub-Family",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://github.com/MistaKilla/Sub-Family-MCP",
        lifespan=lifespan,
    )

    @server.tool()
    async def list_family(include_disabled: bool = False) -> str:
        """List the available model family and what each member is good at.

        Call this before delegating work. Each member has a name, model id,
        capabilities (`vision`, `reasoning`, `code`, `fast`, `long_context`),
        a trust level, a context window, and a concurrency limit (`max_clones`).
        Members marked `untrusted_cloud` leave the local machine and must not
        receive secrets. Disabled members are hidden unless `include_disabled`
        is true.

        Args:
            include_disabled: Also return members disabled in the config.

        Returns:
            JSON with `members` (array) and `count`.
        """
        members = service.list_members(include_disabled=include_disabled)
        return _json(
            {
                "members": members,
                "count": len(members),
                "tip": (
                    "Use ask_family for one member, ask_family_swarm for 3+ independent "
                    "subtasks, and family_status to check reachability."
                ),
            }
        )

    @server.tool()
    async def family_status(member: str | None = None) -> str:
        """Check whether family members are reachable right now.

        Probes each member's OpenAI-compatible `/models` endpoint. This does not
        spend generation tokens and is safe to call before a batch of work. A
        local member that is offline, or a missing API key, shows up as `ok:
        false` with an explanation; a reachable endpoint with a missing model
        shows `model_available: false`.

        Args:
            member: Optional single member name. Omit to check everyone.

        Returns:
            JSON with one status object per member.
        """
        try:
            statuses = await service.status(member)
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json({"members": [status.to_dict() for status in statuses]})

    @server.tool()
    async def ask_family(
        member: str,
        prompt: str,
        session_id: str | None = None,
        images: list[str] | None = None,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send one prompt to one family member and return its answer.

        Use this to delegate a single, well-scoped piece of work: architecture
        review, a hard bug, a second opinion, screenshot analysis, boilerplate
        generation. For several independent tasks, use `ask_family_swarm`.

        Sessions: pass a stable `session_id` (for example `"auth-refactor"`) to
        keep a multi-turn conversation with that member. The server stores the
        history and replays it automatically; the calling model only sees the
        latest answer, which keeps its own context small. Omit `session_id` for
        a stateless call. Reset a finished session with `reset_session`.

        Images: pass local file paths, http(s) URLs, or `data:image/...;base64`
        URIs. The member must have the `vision` capability. Image bytes are not
        stored in session history (only a text reference), to keep the database
        small.

        Privacy: `untrusted_cloud` members are sent through the secret guard,
        which redacts common credential shapes and returns a warning. That is a
        safety net, not a guarantee -- never intentionally paste secrets.

        Args:
            member: Member name from `list_family`.
            prompt: The task or question. May be empty only when images are sent.
            session_id: Optional persistent conversation id (1-64 safe chars).
            images: Optional image paths/URLs/data URIs (max 8, vision members).
            system: Optional per-call system instruction, prepended to the
                member's configured system prompt for this call only.
            temperature: Optional sampling override (0-2).
            max_tokens: Optional output limit override.

        Returns:
            JSON with `content`, provenance (`member`, `model`, `trust`), usage,
            warnings, and session metadata.
        """
        try:
            result = await service.ask(
                member,
                prompt,
                session_id=session_id,
                images=images,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json(result.to_dict())

    @server.tool()
    async def ask_family_swarm(
        member: str,
        tasks: list[str],
        session_prefix: str | None = None,
        max_concurrency: int | None = None,
        context: str | None = None,
        synthesize: bool | None = None,
    ) -> str:
        """Run several independent prompts against one member in parallel.

        This is the "bring the whole crew" tool: the server issues the requests
        concurrently (bounded by the member's `max_clones`), so a powerful cloud
        model can work on many slices while the local machine stays idle. One
        failing branch does not fail the call; each result has `ok` and `error`.

        Use it for 3+ truly independent subtasks (audit five modules, generate
        tests for several files, compare approaches). Do not use it for steps
        that depend on each other -- those belong in one prompt or one session.

        Args:
            member: Member name; must have enough capacity for the fan-out.
            tasks: Independent prompts, one per branch (at most swarm.max_tasks).
            session_prefix: If set, branch N gets the persistent session
                `<prefix>-N`, so the same branch can be continued later.
            max_concurrency: Optional lower concurrency cap (never above the
                member's `max_clones`).
            context: Optional shared background included in the synthesis step.
            synthesize: Ask the member to merge successful branch outputs into
                one answer (extra request). Defaults to the config setting.

        Returns:
            JSON with per-branch results, a success/failure summary, optional
            synthesis, and warnings.
        """
        try:
            result = await service.swarm(
                member,
                tasks,
                session_prefix=session_prefix,
                max_concurrency=max_concurrency,
                context=context,
                synthesize=synthesize,
            )
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json(result.to_dict())

    @server.tool()
    async def list_sessions(member: str | None = None) -> str:
        """List persisted virtual conversations.

        Sessions are stored by `(member, session_id)` and survive restarts. Use
        this to discover a session id you created earlier, or to check whether a
        branch still has history before continuing it.

        Args:
            member: Optional member name to filter by.

        Returns:
            JSON array of session metadata, newest first.
        """
        try:
            sessions = await service.list_sessions(member)
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json({"sessions": sessions, "count": len(sessions)})

    @server.tool()
    async def session_history(member: str, session_id: str, limit: int = 20) -> str:
        """Read recent messages from one session.

        Useful when you need to recover the exact earlier context of a branch
        (for example after another agent worked in it). Prefer letting
        `ask_family` replay history automatically; reading it into the main
        context costs tokens.

        Args:
            member: Member name.
            session_id: Session to inspect.
            limit: Number of most recent messages to return (1-200).

        Returns:
            JSON array of `{role, content, created_at}` messages.
        """
        try:
            messages = await service.session_history(member, session_id, limit=limit)
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json({"member": member, "session_id": session_id, "messages": messages})

    @server.tool()
    async def reset_session(member: str, session_id: str) -> str:
        """Delete stored history for one session, or all sessions of a member.

        Call this when a task is finished so stale context does not leak into
        later work. This only deletes local history; it does not affect the
        provider or any files.

        Args:
            member: Member name.
            session_id: Session to delete, or `*` explicitly for every session
                of the member. Pass the exact id unless you intend a full wipe.

        Returns:
            JSON with the number of removed sessions and messages.
        """
        try:
            removed = await service.reset_session(member, session_id)
        except SubFamilyError as exc:
            _raise_tool_error(exc)
        return _json({"ok": True, "member": member, "session_id": session_id, **removed})

    return server


def run_stdio(config_path: Path | None = None, *, log_level: str = "WARNING") -> None:
    """Load the config and serve MCP over stdio (the Claude Code transport)."""
    path = (config_path or paths.config_path()).expanduser()
    load_environment(path.parent / ".env")
    configure_logging(log_level)
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"sub-family: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    service = FamilyService(config, config_path=path)
    server = create_server(service, own_service=True)
    _log.info("serving %d member(s) over stdio", len(config.members))
    server.run("stdio")


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _raise_tool_error(exc: SubFamilyError) -> NoReturn:
    raise ToolError(str(exc)) from exc
