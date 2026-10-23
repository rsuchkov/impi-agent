"""The ReplicaStore facet of the SQLite store.

One row per conversation, naming the replica that owns it. The claim is one
UPDATE whose WHERE says "mine, taken by force, or the owner's heartbeat has
lapsed" — the heartbeat being the lease ``replica:<id>`` — and, when the row is
not there, one INSERT the primary key turns into a no when another replica got
there first. Who owns it afterwards is read back in the same lock.
"""

import asyncio
import sqlite3

from crucible.ports.replica import REPLICA_LEASE_PREFIX, ConversationOwner
from crucible.store import clock
from crucible.store.sqlite.base import SqliteBase

_REPLICA_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_owners (
  agent TEXT NOT NULL,
  conversation TEXT NOT NULL,
  replica TEXT NOT NULL,
  PRIMARY KEY (agent, conversation)
);
"""

# A replica is alive while its lease has not lapsed; its address is the holder.
_LIVE_URL = "SELECT owner FROM leases WHERE name = ? AND until >= ?"


class ReplicaStoreMixin(SqliteBase):
    def _create_replica_table(self) -> None:
        self._conn.executescript(_REPLICA_SCHEMA)

    def claim_conversation_sync(
        self, agent: str, conversation: str, replica: str, *, take: bool = False
    ) -> ConversationOwner:
        now = clock.now_iso()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE conversation_owners SET replica = ? "
                "WHERE agent = ? AND conversation = ? AND (replica = ? OR ? OR NOT EXISTS ("
                "  SELECT 1 FROM leases WHERE name = ? || conversation_owners.replica AND until >= ?))",
                (replica, agent, conversation, replica, int(take), REPLICA_LEASE_PREFIX, now),
            )
            if cursor.rowcount == 0:
                try:
                    self._conn.execute(
                        "INSERT INTO conversation_owners (agent, conversation, replica) VALUES (?, ?, ?)",
                        (agent, conversation, replica),
                    )
                except sqlite3.IntegrityError:  # a live owner has it
                    self._conn.rollback()
            self._conn.commit()
            row = self._conn.execute(
                "SELECT replica FROM conversation_owners WHERE agent = ? AND conversation = ?",
                (agent, conversation),
            ).fetchone()
            owner = row[0] if row else replica
            url = self._live_url(owner, now) if owner != replica else ""
            return ConversationOwner(owner, url or "")

    def replica_url_sync(self, replica: str) -> str | None:
        with self._lock:
            return self._live_url(replica, clock.now_iso())

    def _live_url(self, replica: str, now: str) -> str | None:
        row = self._conn.execute(_LIVE_URL, (REPLICA_LEASE_PREFIX + replica, now)).fetchone()
        return row[0] if row else None

    async def claim_conversation(
        self, agent: str, conversation: str, replica: str, *, take: bool = False
    ) -> ConversationOwner:
        return await asyncio.to_thread(
            self.claim_conversation_sync, agent, conversation, replica, take=take
        )

    async def replica_url(self, replica: str) -> str | None:
        return await asyncio.to_thread(self.replica_url_sync, replica)
