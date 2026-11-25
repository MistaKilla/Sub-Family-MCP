"""Full-screen Textual editor for ``family.yaml``.

The editor keeps the immutable :class:`FamilyConfig` model as its single source
of truth: form submissions are validated into a new model, and saving revalidates
the whole document before handing it to the atomic ``save_config`` writer.

Launch it with ``sub-family ui``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar

import httpx
from pydantic import ValidationError
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TextArea,
)

from sub_family.config.loader import resolve_api_key, save_config
from sub_family.config.models import (
    KNOWN_CAPABILITIES,
    MEMBER_NAME_RE,
    FamilyConfig,
    MemberConfig,
    TrustLevel,
)
from sub_family.errors import ConfigError
from sub_family.providers.base import ProviderHealth
from sub_family.providers.openai_compat import OpenAICompatProvider

PROBE_TIMEOUT_S = 10.0
_NUMBER_LABELS = {
    "max_clones": "Clones",
    "timeout_s": "Timeout",
    "retries": "Retries",
}
_NUMERIC_FIELDS = ("max_clones", "timeout_s", "retries")


class FormError(ValueError):
    """A form value is unusable; the message is shown inline in the dialog."""


# --------------------------------------------------------------------------- helpers
def shorten(text: str, limit: int = 180) -> str:
    """Collapse whitespace and truncate text for one-line UI surfaces."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def format_validation_error(exc: ValidationError) -> str:
    """Render Pydantic errors as compact, human-readable lines."""
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        message = str(error["msg"]).removeprefix("Value error, ")
        messages.append(f"{location}: {message}")
    return " · ".join(messages)


def format_probe_result(health: ProviderHealth) -> str:
    """One-line summary of a ``/models`` probe."""
    if not health.ok:
        return f"{health.latency_ms} ms · {shorten(health.error or 'probe failed')}"
    parts = [f"{health.latency_ms} ms"]
    if health.model_available is True:
        parts.append("configured model available")
    elif health.model_available is False:
        parts.append("configured model not listed")
    else:
        parts.append("endpoint reachable")
    return " · ".join(parts)


def _parse_int(raw: Any, field: str, default: int) -> int:
    text = str(raw).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError as exc:
        raise FormError(f"{_NUMBER_LABELS[field]} must be a whole number") from exc


def _parse_float(raw: Any, field: str, default: float) -> float:
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError as exc:
        raise FormError(f"{_NUMBER_LABELS[field]} must be a number") from exc
    if value <= 0:
        raise FormError(f"{_NUMBER_LABELS[field]} must be greater than zero")
    return value


