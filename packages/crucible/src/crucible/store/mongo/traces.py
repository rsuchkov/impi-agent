"""The TraceStore facet of the MongoDB backend: one turn's tool calls, kept for
the widget that shows them. Mirrors ``store/traces.py`` method for method."""

from __future__ import annotations

from crucible.store.base import TraceRecord
from crucible.store.mongo.base import TRACES, MongoBase, from_doc, to_doc


class MongoTraceMixin(MongoBase):
    async def create_trace(self, record: TraceRecord) -> None:
        db = await self._ready()
        await db[TRACES].insert_one(to_doc(record, _id=record.token))

    async def get_trace(self, token: str) -> TraceRecord | None:
        db = await self._ready()
        doc = await db[TRACES].find_one({"_id": token})
        return from_doc(TraceRecord, doc) if doc else None

    async def prune_traces(self, *, before: str) -> int:
        db = await self._ready()
        result = await db[TRACES].delete_many({"finished_at": {"$lt": before}})
        return result.deleted_count
