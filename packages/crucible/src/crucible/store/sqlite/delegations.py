"""The tasks agents handed one another — the DelegationStore facet of the
SQLite store.

A mixin over the same connection and lock as the session inventory. One row
per task, written when it starts and replaced as it ends; the asker reads it
by id. The answer is bounded by the service before it gets here.
"""

import asyncio
from dataclasses import fields

from crucible.ports.delegation import DELEGATION_RUNNING
from crucible.store.base import DelegationRecord
from crucible.store.sqlite.base import SqliteBase

_DELEGATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
  id              TEXT PRIMARY KEY,
  agent           TEXT NOT NULL,
  "to"            TEXT NOT NULL,
  channel_id      TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  kind            TEXT NOT NULL,
  gateway         TEXT NOT NULL,
  task            TEXT NOT NULL,
  user_id         TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  files           TEXT NOT NULL DEFAULT '',
  attributes      TEXT NOT NULL DEFAULT '',
  finished_at     TEXT NOT NULL DEFAULT '',
  answer          TEXT NOT NULL DEFAULT '',
  notice          TEXT NOT NULL DEFAULT '',
  delivered_at    TEXT NOT NULL DEFAULT ''
);
-- A restart asks for every task still running; nothing else scans the table.
CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations (status);
"""

# Column order comes from the dataclass, so SQL and records cannot drift.
# ``to`` is a keyword in SQL, hence the quoting.
_FIELDS = tuple(f.name for f in fields(DelegationRecord))
_COLUMNS = ", ".join(f'"{name}"' for name in _FIELDS)
_PLACEHOLDERS = ", ".join("?" * len(_FIELDS))


def _record(row: tuple) -> DelegationRecord:
    return DelegationRecord(**dict(zip(_FIELDS, row, strict=True)))


class DelegationStoreMixin(SqliteBase):
    """The DelegationStore facet of the SQLite store."""

    def _create_delegation_table(self) -> None:
        self._conn.executescript(_DELEGATIONS_SCHEMA)

    # -- sync core -------------------------------------------------------------

    def save_delegation_sync(self, record: DelegationRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO delegations ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                tuple(getattr(record, name) for name in _FIELDS),
            )
            self._conn.commit()

    def end_delegation_sync(self, record: DelegationRecord) -> bool:
        assignments = ", ".join(f'"{name}" = ?' for name in _FIELDS if name != "id")
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE delegations SET {assignments} WHERE id = ? AND status = ?",
                (*(getattr(record, name) for name in _FIELDS if name != "id"), record.id, DELEGATION_RUNNING),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def get_delegation_sync(self, task_id: str) -> DelegationRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE id = ?", (task_id,)
            ).fetchone()
        return _record(row) if row else None

    def running_delegations_sync(self) -> list[DelegationRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE status = ? ORDER BY started_at, id",
                (DELEGATION_RUNNING,),
            ).fetchall()
        return [_record(row) for row in rows]

    # -- async facade (DelegationStore port) -----------------------------------

    async def save_delegation(self, record: DelegationRecord) -> None:
        await asyncio.to_thread(self.save_delegation_sync, record)

    async def end_delegation(self, record: DelegationRecord) -> bool:
        return await asyncio.to_thread(self.end_delegation_sync, record)

    async def get_delegation(self, task_id: str) -> DelegationRecord | None:
        return await asyncio.to_thread(self.get_delegation_sync, task_id)

    async def running_delegations(self) -> list[DelegationRecord]:
        return await asyncio.to_thread(self.running_delegations_sync)
