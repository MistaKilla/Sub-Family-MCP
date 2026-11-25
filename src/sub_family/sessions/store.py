"""SQLite-backed history for virtual family sessions.

Sessions are scoped by ``(member, session_id)`` so that several "clones" of the
same model can hold independent conversations. History survives MCP server
restarts, which matters for Claude Code: the server process is restarted far
more often than the work it supports.

The store is synchronous ``sqlite3`` wrapped in ``asyncio.to_thread``. At the
scale of one developer's delegation traffic this is simpler and more robust
than a second async database dependency, and SQLite's WAL mode handles the
occasional parallel swarm fine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sub_family.config.models import SessionsConfig
from sub_family.errors import SessionError

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
PREVIEW_CHARS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    member        TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_preview  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (member, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    member            TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    content           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    FOREIGN KEY (member, session_id) REFERENCES sessions(member, session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(member, session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated
    ON sessions(updated_at);
"""


def validate_session_id(value: str) -> str:
    """Validate a user-supplied session id.

    Ids may contain letters, digits, dots, underscores, hyphens, and colons
    (used by swarm-generated names). They are not filesystem paths.
    """
    if not isinstance(value, str):
        raise SessionError("session_id must be a string")
    cleaned = value.strip()
    if not SESSION_ID_RE.match(cleaned):
        raise SessionError(
            "invalid session_id: use 1-64 characters from A-Z, a-z, 0-9, '.', '_', ':' or '-' "
            "and start with a letter or digit"
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """One persisted message."""

    role: str
    content: Any
    created_at: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def as_chat_message(self) -> dict[str, Any]:
        """Convert to the OpenAI Chat Completions message shape."""
        return {"role": self.role, "content": self.content}

    def preview(self, limit: int = PREVIEW_CHARS) -> str:
        """A single-line preview for terminal output."""
        return preview_content(self.content, limit)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Metadata about a stored session."""

    member: str
    session_id: str
    created_at: str
    updated_at: str
    message_count: int
    last_preview: str


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Outcome of storing one user/assistant exchange."""

    message_count: int
    trimmed: int


def preview_content(content: Any, limit: int = PREVIEW_CHARS) -> str:
    """Render arbitrary message content as a compact one-line preview."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    parts.append("[image]")
        text = " ".join(parts)
    else:
        text = str(content)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class SessionStore:
    """Async facade over a single SQLite database file."""

    def __init__(self, path: Path, config: SessionsConfig) -> None:
        self.path = path
        self.config = config
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ lifecycle
    async def open(self) -> None:
        """Create the parent directory, open the DB, and apply the schema."""
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _chmod(self.path.parent, 0o700)
            except OSError as exc:
                raise SessionError(
                    f"cannot create session directory {self.path.parent}: {exc}"
                ) from exc
            try:
                connection = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
            except sqlite3.Error as exc:
                raise SessionError(f"cannot open session database {self.path}: {exc}") from exc
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise SessionError(
                        f"session database {self.path} has schema version {version}, "
                        "which this build does not understand"
                    )
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            except (sqlite3.Error, SessionError) as exc:
                connection.close()
                if isinstance(exc, SessionError):
                    raise
                raise SessionError(f"cannot initialise session database: {exc}") from exc
            self._connection = connection
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                if candidate.exists():
                    _chmod(candidate, 0o600)

    async def close(self) -> None:
        """Close the database connection (idempotent)."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    async def __aenter__(self) -> SessionStore:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SessionError("session store is not open")
        return self._connection

    # --------------------------------------------------------------- writes
    async def append_exchange(
        self,
        member: str,
        session_id: str,
        user_content: Any,
        assistant_content: str,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> AppendResult:
        """Persist a user message and the assistant reply, trimming old turns."""
        return await asyncio.to_thread(
            self._append_sync,
            member,
            session_id,
            user_content,
            assistant_content,
            prompt_tokens,
            completion_tokens,
        )

    def _append_sync(
        self,
        member: str,
        session_id: str,
        user_content: Any,
        assistant_content: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> AppendResult:
        with self._lock:
            connection = self._conn()
            now = _utcnow()
            preview = preview_content(assistant_content)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO sessions (member, session_id, created_at, updated_at,
                                          message_count, last_preview)
                    VALUES (?, ?, ?, ?, 0, '')
                    ON CONFLICT(member, session_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        last_preview = excluded.last_preview
                    """,
                    (member, session_id, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO messages (member, session_id, role, content, created_at,
                                          prompt_tokens, completion_tokens)
                    VALUES (?, ?, 'user', ?, ?, ?, NULL)
                    """,
                    (member, session_id, _encode(user_content), now, prompt_tokens),
                )
                connection.execute(
                    """
                    INSERT INTO messages (member, session_id, role, content, created_at,
                                          prompt_tokens, completion_tokens)
                    VALUES (?, ?, 'assistant', ?, ?, NULL, ?)
                    """,
                    (member, session_id, _encode(assistant_content), now, completion_tokens),
                )
                rows = connection.execute(
                    """
                    SELECT id, role, length(content) AS size
                    FROM messages
                    WHERE member = ? AND session_id = ?
                    ORDER BY id ASC
                    """,
                    (member, session_id),
                ).fetchall()
                keep_from, kept = self._compute_window(rows)
                removed_ids = [row["id"] for row in rows[:keep_from]]
                if removed_ids:
                    placeholders = ",".join("?" for _ in removed_ids)
                    connection.execute(
                        f"DELETE FROM messages WHERE id IN ({placeholders})",
                        removed_ids,
                    )
                connection.execute(
                    """
                    UPDATE sessions
                    SET message_count = ?, updated_at = ?, last_preview = ?
                    WHERE member = ? AND session_id = ?
                    """,
                    (kept, now, preview, member, session_id),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise SessionError(f"cannot write session history: {exc}") from exc
            return AppendResult(message_count=kept, trimmed=len(removed_ids))

    def _compute_window(self, rows: Sequence[sqlite3.Row]) -> tuple[int, int]:
        """Return the index of the first kept row and the number of kept rows."""
        if not rows:
            return 0, 0
        keep_from = len(rows)
        total_chars = 0
        for offset, row in enumerate(reversed(rows)):
            if offset >= self.config.max_messages:
                break
            size = int(row["size"] or 0)
            if total_chars + size > self.config.max_chars and keep_from != len(rows):
                break
            total_chars += size
            keep_from = len(rows) - offset - 1
        if keep_from >= len(rows):
            keep_from = len(rows) - 1
        # Never start the visible history with an orphaned assistant message.
        while keep_from < len(rows) - 1 and rows[keep_from]["role"] != "user":
            keep_from += 1
        return keep_from, len(rows) - keep_from

    async def reset_session(self, member: str, session_id: str) -> int:
        """Delete one session and return the number of messages removed."""
        return await asyncio.to_thread(self._reset_sync, member, session_id, False)

    async def reset_member_sessions(self, member: str) -> int:
        """Delete every session of one member and return removed message count."""
        return await asyncio.to_thread(self._reset_sync, member, None, True)

    def _reset_sync(self, member: str, session_id: str | None, all_for_member: bool) -> int:
        with self._lock:
            connection = self._conn()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if all_for_member:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE member = ?", (member,)
                    ).fetchone()[0]
                    connection.execute("DELETE FROM sessions WHERE member = ?", (member,))
                else:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE member = ? AND session_id = ?",
                        (member, session_id),
                    ).fetchone()[0]
                    connection.execute(
                        "DELETE FROM sessions WHERE member = ? AND session_id = ?",
                        (member, session_id),
                    )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise SessionError(f"cannot reset session: {exc}") from exc
            return int(count)

    async def prune(self, ttl_days: int) -> int:
        """Delete sessions not updated within ``ttl_days``; return session count."""
        return await asyncio.to_thread(self._prune_sync, ttl_days)

    def _prune_sync(self, ttl_days: int) -> int:
        if ttl_days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
        with self._lock:
            connection = self._conn()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise SessionError(f"cannot prune sessions: {exc}") from exc
            return int(cursor.rowcount or 0)

    # ---------------------------------------------------------------- reads
    async def get_messages(self, member: str, session_id: str) -> list[StoredMessage]:
        """Return the full stored history in chronological order."""
        return await asyncio.to_thread(self._get_messages_sync, member, session_id, None)

    async def get_history(
        self, member: str, session_id: str, limit: int = 50
    ) -> list[StoredMessage]:
        """Return the last ``limit`` messages in chronological order."""
        return await asyncio.to_thread(self._get_messages_sync, member, session_id, limit)

    def _get_messages_sync(
        self, member: str, session_id: str, limit: int | None
    ) -> list[StoredMessage]:
        with self._lock:
            connection = self._conn()
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT role, content, created_at, prompt_tokens, completion_tokens
                    FROM messages WHERE member = ? AND session_id = ?
                    ORDER BY id ASC
                    """,
                    (member, session_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT role, content, created_at, prompt_tokens, completion_tokens
                    FROM messages WHERE member = ? AND session_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (member, session_id, max(1, limit)),
                ).fetchall()
                rows = list(reversed(rows))
            return [
                StoredMessage(
                    role=row["role"],
                    content=_decode(row["content"]),
                    created_at=row["created_at"],
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                )
                for row in rows
            ]

    async def list_sessions(self, member: str | None = None) -> list[SessionInfo]:
        """List sessions, newest first."""
        return await asyncio.to_thread(self._list_sessions_sync, member)

    def _list_sessions_sync(self, member: str | None) -> list[SessionInfo]:
        with self._lock:
            connection = self._conn()
            if member:
                rows = connection.execute(
                    """
                    SELECT * FROM sessions WHERE member = ?
                    ORDER BY updated_at DESC LIMIT 200
                    """,
                    (member,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200"
                ).fetchall()
            return [
                SessionInfo(
                    member=row["member"],
                    session_id=row["session_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    message_count=row["message_count"],
                    last_preview=row["last_preview"],
                )
                for row in rows
            ]

    async def get_session(self, member: str, session_id: str) -> SessionInfo | None:
        """Return metadata for one session, or ``None``."""
        return await asyncio.to_thread(self._get_session_sync, member, session_id)

    def _get_session_sync(self, member: str, session_id: str) -> SessionInfo | None:
        with self._lock:
            row = (
                self._conn()
                .execute(
                    "SELECT * FROM sessions WHERE member = ? AND session_id = ?",
                    (member, session_id),
                )
                .fetchone()
            )
            if row is None:
                return None
            return SessionInfo(
                member=row["member"],
                session_id=row["session_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=row["message_count"],
                last_preview=row["last_preview"],
            )

    async def count_sessions(self) -> int:
        """Total number of stored sessions."""
        return await asyncio.to_thread(self._count_sessions_sync)

    def _count_sessions_sync(self) -> int:
        with self._lock:
            return int(self._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _encode(content: Any) -> str:
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _chmod(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        path.chmod(mode)
