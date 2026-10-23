"""The LeaseStore facet of the MongoDB store.

One document per lease name, ``_id`` the name. Taking a lease is one
``update_one`` whose filter says "mine, or lapsed" — a single-document
operation, so two replicas asking at once get one yes — and, when nothing
matched, one ``insert_one`` that the unique ``_id`` turns into a no when the
other replica got there first.
"""

from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from crucible.store import clock
from crucible.store.mongo.base import LEASES, MongoBase


def _until(ttl_s: float) -> str:
    now = datetime.fromisoformat(clock.now_iso())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now + timedelta(seconds=ttl_s)).isoformat()


class MongoLeaseMixin(MongoBase):
    async def acquire_lease(self, name: str, owner: str, *, ttl_s: float) -> bool:
        db = await self._ready()
        now, until = clock.now_iso(), _until(ttl_s)
        taken = await db[LEASES].update_one(
            {"_id": name, "$or": [{"owner": owner}, {"until": {"$lt": now}}]},
            {"$set": {"owner": owner, "until": until}},
        )
        if taken.matched_count == 1:
            return True
        try:
            await db[LEASES].insert_one({"_id": name, "owner": owner, "until": until})
        except DuplicateKeyError:
            return False
        return True

    async def release_lease(self, name: str, owner: str) -> None:
        db = await self._ready()
        await db[LEASES].delete_one({"_id": name, "owner": owner})
