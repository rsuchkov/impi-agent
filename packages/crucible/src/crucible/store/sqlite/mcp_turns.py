"""The turns MCP clients' calls opened — the McpTurnStore facet of the SQLite
store: one row per turn, read by its id (a task's) or as its conversation's
newest."""

import asyncio
from dataclasses import fields

from crucible.ports.mcp_turns import TURN_ENDED, TURN_FAILED, McpTurnRecord
from crucible.store.sqlite.base import SqliteBase

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_turns (
  turn_id          TEXT PRIMARY KEY,
  conversation     TEXT NOT NULL,
  handle           TEXT NOT NULL,
  subject          TEXT NOT NULL,
  client           TEXT NOT NULL,
  agent            TEXT NOT NULL DEFAULT '',
  replica          TEXT NOT NULL DEFAULT '',
  state            TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  step             TEXT NOT NULL DEFAULT '',
  answer           TEXT NOT NULL DEFAULT '',
  is_error         INTEGER NOT NULL DEFAULT 0,
  request_digest   TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mcp_turns_conversation ON mcp_turns (conversation, started_at);
CREATE INDEX IF NOT EXISTS idx_mcp_turns_request ON mcp_turns (subject, client, request_digest, started_at);
CREATE INDEX IF NOT EXISTS idx_mcp_turns_expires ON mcp_turns (expires_at);
"""

_FIELDS = tuple(f.name for f in fields(McpTurnRecord))
_COLUMNS = ", ".join(_FIELDS)
# What a save rewrites on a row that exists: everything but the cancel mark,
# which another replica may have set meanwhile.
_REWRITTEN = tuple(name for name in _FIELDS if name not in ("turn_id", "cancel_requested"))


def _record(row: tuple) -> McpTurnRecord:
    values = dict(zip(_FIELDS, row, strict=True))
    values["is_error"] = bool(values["is_error"])
    values["cancel_requested"] = bool(values["cancel_requested"])
    return McpTurnRecord(**values)


def _not_ended() -> tuple[str, tuple[str, ...]]:
    return f"state NOT IN ({', '.join('?' * len(TURN_ENDED))})", tuple(sorted(TURN_ENDED))


class McpTurnStoreMixin(SqliteBase):
    """The McpTurnStore facet of the SQLite store."""

    def _create_mcp_turn_table(self) -> None:
        self._conn.executescript(_SCHEMA)

    # -- sync core -------------------------------------------------------------

    def save_mcp_turn_sync(self, record: McpTurnRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO mcp_turns ({_COLUMNS}) VALUES ({', '.join('?' * len(_FIELDS))}) "
                f"ON CONFLICT(turn_id) DO UPDATE SET {', '.join(f'{name} = excluded.{name}' for name in _REWRITTEN)}",
                tuple(getattr(record, name) for name in _FIELDS),
            )
            self._conn.commit()

    def get_mcp_turn_sync(self, turn_id: str) -> McpTurnRecord | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM mcp_turns WHERE turn_id = ?", (turn_id,)).fetchone()
        return _record(row) if row is not None else None

    def latest_mcp_turn_sync(self, conversation: str) -> McpTurnRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM mcp_turns WHERE conversation = ? ORDER BY started_at DESC LIMIT 1",
                (conversation,),
            ).fetchone()
        return _record(row) if row is not None else None

    def find_mcp_turn_sync(self, subject: str, client: str, request_digest: str) -> McpTurnRecord | None:
        if not request_digest:
            return None
        clause, states = _not_ended()
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM mcp_turns WHERE subject = ? AND client = ? AND request_digest = ? AND {clause} "
                "ORDER BY started_at DESC LIMIT 1",
                (subject, client, request_digest, *states),
            ).fetchone()
        return _record(row) if row is not None else None

    def request_mcp_cancel_sync(self, turn_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("UPDATE mcp_turns SET cancel_requested = 1 WHERE turn_id = ?", (turn_id,))
            self._conn.commit()
        return cursor.rowcount == 1

    def fail_mcp_turns_sync(self, replica: str, reason: str, now: str, expires_at: str) -> int:
        clause, states = _not_ended()
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE mcp_turns SET state = ?, step = '', answer = ?, is_error = 1, updated_at = ?, expires_at = ? "
                f"WHERE replica = ? AND {clause}",
                (TURN_FAILED, reason, now, expires_at, replica, *states),
            )
            self._conn.commit()
        return cursor.rowcount

    def purge_mcp_turns_sync(self, now: str) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM mcp_turns WHERE expires_at <= ?", (now,))
            self._conn.commit()
        return cursor.rowcount

    # -- async facade (McpTurnStore port) --------------------------------------

    async def save_mcp_turn(self, record: McpTurnRecord) -> None:
        await asyncio.to_thread(self.save_mcp_turn_sync, record)

    async def get_mcp_turn(self, turn_id: str) -> McpTurnRecord | None:
        return await asyncio.to_thread(self.get_mcp_turn_sync, turn_id)

    async def latest_mcp_turn(self, conversation: str) -> McpTurnRecord | None:
        return await asyncio.to_thread(self.latest_mcp_turn_sync, conversation)

    async def find_mcp_turn(self, subject: str, client: str, request_digest: str) -> McpTurnRecord | None:
        return await asyncio.to_thread(self.find_mcp_turn_sync, subject, client, request_digest)

    async def request_mcp_cancel(self, turn_id: str) -> bool:
        return await asyncio.to_thread(self.request_mcp_cancel_sync, turn_id)

    async def fail_mcp_turns(self, replica: str, reason: str, now: str, expires_at: str) -> int:
        return await asyncio.to_thread(self.fail_mcp_turns_sync, replica, reason, now, expires_at)

    async def purge_mcp_turns(self, now: str) -> int:
        return await asyncio.to_thread(self.purge_mcp_turns_sync, now)
