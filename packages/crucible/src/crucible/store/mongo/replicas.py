"""The ReplicaStore facet of the MongoDB store.

One document per conversation, ``_id`` the agent and the conversation, naming
the replica that owns it. A claim reads the document, decides whether the
owner may be replaced — it is this replica, it is taken by force, or its
heartbeat (the lease ``replica:<id>``) has lapsed — and replaces it with one
``update_one`` whose filter still names the owner it read, so two replicas
deciding at once get one yes; a missing document is one ``insert_one`` the
unique ``_id`` turns into a no for the second comer.
"""

from pymongo.errors import DuplicateKeyError

from crucible.ports.replica import REPLICA_LEASE_PREFIX, ConversationOwner
from crucible.store import clock
from crucible.store.mongo.base import CONVERSATION_OWNERS, LEASES, MongoBase


def _key(agent: str, conversation: str) -> str:
    return f"{agent}:{conversation}"  # an agent's name carries no colon


class MongoReplicaMixin(MongoBase):
    async def claim_conversation(
        self, agent: str, conversation: str, replica: str, *, take: bool = False
    ) -> ConversationOwner:
        db = await self._ready()
        key = _key(agent, conversation)
        for _ in range(3):  # a lost race is read again, not guessed about
            doc = await db[CONVERSATION_OWNERS].find_one({"_id": key})
            if doc is None:
                try:
                    await db[CONVERSATION_OWNERS].insert_one({"_id": key, "replica": replica})
                except DuplicateKeyError:
                    continue
                return ConversationOwner(replica)
            owner = str(doc["replica"])
            if owner == replica:
                return ConversationOwner(replica)
            url = await self.replica_url(owner)
            if url is not None and not take:
                return ConversationOwner(owner, url)
            moved = await db[CONVERSATION_OWNERS].update_one(
                {"_id": key, "replica": owner}, {"$set": {"replica": replica}}
            )
            if moved.matched_count == 1:
                return ConversationOwner(replica)
        doc = await db[CONVERSATION_OWNERS].find_one({"_id": key})
        owner = str(doc["replica"]) if doc else replica
        return ConversationOwner(owner, (await self.replica_url(owner) or "") if owner != replica else "")

    async def replica_url(self, replica: str) -> str | None:
        db = await self._ready()
        doc = await db[LEASES].find_one(
            {"_id": REPLICA_LEASE_PREFIX + replica, "until": {"$gte": clock.now_iso()}}
        )
        return str(doc["owner"]) if doc else None
