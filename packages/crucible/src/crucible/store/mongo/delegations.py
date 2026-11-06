"""The DelegationStore facet of the MongoDB backend: the tasks agents handed
one another, one document per task, replaced as the task ends."""

from __future__ import annotations

from crucible.ports.delegation import DELEGATION_RUNNING
from crucible.store.base import DelegationRecord
from crucible.store.mongo.base import DELEGATIONS, MongoBase, from_doc, to_doc


class MongoDelegationMixin(MongoBase):
    async def save_delegation(self, record: DelegationRecord) -> None:
        db = await self._ready()
        await db[DELEGATIONS].replace_one({"_id": record.id}, to_doc(record, _id=record.id), upsert=True)

    async def end_delegation(self, record: DelegationRecord) -> bool:
        db = await self._ready()
        result = await db[DELEGATIONS].replace_one(
            {"_id": record.id, "status": DELEGATION_RUNNING}, to_doc(record, _id=record.id),
        )
        return result.matched_count == 1

    async def get_delegation(self, task_id: str) -> DelegationRecord | None:
        db = await self._ready()
        doc = await db[DELEGATIONS].find_one({"_id": task_id})
        return from_doc(DelegationRecord, doc) if doc else None

    async def running_delegations(self) -> list[DelegationRecord]:
        db = await self._ready()
        cursor = db[DELEGATIONS].find({"status": DELEGATION_RUNNING}).sort([("started_at", 1), ("_id", 1)])
        return [from_doc(DelegationRecord, doc) async for doc in cursor]