def parse_member_form(
    values: Mapping[str, Any],
    *,
    base: MemberConfig | None = None,
    existing_names: Iterable[str] = (),
) -> tuple[str, MemberConfig]:
    """Validate form values into ``(name, MemberConfig)``.

    ``base`` is the member being edited. Its dump is used as the starting payload
    so fields the form does not expose (headers, extra_body, max_tokens, ...)
    survive the round trip. ``api_key`` left blank while editing keeps the
    existing key, because the dialog never displays the secret;
    ``clear_api_key`` removes it explicitly.
    """
    name = str(values.get("name", "")).strip()
    if not MEMBER_NAME_RE.match(name):
        raise FormError(
            "Name must start with a lowercase letter and contain only lowercase "
            "letters, digits, '_' or '-' (max 40 characters)."
        )
    if base is None and name in set(existing_names):
        raise FormError(f"A member named {name!r} already exists.")

    trust_value = values.get("trust")
    trust = trust_value.value if isinstance(trust_value, TrustLevel) else str(trust_value)
    valid_trust = {level.value for level in TrustLevel}
    if trust not in valid_trust:
        raise FormError(f"Trust must be one of: {', '.join(sorted(valid_trust))}.")

    capabilities: list[str] = []
    raw_capabilities = values.get("capabilities", ())
    if isinstance(raw_capabilities, str):
        raw_capabilities = [raw_capabilities]
    for capability in raw_capabilities:
        capability = str(capability).strip().lower()
        if capability:
            capabilities.append(capability)
    for extra in str(values.get("extra_capabilities", "")).split(","):
        extra = extra.strip().lower()
        if extra:
            capabilities.append(extra)
    # Preserve the form order (known boxes first, then custom entries) while
    # removing duplicates; MemberConfig validates the format.
    capabilities = list(dict.fromkeys(capabilities))

    defaults: dict[str, Any] = {
        "max_clones": 1,
        "timeout_s": 120.0,
        "retries": 2,
    }
    parsed_numbers: dict[str, Any] = {}
    for field in _NUMERIC_FIELDS:
        fallback = getattr(base, field) if base is not None else defaults[field]
        if field == "timeout_s":
            parsed_numbers[field] = _parse_float(values.get(field), field, float(fallback))
        else:
            parsed_numbers[field] = _parse_int(values.get(field), field, int(fallback))

    api_key_value = str(values.get("api_key") or "").strip()
    if values.get("clear_api_key", False):
        api_key: Any = None
    elif api_key_value:
        api_key = api_key_value
    elif base is not None:
        api_key = base.api_key
    else:
        api_key = None

    api_key_env = str(values.get("api_key_env") or "").strip() or None
    system_prompt = str(values.get("system_prompt") or "").strip() or None

    payload: dict[str, Any] = base.model_dump() if base is not None else {}
    payload.update(
        {
            "description": str(values.get("description") or "").strip(),
            "base_url": str(values.get("base_url") or "").strip(),
            "model": str(values.get("model") or "").strip(),
            "trust": trust,
            "capabilities": capabilities,
            **parsed_numbers,
            "api_key_env": api_key_env,
            "api_key": api_key,
            "system_prompt": system_prompt,
            "enabled": bool(values.get("enabled", True)),
            "verify_tls": bool(values.get("verify_tls", True)),
        }
    )
    try:
        member = MemberConfig.model_validate(payload)
    except ValidationError as exc:
        raise FormError(format_validation_error(exc)) from exc
    return name, member


async def probe_endpoint(
    name: str,
    member: MemberConfig,
    *,
    timeout: float = PROBE_TIMEOUT_S,
) -> ProviderHealth:
    """Probe ``<base_url>/models`` using the provider's URL/auth rules."""
    api_key = resolve_api_key(member)
    probe_member = member.model_copy(update={"timeout_s": timeout})
    async with httpx.AsyncClient(
        verify=member.verify_tls,
        timeout=httpx.Timeout(timeout + 2.0),
        follow_redirects=True,
    ) as client:
        provider = OpenAICompatProvider(
            name,
            probe_member,
            api_key=api_key,
            client=client,
        )
        return await provider.health()


