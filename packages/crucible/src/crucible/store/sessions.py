"""SqliteSessionStore: the concrete SessionStore over a single SQLite file.

stdlib sqlite3 wrapped in ``asyncio.to_thread`` — operations are sub-millisecond
and there is one writer (the engine), so a driver dependency isn't warranted.
Sync ``*_sync`` methods exist for the cleanup CLI.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from crucible.ports.chat.directory import AgentInfo
from crucible.store.base import (
    FormRecord,
    InteractionRecord,
    SessionRecord,
    derive_runtime_session_id,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  agent TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,   -- thread root post id, or channel id (dm/channel)
  kind TEXT NOT NULL,              -- 'thread' | 'dm' | 'channel'
  runtime_session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,        -- ISO8601 UTC
  last_active TEXT NOT NULL,
  last_user_id TEXT NOT NULL DEFAULT '',  -- who last triggered a turn here
  UNIQUE (agent, conversation_id)
);

-- agent registry snapshot (synced from profiles) + processed-post dedup:
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY, role TEXT, description TEXT,
  user_id TEXT, username TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS processed_posts (
  agent TEXT NOT NULL, post_id TEXT NOT NULL, PRIMARY KEY (agent, post_id)
);

-- widgets/forms awaiting a click:
CREATE TABLE IF NOT EXISTS pending_interactions (
  interaction_id TEXT PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  agent TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_forms (
  token TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

_INTERACTION_COLUMNS = (
    "interaction_id, token, agent, channel_id, conversation_id, kind, created_at"
)
_FORM_COLUMNS = "token, agent, channel_id, conversation_id, kind, spec, created_at"

# Order matches SessionRecord's fields (SessionRecord(*row)); last_user_id last.
_COLUMNS = (
    "agent, channel_id, conversation_id, kind, runtime_session_id, "
    "created_at, last_active, last_user_id"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqliteSessionStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        # One long-lived connection; to_thread hops threads, so disable the
        # same-thread check and serialize with our own lock instead.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns absent from DBs created by older versions. Guarded so a
        second run is a no-op (CREATE TABLE IF NOT EXISTS won't ALTER)."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "last_user_id" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN last_user_id TEXT NOT NULL DEFAULT ''"
            )

    # -- async facade (SessionStore port) ------------------------------------

    async def get_or_create(
        self, agent: str, channel_id: str, conversation_id: str, kind: str,
        user_id: str = "",
    ) -> tuple[SessionRecord, bool]:
        return await asyncio.to_thread(
            self.get_or_create_sync, agent, channel_id, conversation_id, kind, user_id
        )

    async def touch(self, agent: str, conversation_id: str, user_id: str = "") -> None:
        await asyncio.to_thread(self.touch_sync, agent, conversation_id, user_id)

    async def list(self, agent: str | None = None) -> list[SessionRecord]:
        return await asyncio.to_thread(self.list_sync, agent)

    async def delete(self, agent: str, conversation_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self.delete_sync, agent, conversation_id)

    async def get_by_runtime_session(self, runtime_session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self.get_by_runtime_session_sync, runtime_session_id)

    async def mark_processed(self, agent: str, post_id: str) -> bool:
        return await asyncio.to_thread(self.mark_processed_sync, agent, post_id)

    async def create_interaction(self, record: InteractionRecord) -> None:
        await asyncio.to_thread(self.create_interaction_sync, record)

    async def take_interaction(self, token: str) -> InteractionRecord | None:
        return await asyncio.to_thread(self.take_interaction_sync, token)

    async def create_form(self, record: FormRecord) -> None:
        await asyncio.to_thread(self.create_form_sync, record)

    async def get_form(self, token: str) -> FormRecord | None:
        return await asyncio.to_thread(self.get_form_sync, token)

    async def delete_form(self, token: str) -> None:
        await asyncio.to_thread(self.delete_form_sync, token)

    async def upsert_agent(self, info: AgentInfo) -> None:
        await asyncio.to_thread(self.upsert_agent_sync, info)

    async def list_agents(self) -> list[AgentInfo]:
        return await asyncio.to_thread(self.list_agents_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self.close_sync)

    # -- sync core (also used by the cleanup CLI) -----------------------------

    def get_or_create_sync(
        self, agent: str, channel_id: str, conversation_id: str, kind: str,
        user_id: str = "",
    ) -> tuple[SessionRecord, bool]:
        now = _now_iso()
        runtime_session_id = derive_runtime_session_id(agent, conversation_id)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO sessions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (agent, conversation_id) DO NOTHING",
                (agent, channel_id, conversation_id, kind, runtime_session_id, now, now, user_id),
            )
            created = cursor.rowcount == 1
            # Refresh the last user on an existing row too, so a tool called
            # mid-turn addresses THIS turn's user, not the one who opened the
            # conversation. (ON CONFLICT DO NOTHING keeps the `created` flag clean.)
            if not created and user_id:
                self._conn.execute(
                    "UPDATE sessions SET last_user_id = ? "
                    "WHERE agent = ? AND conversation_id = ?",
                    (user_id, agent, conversation_id),
                )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE agent = ? AND conversation_id = ?",
                (agent, conversation_id),
            ).fetchone()
        return SessionRecord(*row), created

    def touch_sync(self, agent: str, conversation_id: str, user_id: str = "") -> None:
        with self._lock:
            if user_id:
                self._conn.execute(
                    "UPDATE sessions SET last_active = ?, last_user_id = ? "
                    "WHERE agent = ? AND conversation_id = ?",
                    (_now_iso(), user_id, agent, conversation_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET last_active = ? "
                    "WHERE agent = ? AND conversation_id = ?",
                    (_now_iso(), agent, conversation_id),
                )
            self._conn.commit()

    def list_sync(self, agent: str | None = None) -> list[SessionRecord]:
        query = f"SELECT {_COLUMNS} FROM sessions"
        params: tuple = ()
        if agent is not None:
            query += " WHERE agent = ?"
            params = (agent,)
        query += " ORDER BY last_active DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [SessionRecord(*row) for row in rows]

    def delete_sync(self, agent: str, conversation_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE agent = ? AND conversation_id = ?",
                (agent, conversation_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM sessions WHERE agent = ? AND conversation_id = ?",
                (agent, conversation_id),
            )
            self._conn.commit()
        return SessionRecord(*row)

    def get_by_runtime_session_sync(self, runtime_session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE runtime_session_id = ?",
                (runtime_session_id,),
            ).fetchone()
        return SessionRecord(*row) if row else None

    def create_interaction_sync(self, r: InteractionRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO pending_interactions ({_INTERACTION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r.interaction_id, r.token, r.agent, r.channel_id, r.conversation_id, r.kind, r.created_at),
            )
            self._conn.commit()

    def take_interaction_sync(self, token: str) -> InteractionRecord | None:
        # Fetch + delete atomically under the lock — one-shot, so a replayed
        # click can't fire the interaction twice.
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_INTERACTION_COLUMNS} FROM pending_interactions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM pending_interactions WHERE token = ?", (token,))
            self._conn.commit()
        return InteractionRecord(*row)

    def create_form_sync(self, r: FormRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO pending_forms ({_FORM_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r.token, r.agent, r.channel_id, r.conversation_id, r.kind, r.spec, r.created_at),
            )
            self._conn.commit()

    def get_form_sync(self, token: str) -> FormRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_FORM_COLUMNS} FROM pending_forms WHERE token = ?", (token,)
            ).fetchone()
        return FormRecord(*row) if row else None

    def delete_form_sync(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_forms WHERE token = ?", (token,))
            self._conn.commit()

    def mark_processed_sync(self, agent: str, post_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO processed_posts (agent, post_id) VALUES (?, ?)",
                (agent, post_id),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def upsert_agent_sync(self, info: AgentInfo) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO agents (name, role, description, username, user_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET role = excluded.role, "
                "description = excluded.description, username = excluded.username, "
                "user_id = excluded.user_id, updated_at = excluded.updated_at",
                (info.name, info.role, info.description, info.username, info.user_id, _now_iso()),
            )
            self._conn.commit()

    def list_agents_sync(self) -> list[AgentInfo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, role, description, username, user_id FROM agents ORDER BY name"
            ).fetchall()
        return [AgentInfo(*row) for row in rows]

    def close_sync(self) -> None:
        with self._lock:
            self._conn.close()
