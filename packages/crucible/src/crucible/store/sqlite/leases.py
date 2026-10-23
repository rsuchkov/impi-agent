"""The LeaseStore facet of the SQLite store.

One table, one row per lease name. The compare-and-swap is one UPDATE whose
WHERE says "free, lapsed, or already mine": SQLite serialises writers, so two
processes on one file get one yes. Times are the store's ISO strings, which
order as text.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from crucible.store import clock
from crucible.store.sqlite.base import SqliteBase

_LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  until TEXT NOT NULL      -- ISO8601 UTC; lapsed when in the past
);
"""


def _until(ttl_s: float) -> str:
    now = datetime.fromisoformat(clock.now_iso())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now + timedelta(seconds=ttl_s)).isoformat()


class LeaseStoreMixin(SqliteBase):
    def _create_lease_table(self) -> None:
        self._conn.executescript(_LEASE_SCHEMA)

    def acquire_lease_sync(self, name: str, owner: str, *, ttl_s: float) -> bool:
        now, until = clock.now_iso(), _until(ttl_s)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE leases SET owner = ?, until = ? "
                "WHERE name = ? AND (owner = ? OR until < ?)",
                (owner, until, name, owner, now),
            )
            if cursor.rowcount == 1:
                self._conn.commit()
                return True
            try:
                self._conn.execute(
                    "INSERT INTO leases (name, owner, until) VALUES (?, ?, ?)", (name, owner, until)
                )
            except sqlite3.IntegrityError:  # somebody holds it, and it has not lapsed
                self._conn.rollback()
                return False
            self._conn.commit()
            return True

    def release_lease_sync(self, name: str, owner: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM leases WHERE name = ? AND owner = ?", (name, owner))
            self._conn.commit()

    async def acquire_lease(self, name: str, owner: str, *, ttl_s: float) -> bool:
        return await asyncio.to_thread(self.acquire_lease_sync, name, owner, ttl_s=ttl_s)

    async def release_lease(self, name: str, owner: str) -> None:
        await asyncio.to_thread(self.release_lease_sync, name, owner)
