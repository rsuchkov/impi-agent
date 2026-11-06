"""The UsageStore facet of the MongoDB backend: what turns cost at the model,
one document per turn, summed by agent and person since a moment."""

from __future__ import annotations

from crucible.store.base import UsageRecord, UsageSum
from crucible.store.mongo.base import USAGE, MongoBase, to_doc


class MongoUsageMixin(MongoBase):
    async def add_usage(self, record: UsageRecord) -> None:
        db = await self._ready()
        await db[USAGE].insert_one(to_doc(record))

    async def usage_since(self, agent: str, since: str, *, user_id: str | None = None) -> UsageSum:
        db = await self._ready()
        match: dict = {"agent": agent, "at": {"$gte": since}}
        if user_id is not None:
            match["user_id"] = user_id
        pipeline = [
            {"$match": match},
            {"$group": {"_id": None, "total": {"$sum": "$total"}, "cost_usd": {"$sum": "$cost_usd"}}},
        ]
        cursor = await db[USAGE].aggregate(pipeline)
        async for doc in cursor:
            return UsageSum(total=int(doc.get("total", 0)), cost_usd=float(doc.get("cost_usd", 0.0)))
        return UsageSum()

    async def prune_usage(self, *, before: str) -> int:
        db = await self._ready()
        result = await db[USAGE].delete_many({"at": {"$lt": before}})
        return result.deleted_count