# --------------------------------------------------------------------------- dialogs
class ConfirmScreen(ModalScreen[bool]):
    """Small yes/no dialog used for delete and discard-changes confirmations."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No", show=False),
        Binding("y", "confirm", "Yes", show=False),
    ]

    def __init__(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__()
        self.message = message
        self.dialog_title = title
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static(self.dialog_title, id="confirm-title")
            yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(self.confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm")


class MemberFormScreen(ModalScreen[dict[str, Any] | None]):
    """Add/edit dialog. Dismisses with ``{"name": str, "member": MemberConfig}``."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "submit", "Save member", show=False),
    ]

    def __init__(
        self,
        *,
        member: MemberConfig | None = None,
        name: str | None = None,
        existing_names: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.member = member
        self.member_name = name or ""
        self.existing_names = set(existing_names)
        self.editing = member is not None

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        heading = f"Edit {self.member_name}" if self.editing else "Add member"
        capabilities = set(self.member.capabilities) if self.member else {"chat"}
        extra_capabilities = sorted(capabilities - set(KNOWN_CAPABILITIES))
        with Container(id="form-dialog"):
            yield Static(heading, id="form-title")
            with VerticalScroll(id="form-body"):
                yield Label("Name", classes="field-label")
                yield Input(
                    value=self.member_name,
                    placeholder="lilbro",
                    id="name",
                    disabled=self.editing,
                )
                yield Static(
                    "Lowercase, starts with a letter. Renaming is not supported."
                    if self.editing
                    else "Lowercase letters, digits, '_' or '-', starting with a letter.",
                    classes="hint",
                )

                yield Label("Description", classes="field-label")
                yield Input(
                    value=self.member.description if self.member else "",
                    placeholder="What is this endpoint good at?",
                    id="description",
                )

                yield Label("Base URL", classes="field-label")
                yield Input(
                    value=self.member.base_url if self.member else "",
                    placeholder="http://127.0.0.1:11434",
                    id="base_url",
                )

                yield Label("Model", classes="field-label")
                yield Input(
                    value=self.member.model if self.member else "",
                    placeholder="qwen2.5-coder:32b",
                    id="model",
                )

                yield Label("Trust level", classes="field-label")
                trust_value = (
                    self.member.trust.value if self.member else TrustLevel.SAFE_LOCAL.value
                )
                yield Select(
                    [(level.value, level.value) for level in TrustLevel],
                    value=trust_value,
                    allow_blank=False,
                    id="trust",
                )

                yield Label("Capabilities", classes="field-label")
                with Container(classes="cap-grid"):
                    for capability in KNOWN_CAPABILITIES:
                        yield Checkbox(
                            capability,
                            value=capability in capabilities,
                            id=f"cap-{capability}",
                        )
                yield Label("Extra capabilities", classes="field-label")
                yield Input(
                    value=", ".join(extra_capabilities),
                    placeholder="comma-separated, e.g. json_mode",
                    id="extra_capabilities",
                )

                yield Label("Max clones", classes="field-label")
                yield Input(
                    value=str(self.member.max_clones) if self.member else "1",
                    type="integer",
                    id="max_clones",
                )

                yield Label("Timeout (seconds)", classes="field-label")
                yield Input(
                    value=str(self.member.timeout_s) if self.member else "120",
                    type="number",
                    id="timeout_s",
                )

                yield Label("Retries (advanced)", classes="field-label")
                yield Input(
                    value=str(self.member.retries) if self.member else "2",
                    type="integer",
                    id="retries",
                )

                yield Label("API key env var (recommended)", classes="field-label")
                yield Input(
                    value=(self.member.api_key_env or "") if self.member else "",
                    placeholder="DEEPSEEK_API_KEY",
                    id="api_key_env",
                )

                yield Label("API key (optional, plaintext)", classes="field-label")
                yield Input(
                    value="",
                    placeholder=(
                        "leave blank to keep the current key"
                        if self.editing
                        else "stored in family.yaml; prefer api_key_env"
                    ),
                    password=True,
                    id="api_key",
                )
                yield Static(
                    "Warning: a literal key is saved unencrypted in family.yaml. "
                    "Prefer an environment variable.",
                    classes="hint warning",
                )
                if self.editing:
                    with Horizontal(classes="switch-row"):
                        yield Label("Clear stored API key")
                        yield Switch(value=False, id="clear_api_key")

                yield Label("System prompt", classes="field-label")
                yield TextArea(
                    (self.member.system_prompt or "") if self.member else "",
                    id="system_prompt",
                )

                with Horizontal(classes="switch-row"):
                    yield Label("Enabled")
                    yield Switch(
                        value=self.member.enabled if self.member else True,
                        id="enabled",
                    )
                with Horizontal(classes="switch-row"):
                    yield Label("Verify TLS (advanced)")
                    yield Switch(
                        value=self.member.verify_tls if self.member else True,
                        id="verify_tls",
                    )

            yield Static("", id="form-error")
            with Horizontal(id="form-actions"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Save member", id="form-save", variant="primary")

    def on_mount(self) -> None:
        target = "#description" if self.editing else "#name"
        self.query_one(target, Input).focus()

    # ---------------------------------------------------------------- actions
    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        values = {
            "name": self.query_one("#name", Input).value,
            "description": self.query_one("#description", Input).value,
            "base_url": self.query_one("#base_url", Input).value,
            "model": self.query_one("#model", Input).value,
            "trust": self.query_one("#trust", Select).value,
            "capabilities": [
                capability
                for capability in KNOWN_CAPABILITIES
                if self.query_one(f"#cap-{capability}", Checkbox).value
            ],
            "extra_capabilities": self.query_one("#extra_capabilities", Input).value,
            "max_clones": self.query_one("#max_clones", Input).value,
            "timeout_s": self.query_one("#timeout_s", Input).value,
            "retries": self.query_one("#retries", Input).value,
            "api_key_env": self.query_one("#api_key_env", Input).value,
            "api_key": self.query_one("#api_key", Input).value,
            "clear_api_key": (
                self.query_one("#clear_api_key", Switch).value if self.editing else False
            ),
            "system_prompt": self.query_one("#system_prompt", TextArea).text,
            "enabled": self.query_one("#enabled", Switch).value,
            "verify_tls": self.query_one("#verify_tls", Switch).value,
        }
        try:
            name, member = parse_member_form(
                values,
                base=self.member,
                existing_names=self.existing_names,
            )
        except FormError as exc:
            self.query_one("#form-error", Static).update(Text(str(exc), style="bold red"))
            return
        self.dismiss({"name": name, "member": member})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "form-save":
            self.action_submit()
        elif event.button.id == "form-cancel":
            self.action_cancel()


# --------------------------------------------------------------------------- app
class FamilyEditor(App[None]):
    """Keyboard-first editor for the model family."""

    TITLE = "Sub-Family"
    SUB_TITLE = "model family settings"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { background: $surface; }
    #members-title { height: 1; margin: 1 2 0 2; color: $text-muted; }
    #toolbar { height: 3; padding: 0 1; }
    #toolbar Button { margin-right: 1; min-width: 11; }
    #members { height: 1fr; margin: 0 1; border: round $primary; }
    #empty { height: auto; margin: 1 2 0 2; color: $text-muted; }
    #status { height: 3; padding: 1 2 0 2; color: $text-muted; }

    MemberFormScreen { align: center middle; }
    #form-dialog {
        width: 90%;
        max-width: 96;
        height: 90%;
        background: $panel;
        border: round $accent;
        padding: 1 2;
    }
    #form-title { text-style: bold; color: $accent; height: 1; margin-bottom: 1; }
    #form-body { height: 1fr; padding-right: 1; }
    #form-error { height: auto; min-height: 1; color: $error; text-style: bold; padding-top: 1; }
    #form-actions { height: 3; align: right middle; }
    #form-actions Button { margin-left: 2; min-width: 14; }
    #system_prompt { height: 6; }
    .field-label { margin-top: 1; text-style: bold; }
    .hint { height: auto; color: $text-muted; }
    .hint.warning { color: $warning; }
    .cap-grid { layout: grid; grid-size: 4; grid-gutter: 0 1; height: auto; }
    .switch-row { height: 3; align-vertical: middle; }
    .switch-row Label { width: auto; margin-right: 2; }
    .switch-row Switch { width: auto; }

    ConfirmScreen { align: center middle; }
    #confirm-dialog {
        width: 64;
        max-width: 92%;
        height: auto;
        background: $panel;
        border: round $error;
        padding: 1 2;
    }
    #confirm-title { text-style: bold; color: $warning; }
    #confirm-message { margin: 1 0; height: auto; }
    #confirm-actions { height: 3; align: right middle; }
    #confirm-actions Button { margin-left: 2; min-width: 12; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("a", "add_member", "Add", show=True),
        Binding("e", "edit_member", "Edit", show=True),
        Binding("d", "delete_member", "Delete", show=True),
        Binding("t", "test_member", "Test", show=True),
        Binding("s", "save", "Save", show=True),
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("q", "request_quit", "Quit", show=True),
        Binding("enter", "edit_member", "Edit", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, config: FamilyConfig, config_path: Path) -> None:
        super().__init__()
        self.config = config
        self.config_path = Path(config_path).expanduser()
        self.dirty = False
        self._notice = "Select a member with the arrow keys; press e to edit or a to add."

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="members-title")
        with Horizontal(id="toolbar"):
            yield Button("Add  a", id="btn-add", variant="primary")
            yield Button("Edit  e", id="btn-edit")
            yield Button("Delete  d", id="btn-delete")
            yield Button("Test  t", id="btn-test")
            yield Button("Save  s", id="btn-save")
        yield DataTable(id="members", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty")
        yield Static("", id="status")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        table = self.query_one("#members", DataTable)
        table.add_columns("Name", "Model", "Trust", "Capabilities", "Clones", "State")
        self._refresh_table()
        self._update_status()
        table.focus()

    # ------------------------------------------------------------- table/state
    def _modal_open(self) -> bool:
        return len(self.screen_stack) > 1

    def _selected_name(self) -> str | None:
        table = self.query_one("#members", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        name = row_key.value
        return str(name) if name in self.config.members else None

    def _selected_member(self) -> tuple[str, MemberConfig] | None:
        name = self._selected_name()
        if name is None:
            return None
        return name, self.config.members[name]

    def _refresh_table(self, *, select: str | None = None) -> None:
        table = self.query_one("#members", DataTable)
        previous = select or self._selected_name()
        table.clear()
        for name, member in self.config.members.items():
            trust_style = {
                TrustLevel.SAFE_LOCAL: "green",
                TrustLevel.PRIVATE_NETWORK: "cyan",
                TrustLevel.UNTRUSTED_CLOUD: "yellow",
            }[member.trust]
            capabilities = ", ".join(member.capabilities) or "—"
            table.add_row(
                Text(name, style="bold"),
                member.model,
                Text(member.trust.value, style=trust_style),
                capabilities,
                str(member.max_clones),
                Text("enabled", style="green") if member.enabled else Text("disabled", style="dim"),
                key=name,
            )

        member_count = len(self.config.members)
        enabled_count = len(self.config.enabled_members())
        plural = "" if member_count == 1 else "s"
        title = f"{member_count} member{plural} · {enabled_count} enabled"
        self.query_one("#members-title", Static).update(Text(title, style="dim"))

        empty = self.query_one("#empty", Static)
        empty.display = member_count == 0
        if member_count == 0:
            empty.update(
                Text.from_markup(
                    "No members configured yet. Press [bold]a[/bold] "
                    "or click Add to register your first model endpoint."
                )
            )

        if table.row_count:
            names = list(self.config.members)
            target = previous if previous in names else names[0]
            table.move_cursor(row=names.index(target))

        has_selection = member_count > 0
        for button_id in ("#btn-edit", "#btn-delete", "#btn-test"):
            self.query_one(button_id, Button).disabled = not has_selection

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if self.dirty:
            marker = Text("● unsaved changes", style="bold yellow")
        else:
            marker = Text("● saved", style="green")
        text = Text.assemble(("Config: ", "bold"), (str(self.config_path), "dim"), "    ", marker)
        text.append("\n")
        text.append(self._notice, style="dim")
        status.update(text)

    def _replace_config(self, data: dict[str, Any]) -> FamilyConfig | None:
        try:
            return FamilyConfig.model_validate(data)
        except ValidationError as exc:
            self.notify(
                format_validation_error(exc),
                title="Invalid configuration",
                severity="error",
            )
            return None

    # ---------------------------------------------------------------- actions
    def action_refresh(self) -> None:
        if self._modal_open():
            return
        self._refresh_table()
        self._update_status()
        self.notify("Table refreshed", timeout=2)

    def action_add_member(self) -> None:
        if self._modal_open():
            return
        self._open_form(member=None, name=None)

    def action_edit_member(self) -> None:
        if self._modal_open():
            return
        selected = self._selected_member()
        if selected is None:
            self.notify("Select a member first.", severity="warning", timeout=3)
            return
        name, member = selected
        self._open_form(member=member, name=name)

    def action_delete_member(self) -> None:
        if self._modal_open():
            return
        selected = self._selected_member()
        if selected is None:
            self.notify("Select a member first.", severity="warning", timeout=3)
            return
        name = selected[0]
        self.push_screen(
            ConfirmScreen(
                f"Delete member '{name}' from the family?\n\n"
                "The entry is removed from family.yaml when you save. "
                "Stored sessions are not deleted.",
                title="Delete member?",
                confirm_label="Delete",
            ),
            lambda confirmed: self._delete_member(name, confirmed),
        )

    def action_test_member(self) -> None:
        if self._modal_open():
            return
        selected = self._selected_member()
        if selected is None:
            self.notify("Select a member first.", severity="warning", timeout=3)
            return
        name, member = selected
        self._notice = f"Testing {name} · {member.base_url} …"
        self._update_status()
        self._run_probe(name, member)

    def action_save(self) -> None:
        if self._modal_open():
            return
        try:
            validated = FamilyConfig.model_validate(self.config.model_dump())
            path = save_config(validated, self.config_path)
        except (ValidationError, ConfigError) as exc:
            message = format_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
            self.notify(message, title="Could not save", severity="error", timeout=8)
            return
        self.config = validated
        self.dirty = False
        self._notice = f"Saved {path} (a .bak backup of the previous file was kept)."
        self._refresh_table()
        self._update_status()
        self.notify(f"Saved to {path}", title="Saved", timeout=4)

    def action_request_quit(self) -> None:
        if self._modal_open():
            return
        if not self.dirty:
            self.exit()
            return
        self.push_screen(
            ConfirmScreen(
                "You have unsaved changes. Quit and discard them?",
                title="Discard unsaved changes?",
                confirm_label="Discard",
            ),
            self._confirm_quit,
        )

    def action_quit(self) -> None:
        """Ctrl+Q routes through the dirty check instead of quitting directly."""
        self.action_request_quit()

    # ------------------------------------------------------------- form result
    def _open_form(self, *, member: MemberConfig | None, name: str | None) -> None:
        screen = MemberFormScreen(
            member=member,
            name=name,
            existing_names=set(self.config.members),
        )
        self.push_screen(
            screen,
            lambda result: self._apply_form_result(result, editing=name is not None),
        )

    def _apply_form_result(self, result: dict[str, Any] | None, *, editing: bool) -> None:
        if not result:
            return
        name = str(result["name"])
        member: MemberConfig = result["member"]
        data = self.config.model_dump()
        data.setdefault("members", {})[name] = member.model_dump()
        new_config = self._replace_config(data)
        if new_config is None:
            return
        self.config = new_config
        self.dirty = True
        self._notice = f"{'Updated' if editing else 'Added'} {name} · unsaved changes"
        self._refresh_table(select=name)
        self._update_status()
        self.query_one("#members", DataTable).focus()
        self.notify(
            f"{'Updated' if editing else 'Added'} member {name}",
            title="Unsaved changes",
            timeout=3,
        )

    def _delete_member(self, name: str, confirmed: bool) -> None:
        if not confirmed:
            return
        data = self.config.model_dump()
        if name not in data.get("members", {}):
            return
        data["members"].pop(name)
        new_config = self._replace_config(data)
        if new_config is None:
            return
        self.config = new_config
        self.dirty = True
        self._notice = f"Deleted {name} · unsaved changes"
        self._refresh_table()
        self._update_status()
        self.query_one("#members", DataTable).focus()
        self.notify(f"Deleted member {name}", title="Unsaved changes", timeout=3)

    def _confirm_quit(self, confirmed: bool) -> None:
        if confirmed:
            self.exit()

    # ------------------------------------------------------------------ probe
    @work(exclusive=True, group="probe")
    async def _run_probe(self, name: str, member: MemberConfig) -> None:
        try:
            health = await probe_endpoint(name, member)
        except ConfigError as exc:
            self._finish_probe(
                name,
                f"missing credentials: {shorten(str(exc))}",
                severity="error",
            )
            return
        except Exception as exc:  # broad by design: a probe must never crash the UI
            self._finish_probe(name, f"probe failed: {shorten(str(exc))}", severity="error")
            return
        severity = "information"
        if not health.ok:
            severity = "error"
        elif health.model_available is False:
            severity = "warning"
        self._finish_probe(name, format_probe_result(health), severity=severity)

    def _finish_probe(self, name: str, detail: str, *, severity: str) -> None:
        self._notice = f"Test {name}: {detail}"
        self._update_status()
        self.notify(detail, title=f"Connectivity · {name}", severity=severity, timeout=8)

    # ---------------------------------------------------------------- buttons
    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "btn-add": self.action_add_member,
            "btn-edit": self.action_edit_member,
            "btn-delete": self.action_delete_member,
            "btn-test": self.action_test_member,
            "btn-save": self.action_save,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            event.stop()
            handler()

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit_member()


def run_editor(config: FamilyConfig, config_path: Path) -> None:
    """Run the full-screen family editor (blocking until the user quits)."""
    FamilyEditor(config, Path(config_path)).run()
