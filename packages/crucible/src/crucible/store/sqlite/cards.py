"""The HeldCardStore facet of the SQLite store: one row per card a conversation
holds outside a turn of its gateway's, by post id."""

import asyncio

from crucible.ports.held_cards import HeldCard
from crucible.store.sqlite.base import SqliteBase

_SCHEMA = """
CREATE TABLE IF NOT EXISTS held_cards (
  post_id TEXT PRIMARY KEY,
  gateway TEXT NOT NULL,
  conversation TEXT NOT NULL,   -- the gateway's own key for the conversation
  agent TEXT NOT NULL,
  card TEXT NOT NULL,           -- the card as JSON
  actions TEXT NOT NULL,        -- its controls as JSON
  held_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS held_cards_conversation ON held_cards (gateway, conversation, held_at);
"""
_COLUMNS = "gateway, conversation, post_id, agent, card, actions, held_at"


class HeldCardStoreMixin(SqliteBase):
    def _create_held_card_table(self) -> None:
        self._conn.executescript(_SCHEMA)

    def hold_card_sync(self, r: HeldCard) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO held_cards ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (post_id) DO UPDATE SET card = excluded.card, actions = excluded.actions",
                (r.gateway, r.conversation, r.post_id, r.agent, r.card, r.actions, r.held_at),
            )
            self._conn.commit()

    def held_cards_sync(self, gateway: str, conversation: str) -> list[HeldCard]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM held_cards WHERE gateway = ? AND conversation = ? ORDER BY held_at, post_id",
                (gateway, conversation),
            ).fetchall()
        return [HeldCard(*row) for row in rows]

    def held_card_sync(self, post_id: str) -> HeldCard | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM held_cards WHERE post_id = ?", (post_id,)).fetchone()
        return HeldCard(*row) if row else None

    def release_card_sync(self, post_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM held_cards WHERE post_id = ?", (post_id,))
            self._conn.commit()

    async def hold_card(self, record: HeldCard) -> None:
        await asyncio.to_thread(self.hold_card_sync, record)

    async def held_cards(self, gateway: str, conversation: str) -> list[HeldCard]:
        return await asyncio.to_thread(self.held_cards_sync, gateway, conversation)

    async def held_card(self, post_id: str) -> HeldCard | None:
        return await asyncio.to_thread(self.held_card_sync, post_id)

    async def release_card(self, post_id: str) -> None:
        await asyncio.to_thread(self.release_card_sync, post_id)
