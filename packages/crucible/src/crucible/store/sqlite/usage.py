"""What turns cost at the model — the UsageStore facet of the SQLite store.

A mixin over the same connection and lock as the session inventory. One row
per turn that reported a cost; read summed by agent, and by person, since a
moment — what a budget is checked against before a turn.
"""

import asyncio
from dataclasses import fields

from crucible.store.base import UsageRecord, UsageSum
from crucible.store.sqlite.base import SqliteBase

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  agent       TEXT NOT NULL,
  user_id     TEXT NOT NULL DEFAULT '',
  at          TEXT NOT NULL,
  input       INTEGER NOT NULL DEFAULT 0,
  output      INTEGER NOT NULL DEFAULT 0,
  cache_read  INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  total       INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL NOT NULL DEFAULT 0
);
-- Summed by agent since a moment, and pruned by age.
CREATE INDEX IF NOT EXISTS idx_usage_agent_at ON usage (agent, at);
"""

_FIELDS = tuple(f.name for f in fields(UsageRecord))
_COLUMNS = ", ".join(_FIELDS)
_PLACEHOLDERS = ", ".join("?" * len(_FIELDS))


class UsageStoreMixin(SqliteBase):
    """The UsageStore facet of the SQLite store."""

    def _create_usage_table(self) -> None:
        self._conn.executescript(_USAGE_SCHEMA)

    # -- sync core -------------------------------------------------------------

    def add_usage_sync(self, record: UsageRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO usage ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                tuple(getattr(record, name) for name in _FIELDS),
            )
            self._conn.commit()

    def usage_since_sync(self, agent: str, since: str, *, user_id: str | None = None) -> UsageSum:
        clause, args = "agent = ? AND at >= ?", [agent, since]
        if user_id is not None:
            clause += " AND user_id = ?"
            args.append(user_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM(total), 0), COALESCE(SUM(cost_usd), 0) FROM usage WHERE {clause}", args
            ).fetchone()
        return UsageSum(total=int(row[0]), cost_usd=float(row[1]))

    def prune_usage_sync(self, *, before: str) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM usage WHERE at < ?", (before,))
            self._conn.commit()
        return cursor.rowcount

    # -- async facade (UsageStore port) ----------------------------------------

    async def add_usage(self, record: UsageRecord) -> None:
        await asyncio.to_thread(self.add_usage_sync, record)

    async def usage_since(self, agent: str, since: str, *, user_id: str | None = None) -> UsageSum:
        return await asyncio.to_thread(self.usage_since_sync, agent, since, user_id=user_id)

    async def prune_usage(self, *, before: str) -> int:
        return await asyncio.to_thread(self.prune_usage_sync, before=before)
