"""People's connected accounts and the connects they have started — the
ConnectionStore and ConnectionLinkStore facets of the MongoDB backend.

The refresh lease is a field beside the record, claimed by one ``update_one``
whose filter holds only while the record is still the credential the caller
saw and no lease runs: atomic on the server, so of two claims at once exactly
one matches. A link carries its expiry twice — the ISO string every backend
keeps, and a date for the TTL index, which reads nothing else; the service
checks the expiry itself, since the TTL monitor runs when it runs.
"""

from __future__ import annotations

from datetime import datetime

from crucible.store.base import ConnectionLinkRecord, ConnectionRecord
from crucible.store.mongo.base import (
    CONNECTION_LINKS,
    CONNECTIONS,
    MongoBase,
    from_doc,
    to_doc,
)


def _key(owner: str, provider: str) -> dict:
    return {"owner": owner, "provider": provider}


def _credential(connect_id: str | None, version: int | None) -> dict:
    found: dict = {}
    if connect_id is not None:
        found["connect_id"] = connect_id
    if version is not None:
        found["version"] = version
    return found


class MongoConnectionMixin(MongoBase):
    # -- connections ------------------------------------------------------------

    async def get_connection(self, owner: str, provider: str) -> ConnectionRecord | None:
        db = await self._ready()
        doc = await db[CONNECTIONS].find_one(_key(owner, provider))
        return from_doc(ConnectionRecord, doc) if doc else None

    async def list_connections(self, owner: str) -> list[ConnectionRecord]:
        db = await self._ready()
        cursor = db[CONNECTIONS].find({"owner": owner}).sort("provider", 1)
        return [from_doc(ConnectionRecord, doc) async for doc in cursor]

    async def save_connection(self, record: ConnectionRecord) -> None:
        db = await self._ready()
        await db[CONNECTIONS].replace_one(
            _key(record.owner, record.provider), to_doc(record, lease_until=""), upsert=True,
        )

    async def claim_refresh(
        self, owner: str, provider: str, *, connect_id: str, version: int, now: str, until: str
    ) -> bool:
        db = await self._ready()
        result = await db[CONNECTIONS].update_one(
            {**_key(owner, provider), "connect_id": connect_id, "version": version,
             "lease_until": {"$lte": now}},
            {"$set": {"lease_until": until}},
        )
        return result.matched_count == 1

    async def release_refresh(self, owner: str, provider: str, *, connect_id: str, version: int) -> None:
        db = await self._ready()
        await db[CONNECTIONS].update_one(
            {**_key(owner, provider), "connect_id": connect_id, "version": version},
            {"$set": {"lease_until": ""}},
        )

    async def replace_connection(self, record: ConnectionRecord, *, expected_version: int) -> bool:
        db = await self._ready()
        result = await db[CONNECTIONS].replace_one(
            {**_key(record.owner, record.provider), "connect_id": record.connect_id, "version": expected_version},
            to_doc(record, lease_until=""),
        )
        return result.matched_count == 1

    async def delete_connection(
        self, owner: str, provider: str, *, connect_id: str | None = None, version: int | None = None
    ) -> bool:
        db = await self._ready()
        result = await db[CONNECTIONS].delete_one({**_key(owner, provider), **_credential(connect_id, version)})
        return result.deleted_count == 1

    # -- links ----------------------------------------------------------------------

    async def put_link(self, link: ConnectionLinkRecord) -> None:
        db = await self._ready()
        await db[CONNECTION_LINKS].insert_one(
            to_doc(link, _id=link.state_digest, expire_at=datetime.fromisoformat(link.expires_at)),
        )

    async def take_link(self, state_digest: str) -> ConnectionLinkRecord | None:
        db = await self._ready()
        doc = await db[CONNECTION_LINKS].find_one_and_delete({"_id": state_digest})
        return from_doc(ConnectionLinkRecord, doc) if doc else None
