"""What one turn's tool calls were, kept for the widget that shows them.

A mixin over the same connection and lock as the session inventory, like the
approvals facet. One row per turn that called a tool, written once when the
turn ends; the widget's clicks only ever read it. Rows carry no results —
see ``TraceRecord`` — so nothing here is a place a granted value could land.
"""

import asyncio
import sqlite3
import threading
from dataclasses import fields

from crucible.store.base import TraceRecord

_TRACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_traces (
  token           TEXT PRIMARY KEY,
  agent           TEXT NOT NULL,
  channel_id      TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  kind            TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  finished_at     TEXT NOT NULL,
  calls           TEXT NOT NULL,
  post_id         TEXT NOT NULL DEFAULT ''
);
-- Pruning is by age; nothing else ever scans the table.
CREATE INDEX IF NOT EXISTS idx_traces_finished ON tool_traces (finished_at);
"""

# Column order comes from the dataclass, so SQL and records cannot drift.
_TRACE_FIELDS = tuple(f.name for f in fields(TraceRecord))
_TRACE_COLUMNS = ", ".join(_TRACE_FIELDS)
_TRACE_PLACEHOLDERS = ", ".join("?" * len(_TRACE_FIELDS))


class TraceStoreMixin:
    """The TraceStore facet of the SQLite store."""

    # Declared, not created: both belong to the store this is mixed into.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def _create_trace_tables(self) -> None:
        """Create this facet's table. The composing store calls it while it
        holds the lock on open, so the schema stays the mixin's own business."""
        self._conn.executescript(_TRACES_SCHEMA)

    # -- sync core -------------------------------------------------------------

    def create_trace_sync(self, record: TraceRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tool_traces ({_TRACE_COLUMNS}) VALUES ({_TRACE_PLACEHOLDERS})",
                tuple(getattr(record, name) for name in _TRACE_FIELDS),
            )
            self._conn.commit()

    def get_trace_sync(self, token: str) -> TraceRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TRACE_COLUMNS} FROM tool_traces WHERE token = ?", (token,)
            ).fetchone()
        return TraceRecord(*row) if row else None

    def prune_traces_sync(self, *, before: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM tool_traces WHERE finished_at < ?", (before,)
            )
            self._conn.commit()
        return cursor.rowcount

    # -- async facade (TraceStore port) ----------------------------------------

    async def create_trace(self, record: TraceRecord) -> None:
        await asyncio.to_thread(self.create_trace_sync, record)

    async def get_trace(self, token: str) -> TraceRecord | None:
        return await asyncio.to_thread(self.get_trace_sync, token)

    async def prune_traces(self, *, before: str) -> int:
        return await asyncio.to_thread(self.prune_traces_sync, before=before)
