"""The HeldCardStore facet of the MongoDB store: one document per card a
conversation holds outside a turn of its gateway's, ``_id`` the post id."""

from crucible.ports.held_cards import HeldCard
from crucible.store.mongo.base import HELD_CARDS, MongoBase, from_doc, to_doc


class MongoHeldCardMixin(MongoBase):
    async def hold_card(self, record: HeldCard) -> None:
        db = await self._ready()
        await db[HELD_CARDS].replace_one({"_id": record.post_id}, to_doc(record, _id=record.post_id), upsert=True)

    async def held_cards(self, gateway: str, conversation: str) -> list[HeldCard]:
        db = await self._ready()
        cursor = db[HELD_CARDS].find({"gateway": gateway, "conversation": conversation}).sort([("held_at", 1), ("_id", 1)])
        return [from_doc(HeldCard, doc) async for doc in cursor]

    async def held_card(self, post_id: str) -> HeldCard | None:
        db = await self._ready()
        doc = await db[HELD_CARDS].find_one({"_id": post_id})
        return from_doc(HeldCard, doc) if doc else None

    async def release_card(self, post_id: str) -> None:
        db = await self._ready()
        await db[HELD_CARDS].delete_one({"_id": post_id})
