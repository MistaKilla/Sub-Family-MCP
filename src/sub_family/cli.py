"""Console administration for Sub-Family.

The CLI and the MCP server share the same service and configuration layer, so a
member tested here behaves identically when Claude calls it. Commands are
non-interactive by default (scriptable); the full-screen editor is ``ui``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from sub_family import __version__, paths
from sub_family.config.loader import (
    CONFIG_HEADER,
    load_config,
    load_environment,
    resolve_db_path,
    save_config,
    write_example_config,
)
from sub_family.config.models import FamilyConfig, MemberConfig, TrustLevel
from sub_family.errors import ConfigError, SubFamilyError
from sub_family.logging_config import configure_logging
from sub_family.service import FamilyService, MemberStatus
from sub_family.sessions.store import SessionStore

app = typer.Typer(
    name="sub-family",
    help="Multi-model delegation family for Claude Code and other MCP clients.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
members_app = typer.Typer(help="Inspect and edit family members.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect, validate, and edit family.yaml.", no_args_is_help=True)
sessions_app = typer.Typer(help="Inspect and clean virtual sessions.", no_args_is_help=True)
claude_app = typer.Typer(help="Claude Code integration snippets.", no_args_is_help=True)
app.add_typer(members_app, name="members")
app.add_typer(config_app, name="config")
app.add_typer(sessions_app, name="sessions")
app.add_typer(claude_app, name="claude")

console = Console()
err_console = Console(stderr=True)

TRUST_ALIASES = {
    "local": TrustLevel.SAFE_LOCAL,
    "safe_local": TrustLevel.SAFE_LOCAL,
    "lan": TrustLevel.PRIVATE_NETWORK,
    "private": TrustLevel.PRIVATE_NETWORK,
    "private_network": TrustLevel.PRIVATE_NETWORK,
    "cloud": TrustLevel.UNTRUSTED_CLOUD,
    "untrusted_cloud": TrustLevel.UNTRUSTED_CLOUD,
}


@dataclass(slots=True)
class CLIContext:
    """Values shared by all commands."""

    config_path: Path


@contextmanager
def _errors() -> Iterator[None]:
    """Print expected failures compactly instead of raising a traceback."""
    try:
        yield
    except SubFamilyError as exc:
        err_console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(1) from exc


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"sub-family {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to family.yaml (default: SUB_FAMILY_CONFIG or platform config dir).",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        is_eager=True,
        callback=_version_callback,
        help="Show the version and exit.",
    ),
) -> None:
    del version
    config_path = (config or paths.config_path()).expanduser()
    load_environment(config_path.parent / ".env")
    configure_logging(os.environ.get("SUB_FAMILY_LOG_LEVEL", "WARNING"))
    ctx.obj = CLIContext(config_path=config_path)


def _ctx(ctx: typer.Context) -> CLIContext:
    if not isinstance(ctx.obj, CLIContext):
        return CLIContext(config_path=paths.config_path())
    return ctx.obj


def _load(ctx: typer.Context) -> tuple[FamilyConfig, Path]:
    cli = _ctx(ctx)
    load_environment(cli.config_path.parent / ".env")
    config = load_config(cli.config_path)
    return config, cli.config_path


def _save(config: FamilyConfig, path: Path) -> None:
    save_config(config, path)
    console.print(f"[green]Saved[/green] {escape(str(path))}")


def _validate_family(raw: dict[str, Any], path: Path) -> FamilyConfig:
    try:
        return FamilyConfig.model_validate(raw)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"invalid configuration for {path}:\n{details}") from exc


def _validate_member(raw: dict[str, Any]) -> MemberConfig:
    try:
        return MemberConfig.model_validate(raw)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"invalid member settings:\n{details}") from exc


def _member_dict(config: FamilyConfig, name: str) -> dict[str, Any]:
    _, member = config.member(name)
    data = member.model_dump(mode="json", exclude_none=True)
    if data.get("api_key"):
        data["api_key"] = "***"
    return data


def _parse_trust(value: str | TrustLevel) -> TrustLevel:
    if isinstance(value, TrustLevel):
        return value
    try:
        return TRUST_ALIASES[value.strip().lower()]
    except KeyError as exc:
        choices = ", ".join(sorted(TRUST_ALIASES))
        raise ConfigError(f"unknown trust level {value!r}; choose one of: {choices}") from exc


def _short(value: object, limit: int = 70) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# --------------------------------------------------------------------- init
@app.command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config (keeps .bak)."),
) -> None:
    """Create a commented starter family.yaml with big_brother and lilbro."""
    cli = _ctx(ctx)
    with _errors():
        path = write_example_config(cli.config_path, force=force)
    console.print(
        Panel.fit(
            f"[green]Created[/green] {escape(str(path))}\n\n"
            "Next steps:\n"
            "  1. [bold]sub-family ui[/bold]                  edit members visually\n"
            "  2. [bold]sub-family status[/bold]              check reachability\n"
            "  3. [bold]sub-family claude snippet[/bold]      connect Claude Code\n\n"
            "Set API keys in your shell or in the .env file next to the config.",
            title="Sub-Family",
            border_style="green",
        )
    )


@app.command("paths")
def show_paths(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the config, env, database, and data paths in use."""
    entries = {
        "config_file": paths.config_path(),
        "env_file": paths.env_file_path(),
        "data_dir": paths.data_dir(),
        "sessions_db": paths.sessions_db_path(),
    }
    if as_json:
        console.print_json(
            data={key: str(value) for key, value in entries.items()},
        )
        return
    table = Table(title="Sub-Family paths", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Path")
    table.add_column("Exists", justify="center")
    for key, value in entries.items():
        table.add_row(key, escape(str(value)), "[green]yes[/green]" if value.exists() else "no")
    console.print(table)


# ------------------------------------------------------------------ members
@app.command("list")
def list_command(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured family members."""
    with _errors():
        config, _ = _load(ctx)
    members = config.members
    if as_json:
        console.print_json(
            data=[
                {
                    "name": name,
                    "model": member.model,
                    "trust": member.trust.value,
                    "capabilities": member.capabilities,
                    "max_clones": member.max_clones,
                    "enabled": member.enabled,
                    "base_url": member.base_url,
                }
                for name, member in members.items()
            ]
        )
        return
    if not members:
        console.print("[yellow]No members configured.[/yellow] Run [bold]sub-family init[/bold].")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Model")
    table.add_column("Trust", no_wrap=True)
    table.add_column("Capabilities")
    table.add_column("Clones", justify="right")
    table.add_column("State")
    for name, member in members.items():
        table.add_row(
            name,
            escape(member.model),
            _trust_cell(member.trust),
            escape(", ".join(member.capabilities)),
            str(member.max_clones),
            "[green]on[/green]" if member.enabled else "[dim]off[/dim]",
        )
    console.print(table)
    console.print(
        "[dim]Tip:[/dim] [bold]sub-family ui[/bold] edits these settings; "
        "[bold]sub-family status[/bold] checks reachability."
    )


@members_app.command("show")
def members_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Member name."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one member's full settings (API keys are masked)."""
    with _errors():
        config, _ = _load(ctx)
        data = _member_dict(config, name)
    if as_json:
        console.print_json(data=data)
        return
    table = Table(title=f"Member: {name}", show_header=False)
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        table.add_row(key, escape(str(value)))
    console.print(table)


@members_app.command("add")
def members_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Lowercase member name, e.g. farm_two."),
    base_url: str = typer.Option(..., "--base-url", "-u", help="OpenAI-compatible API root."),
    model: str = typer.Option(..., "--model", "-m", help="Model id understood by the endpoint."),
    description: str = typer.Option("", "--description", "-d", help="Short purpose note."),
    trust: str = typer.Option(
        "safe_local",
        "--trust",
        "-t",
        help="safe_local | private_network | untrusted_cloud (aliases: local, lan, cloud).",
    ),
    capability: list[str] = typer.Option(
        ["chat"], "--capability", "-C", help="Repeatable: vision, code, reasoning, fast, ..."
    ),
    max_clones: int = typer.Option(1, "--max-clones", min=1, max=64),
    timeout_s: float = typer.Option(120.0, "--timeout", min=1.0, max=3600.0),
    api_key_env: str | None = typer.Option(
        None, "--api-key-env", "-k", help="Environment variable holding the API key."
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Literal API key (stored in plaintext; prefer --api-key-env)."
    ),
    system_prompt: str | None = typer.Option(None, "--system-prompt"),
    disabled: bool = typer.Option(False, "--disabled", help="Add the member disabled."),
) -> None:
    """Add a member from flags (use `ui` for the full interactive form)."""
    with _errors():
        config, path = _load(ctx)
        if name in config.members:
            raise ConfigError(f"member {name!r} already exists; use `members edit`")
        if api_key:
            err_console.print(
                "[yellow]Warning:[/yellow] --api-key stores the key in family.yaml; "
                "prefer --api-key-env."
            )
        member = _validate_member(
            {
                "description": description,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "api_key_env": api_key_env,
                "trust": _parse_trust(trust),
                "capabilities": capability,
                "max_clones": max_clones,
                "timeout_s": timeout_s,
                "system_prompt": system_prompt,
                "enabled": not disabled,
            }
        )
        members = {**config.members, name: member}
        updated = _validate_family(
            {**config.model_dump(exclude_none=True), "members": members}, path
        )
        _save(updated, path)
    console.print(
        f"Added [bold]{escape(name)}[/bold]. Check it with [bold]sub-family status[/bold]."
    )


@members_app.command("edit")
def members_edit(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Member name."),
    base_url: str | None = typer.Option(None, "--base-url", "-u"),
    model: str | None = typer.Option(None, "--model", "-m"),
    description: str | None = typer.Option(None, "--description", "-d"),
    trust: str | None = typer.Option(None, "--trust", "-t"),
    capability: list[str] | None = typer.Option(None, "--capability", "-C"),
    clear_capabilities: bool = typer.Option(False, "--clear-capabilities"),
    max_clones: int | None = typer.Option(None, "--max-clones", min=1, max=64),
    timeout_s: float | None = typer.Option(None, "--timeout", min=1.0, max=3600.0),
    api_key_env: str | None = typer.Option(None, "--api-key-env", "-k"),
    api_key: str | None = typer.Option(None, "--api-key"),
    clear_api_key: bool = typer.Option(False, "--clear-api-key"),
    system_prompt: str | None = typer.Option(None, "--system-prompt"),
    clear_system_prompt: bool = typer.Option(False, "--clear-system-prompt"),
) -> None:
    """Update selected fields of an existing member; unspecified fields are kept."""
    with _errors():
        config, path = _load(ctx)
        canonical, member = config.member(name)
        update: dict[str, Any] = {}
        for key, value in {
            "base_url": base_url,
            "model": model,
            "description": description,
            "max_clones": max_clones,
            "timeout_s": timeout_s,
        }.items():
            if value is not None:
                update[key] = value
        if trust is not None:
            update["trust"] = _parse_trust(trust)
        if clear_capabilities:
            update["capabilities"] = []
        elif capability is not None:
            update["capabilities"] = capability
        if clear_api_key:
            update["api_key"] = None
        elif api_key is not None:
            update["api_key"] = api_key
        if api_key_env is not None:
            update["api_key_env"] = api_key_env
        if clear_system_prompt:
            update["system_prompt"] = None
        elif system_prompt is not None:
            update["system_prompt"] = system_prompt
        if not update:
            console.print("[yellow]Nothing to change.[/yellow]")
            return
        raw = member.model_dump(mode="python", exclude_none=True)
        raw.update(update)
        if update.get("api_key") is None and "api_key" in update:
            raw.pop("api_key", None)
        if update.get("system_prompt") is None and "system_prompt" in update:
            raw.pop("system_prompt", None)
        updated_member = _validate_member(raw)
        members = {**config.members, canonical: updated_member}
        updated = _validate_family(
            {**config.model_dump(exclude_none=True), "members": members}, path
        )
        _save(updated, path)


@members_app.command("remove")
def members_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Member name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Remove a member from the registry (its sessions are kept)."""
    with _errors():
        config, path = _load(ctx)
        canonical, _ = config.member(name)
        if not yes and not typer.confirm(f"Remove member {canonical!r}?", default=False):
            raise typer.Abort()
        members = {key: value for key, value in config.members.items() if key != canonical}
        updated = _validate_family(
            {**config.model_dump(exclude_none=True), "members": members}, path
        )
        _save(updated, path)
    console.print(f"Removed [bold]{escape(name)}[/bold].")


def _set_enabled(ctx: typer.Context, name: str, enabled: bool) -> None:
    with _errors():
        config, path = _load(ctx)
        canonical, member = config.member(name)
        updated_member = member.model_copy(update={"enabled": enabled})
        members = {**config.members, canonical: updated_member}
        updated = _validate_family(
            {**config.model_dump(exclude_none=True), "members": members}, path
        )
        _save(updated, path)
    state = "enabled" if enabled else "disabled"
    console.print(f"Member [bold]{escape(name)}[/bold] {state}.")


@members_app.command("enable")
def members_enable(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Enable a member."""
    _set_enabled(ctx, name, True)


@members_app.command("disable")
def members_disable(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Disable a member without deleting it."""
    _set_enabled(ctx, name, False)


@members_app.command("test")
def members_test(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Member name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Probe one member's endpoint and model availability."""
    with _errors():
        config, path = _load(ctx)
        statuses = asyncio.run(_statuses(config, path, name))
    if as_json:
        console.print_json(data=[status.to_dict() for status in statuses])
    else:
        _print_status_table(statuses)
    if any(status.enabled and not status.ok for status in statuses):
        raise typer.Exit(1)


# ------------------------------------------------------------------- status
async def _statuses(
    config: FamilyConfig, path: Path, member: str | None = None
) -> list[MemberStatus]:
    async with FamilyService(config, config_path=path) as service:
        return await service.status(member)


@app.command("status")
def status_command(
    ctx: typer.Context,
    member: str | None = typer.Option(None, "--member", "-m", help="Check only this member."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check reachability and model availability for every member."""
    with _errors():
        config, path = _load(ctx)
        if not config.members:
            console.print("[yellow]No members configured.[/yellow]")
            return
        statuses = asyncio.run(_statuses(config, path, member))
    if as_json:
        console.print_json(data=[status.to_dict() for status in statuses])
    else:
        _print_status_table(statuses)
    if any(item.enabled and not item.ok for item in statuses):
        raise typer.Exit(1)


def _print_status_table(statuses: list[MemberStatus]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Member", style="bold")
    table.add_column("State")
    table.add_column("Latency", justify="right")
    table.add_column("Model available", justify="center")
    table.add_column("Error")
    for status in statuses:
        if not status.enabled:
            state = "[dim]disabled[/dim]"
        elif status.ok:
            state = "[green]ok[/green]"
        else:
            state = "[red]fail[/red]"
        if status.model_available is True:
            available = "[green]yes[/green]"
        elif status.model_available is False:
            available = "[yellow]no[/yellow]"
        else:
            available = "[dim]?[/dim]"
        table.add_row(
            status.name,
            state,
            f"{status.latency_ms} ms" if status.ok else "-",
            available,
            escape(_short(status.error or "", 80)),
        )
    console.print(table)
    if any(status.ok and status.model_available is False for status in statuses):
        console.print(
            "[yellow]A reachable endpoint does not expose the configured model id.[/yellow] "
            "Check the `model` value and whether the model is pulled."
        )


def _trust_cell(trust: TrustLevel) -> str:
    colour = {
        TrustLevel.SAFE_LOCAL: "green",
        TrustLevel.PRIVATE_NETWORK: "yellow",
        TrustLevel.UNTRUSTED_CLOUD: "red",
    }[trust]
    return f"[{colour}]{trust.value}[/{colour}]"


# ------------------------------------------------------------------- config
@config_app.command("path")
def config_path_command(ctx: typer.Context) -> None:
    """Print the active configuration path."""
    console.print(str(_ctx(ctx).config_path))


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration with API keys masked."""
    with _errors():
        config, _ = _load(ctx)
    body = yaml.safe_dump(config.masked_dump(), sort_keys=False, allow_unicode=True)
    console.print(Syntax(f"{CONFIG_HEADER}\n{body}", "yaml", theme="ansi_dark"))


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate family.yaml and summarize what was found."""
    with _errors():
        config, path = _load(ctx)
    enabled = len(config.enabled_members())
    console.print(
        f"[green]Valid[/green] {escape(str(path))}: {len(config.members)} member(s), "
        f"{enabled} enabled, sessions "
        f"{'on' if config.sessions.enabled else 'off'}."
    )


@config_app.command("edit")
def config_edit(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the validation prompt on errors."),
) -> None:
    """Open family.yaml in $VISUAL/$EDITOR and validate the result before saving."""
    with _errors():
        config, path = _load(ctx)
    raw_config = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    current = f"{CONFIG_HEADER}\n{raw_config}"
    edited = click.edit(current, extension=".yaml")
    if edited is None or edited == current:
        console.print("[dim]No changes.[/dim]")
        return
    try:
        raw = yaml.safe_load(edited)
        if not isinstance(raw, dict):
            raise ConfigError("the edited YAML must be a mapping at the top level")
        new_config = FamilyConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError, ConfigError) as exc:
        fallback = path.with_suffix(path.suffix + ".edited")
        fallback.write_text(edited, encoding="utf-8")
        err_console.print(f"[bold red]Not saved:[/bold red] {escape(str(exc))}")
        err_console.print(f"Your edit was kept at {escape(str(fallback))}")
        if not yes:
            raise typer.Exit(1) from exc
        return
    save_config(new_config, path)
    console.print(f"[green]Saved[/green] {escape(str(path))}")


# ----------------------------------------------------------------- sessions
@asynccontextmanager
async def _open_store(config: FamilyConfig, path: Path) -> AsyncIterator[SessionStore]:
    if not config.sessions.enabled:
        raise ConfigError("sessions are disabled in the config")
    store = SessionStore(resolve_db_path(config, path), config.sessions)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@sessions_app.command("list")
def sessions_list(
    ctx: typer.Context,
    member: str | None = typer.Option(None, "--member", "-m"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List stored sessions, newest first."""
    with _errors():
        config, path = _load(ctx)

        async def collect() -> list[dict[str, Any]]:
            async with _open_store(config, path) as store:
                infos = await store.list_sessions(member)
                return [
                    {
                        "member": info.member,
                        "session_id": info.session_id,
                        "message_count": info.message_count,
                        "updated_at": info.updated_at,
                        "last_preview": info.last_preview,
                    }
                    for info in infos
                ]

        rows = asyncio.run(collect())
    if as_json:
        console.print_json(data=rows)
        return
    if not rows:
        console.print("[dim]No sessions stored.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Member")
    table.add_column("Session")
    table.add_column("Messages", justify="right")
    table.add_column("Updated")
    table.add_column("Last preview")
    for row in rows:
        table.add_row(
            row["member"],
            row["session_id"],
            str(row["message_count"]),
            row["updated_at"],
            escape(_short(row["last_preview"], 60)),
        )
    console.print(table)


@sessions_app.command("show")
def sessions_show(
    ctx: typer.Context,
    member: str = typer.Argument(...),
    session_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print recent messages of one session."""
    with _errors():
        config, path = _load(ctx)

        async def collect() -> list[dict[str, Any]]:
            async with _open_store(config, path) as store:
                canonical, _ = config.member(member)
                info = await store.get_session(canonical, session_id)
                if info is None:
                    raise ConfigError(
                        f"session {session_id!r} for member {canonical!r} was not found"
                    )
                messages = await store.get_history(canonical, session_id, limit=limit)
                return [
                    {
                        "role": message.role,
                        "created_at": message.created_at,
                        "content": message.content,
                    }
                    for message in messages
                ]

        messages = asyncio.run(collect())
    if as_json:
        console.print_json(data=messages)
        return
    for message in messages:
        colour = "cyan" if message["role"] == "user" else "green"
        console.print(
            f"[{colour} bold]{message['role']}[/{colour} bold] [dim]{message['created_at']}[/dim]"
        )
        console.print(str(message["content"]))
        console.print()


@sessions_app.command("reset")
def sessions_reset(
    ctx: typer.Context,
    member: str = typer.Argument(...),
    session_id: str = typer.Argument("*", help="Session id, or * for all sessions of the member."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete one session's history (or all of a member's sessions)."""
    with _errors():
        config, path = _load(ctx)
        canonical, _ = config.member(member)
        if not yes and not typer.confirm(
            f"Delete session {session_id!r} for {canonical!r}?", default=False
        ):
            raise typer.Abort()

        async def reset() -> int:
            async with _open_store(config, path) as store:
                if session_id == "*":
                    return await store.reset_member_sessions(canonical)
                return await store.reset_session(canonical, session_id)

        removed = asyncio.run(reset())
    console.print(f"Removed {removed} message(s).")


@sessions_app.command("prune")
def sessions_prune(
    ctx: typer.Context,
    days: int = typer.Option(
        30, "--days", min=0, help="Delete sessions older than N days (0 = no-op)."
    ),
) -> None:
    """Delete expired sessions according to a TTL."""
    with _errors():
        config, path = _load(ctx)

        async def prune() -> int:
            async with _open_store(config, path) as store:
                return await store.prune(days)

        removed = asyncio.run(prune())
    console.print(f"Pruned {removed} session(s).")


# ------------------------------------------------------------------ doctor
@app.command()
def doctor(
    ctx: typer.Context,
    network: bool = typer.Option(False, "--network", help="Also probe every endpoint."),
) -> None:
    """Check config, storage, keys, and (optionally) network reachability."""
    checks: list[tuple[str, str, str]] = []
    cli = _ctx(ctx)
    checks.append(
        ("Python", "ok" if sys.version_info >= (3, 11) else "fail", sys.version.split()[0])
    )
    exists = cli.config_path.exists()
    checks.append(("Config file", "ok" if exists else "fail", str(cli.config_path)))
    config: FamilyConfig | None = None
    if exists:
        try:
            config, _ = _load(ctx)
            checks.append(("Config valid", "ok", f"{len(config.members)} member(s)"))
        except SubFamilyError as exc:
            checks.append(("Config valid", "fail", str(exc)))
    if config is not None:
        try:
            config_dir_writable = os.access(cli.config_path.parent, os.W_OK)
        except OSError:  # pragma: no cover
            config_dir_writable = False
        checks.append(
            (
                "Config writable",
                "ok" if config_dir_writable else "fail",
                str(cli.config_path.parent),
            )
        )
        if config.sessions.enabled:
            try:
                asyncio.run(_check_db(config, cli.config_path))
                checks.append(
                    (
                        "Session database",
                        "ok",
                        str(resolve_db_path(config, cli.config_path)),
                    )
                )
            except SubFamilyError as exc:
                checks.append(("Session database", "fail", str(exc)))
        else:
            checks.append(("Session database", "ok", "disabled in config"))
        missing_keys = [
            name
            for name, member in config.enabled_members().items()
            if member.api_key is None
            and member.api_key_env
            and not os.environ.get(member.api_key_env)
        ]
        checks.append(
            (
                "API keys",
                "ok" if not missing_keys else "warn",
                "all present" if not missing_keys else "missing: " + ", ".join(missing_keys),
            )
        )
        cloud = [
            name
            for name, member in config.enabled_members().items()
            if member.trust is TrustLevel.UNTRUSTED_CLOUD
        ]
        if cloud and not config.security.redact_secrets:
            checks.append(
                (
                    "Secret guard",
                    "fail",
                    f"disabled while cloud members exist: {', '.join(cloud)}",
                )
            )
        else:
            checks.append(
                (
                    "Secret guard",
                    "ok",
                    "enabled" if config.security.redact_secrets else "not needed",
                )
            )
        claude_path = shutil.which("claude")
        checks.append(
            ("Claude Code", "ok" if claude_path else "warn", claude_path or "not on PATH")
        )
    table = Table(title="sub-family doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    styles = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
    for label, status, detail in checks:
        table.add_row(label, styles[status], escape(_short(detail, 90)))
    console.print(table)
    network_failed = False
    if network and config is not None:
        statuses = asyncio.run(_statuses(config, cli.config_path))
        _print_status_table(statuses)
        network_failed = any(item.enabled and not item.ok for item in statuses)
    if any(status == "fail" for _, status, _ in checks) or network_failed:
        raise typer.Exit(1)


async def _check_db(config: FamilyConfig, path: Path) -> None:
    async with _open_store(config, path) as store:
        await store.count_sessions()


# ------------------------------------------------------------------- editor
@app.command("ui")
def ui_command(ctx: typer.Context) -> None:
    """Open the full-screen family editor (Textual TUI)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise typer.BadParameter(
            "`ui` needs an interactive terminal; use `sub-family members add`."
        )
    with _errors():
        config, path = _load(ctx)
    from sub_family.tui.app import run_editor

    run_editor(config, config_path=path)
    console.print(f"[dim]Config:[/dim] {escape(str(path))}")


# -------------------------------------------------------------------- serve
@app.command()
def serve(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to family.yaml."),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        case_sensitive=False,
        help="DEBUG, INFO, WARNING, ERROR (default: SUB_FAMILY_LOG_LEVEL or WARNING).",
    ),
) -> None:
    """Run the MCP server over stdio (normally launched by Claude Code)."""
    cli = _ctx(ctx)
    path = (config or cli.config_path).expanduser()
    level = (log_level or os.environ.get("SUB_FAMILY_LOG_LEVEL", "WARNING")).upper()
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level not in allowed:
        raise typer.BadParameter(f"log level must be one of {', '.join(sorted(allowed))}")
    from sub_family.server import run_stdio

    run_stdio(path, log_level=level)


# ------------------------------------------------------------------- claude
@claude_app.command("snippet")
def claude_snippet(
    ctx: typer.Context,
    env: list[str] = typer.Option(
        [], "--env", "-e", help="Repeatable ENV_VAR name to pass through to the server."
    ),
) -> None:
    """Print a ready-to-paste Claude Code MCP configuration."""
    cli = _ctx(ctx)
    env_map = {name: f"${{{name}}}" for name in env} if env else {}
    snippet: dict[str, Any] = {
        "mcpServers": {
            "sub-family": {
                "command": "uvx",
                "args": ["sub-family-mcp"],
                "env": env_map,
            }
        }
    }
    console.print("[bold]1. Add the server:[/bold]")
    env_flags = " ".join(f"-e {name}=..." for name in env)
    console.print(
        f"  claude mcp add --scope user {env_flags} sub-family -- uvx sub-family-mcp".strip()
    )
    console.print("\n[bold]2. Or paste into .mcp.json:[/bold]")
    console.print(Syntax(json.dumps(snippet, indent=2), "json", theme="ansi_dark"))
    if str(cli.config_path) != str(paths.config_path()):
        console.print(
            f"[yellow]Using a custom config:[/yellow] add "
            f"[bold]--config {escape(str(cli.config_path))}[/bold] after "
            "[bold]sub-family-mcp[/bold]."
        )
    console.print("\n[dim]Then restart Claude Code and ask it to call `list_family`.[/dim]")


@claude_app.command("guide")
def claude_guide() -> None:
    """Print the delegation rules to append to ~/.claude/CLAUDE.md."""
    console.print(Syntax(CLAUDE_GUIDE, "markdown", theme="ansi_dark"))


CLAUDE_GUIDE = """\
## Sub-Family delegation

This machine has a Sub-Family MCP server with additional models. Delegate
instead of overloading the local model:

- Call `list_family` once per session. Choose by capability and trust.
- Use `ask_family_swarm` for 3+ independent subtasks; do not spawn expensive
  local subagents for work a cloud member can run in parallel.
- Keep multi-turn work in a named `session_id`; reset it when done.
- Never send API keys, .env contents, credentials, or private business logic to
  `untrusted_cloud` members. Redaction is a seatbelt, not a guarantee.
- Send images only to members with `vision`; prefer local vision members for
  screenshots that may contain secrets.
- Treat member answers as advice. Verify code and commands before applying.
- Check `family_status` when a member fails or before a large fan-out.
"""


@app.command("version")
def version_command() -> None:
    """Print the installed version."""
    console.print(f"sub-family {__version__}")


def serve_entry() -> None:
    """Entry point for the ``sub-family-mcp`` console script.

    Supports both ``sub-family-mcp`` (serve with defaults) and
    ``sub-family-mcp --config path`` without requiring the word ``serve``.
    """
    argv = sys.argv[1:]
    if argv and argv[0] not in {"serve", "--help", "-h", "--version", "-V"}:
        argv = ["serve", *argv]
    elif not argv:
        argv = ["serve"]
    app(args=argv, prog_name="sub-family-mcp")


if __name__ == "__main__":  # pragma: no cover
    app()
