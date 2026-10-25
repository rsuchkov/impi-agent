"""The MongoDB backend: the session inventory, plus the interaction, form,
dedup and agent facets, composed with the task and approval mixins into the one
class an application constructs.

One method per port method, in the port's own order — the same order the
SQLite package keeps — so the two backends read side by side; the conformance
suite holds both to the same answers.
"""

from __future__ import annotations

import asyncio

from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from crucible.ports.chat.directory import AgentInfo
from crucible.store import clock
from crucible.store.base import (
    FormRecord,
    GatewayAddress,
    InteractionRecord,
    SessionRecord,
    derive_runtime_session_id,
    with_gateway_address,
)
from crucible.store.mongo.approvals import MongoApprovalMixin
from crucible.store.mongo.base import (
    AGENTS,
    FORMS,
    INTERACTIONS,
    PROCESSED,
    SESSIONS,
    Collections,
    from_doc,
    to_doc,
)
from crucible.store.mongo.connections import MongoConnectionMixin
from crucible.store.mongo.leases import MongoLeaseMixin
from crucible.store.mongo.replicas import MongoReplicaMixin
from crucible.store.mongo.tasks import MongoTaskMixin
from crucible.store.mongo.traces import MongoTraceMixin


class MongoSessionStore(
    MongoTaskMixin, MongoApprovalMixin, MongoTraceMixin, MongoConnectionMixin, MongoLeaseMixin,
    MongoReplicaMixin,
):
    """The whole inventory on MongoDB — every facet of the ``Store`` port.

    Async all the way down: ``pymongo`` ships its own async client, so there is
    no thread to hop to and no sync core underneath.
    """

    def __init__(self, url: str, database: str, *, prefix: str = "") -> None:
        self._client: AsyncMongoClient = AsyncMongoClient(url)
        # ``prefix`` goes on every collection name: for a database this engine
        # shares with another one, or with the application's own collections.
        self._db = Collections(self._client[database], prefix)
        self._indexed = False
        self._index_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.close()

    # -- sessions -------------------------------------------------------------

    async def get_or_create(
        self, agent: str, channel_id: str, conversation_id: str, kind: str,
        user_id: str = "", gateway: str = "",
    ) -> tuple[SessionRecord, bool]:
        db = await self._ready()
        now = clock.now_iso()
        key = {"agent": agent, "conversation_id": conversation_id}
        result = await db[SESSIONS].update_one(
            key,
            {"$setOnInsert": to_doc(SessionRecord(
                agent=agent, channel_id=channel_id, conversation_id=conversation_id,
                kind=kind, runtime_session_id=derive_runtime_session_id(agent, conversation_id),
                created_at=now, last_active=now, last_user_id=user_id, last_gateway=gateway,
            ))},
            upsert=True,
        )
        created = result.upserted_id is not None
        # Refresh the last user and gateway on an existing record too, so a
        # tool called mid-turn — and the card it raises — addresses THIS turn's
        # user where they wrote from, not the one who opened the conversation.
        # ($setOnInsert keeps the `created` flag clean.)
        arrival = {k: v for k, v in (("last_user_id", user_id), ("last_gateway", gateway)) if v}
        if not created and arrival:
            await db[SESSIONS].update_one(key, {"$set": arrival})
        doc = await db[SESSIONS].find_one(key)
        assert doc is not None  # we just upserted it
        return from_doc(SessionRecord, doc), created

    async def touch(
        self, agent: str, conversation_id: str, user_id: str = "", *,
        engine_turn: bool = False,
    ) -> None:
        db = await self._ready()
        now = clock.now_iso()
        changes = {"last_active": now}
        if user_id:
            changes["last_user_id"] = user_id
        if engine_turn:
            changes["last_engine_turn_at"] = now
        await db[SESSIONS].update_one(
            {"agent": agent, "conversation_id": conversation_id}, {"$set": changes}
        )

    async def set_gateway_address(
        self, agent: str, conversation_id: str, gateway: str, address: GatewayAddress
    ) -> None:
        db = await self._ready()
        key = {"agent": agent, "conversation_id": conversation_id}
        doc = await db[SESSIONS].find_one(key)
        if doc is None:
            return
        record = from_doc(SessionRecord, doc)
        await db[SESSIONS].update_one(key, {"$set": {"gateways": with_gateway_address(record, gateway, address)}})

    async def set_cut_short(self, agent: str, conversation_id: str, note: str) -> None:
        db = await self._ready()
        await db[SESSIONS].update_one(
            {"agent": agent, "conversation_id": conversation_id}, {"$set": {"cut_short": note}}
        )

    async def list(self, agent: str | None = None) -> list[SessionRecord]:
        db = await self._ready()
        query = {"agent": agent} if agent is not None else {}
        cursor = db[SESSIONS].find(query).sort([("last_active", -1)])
        return [from_doc(SessionRecord, doc) async for doc in cursor]

    async def recent_engine_turns(self, agent: str, since: str) -> list[SessionRecord]:
        db = await self._ready()
        cursor = db[SESSIONS].find(
            {"agent": agent, "last_engine_turn_at": {"$gte": since, "$ne": ""}}
        ).sort([("last_engine_turn_at", -1)])
        return [from_doc(SessionRecord, doc) async for doc in cursor]

    async def delete(self, agent: str, conversation_id: str) -> SessionRecord | None:
        db = await self._ready()
        doc = await db[SESSIONS].find_one_and_delete(
            {"agent": agent, "conversation_id": conversation_id}
        )
        return from_doc(SessionRecord, doc) if doc else None

    async def get_by_runtime_session(self, runtime_session_id: str) -> SessionRecord | None:
        db = await self._ready()
        doc = await db[SESSIONS].find_one({"runtime_session_id": runtime_session_id})
        return from_doc(SessionRecord, doc) if doc else None

    async def mark_processed(self, agent: str, post_id: str) -> bool:
        db = await self._ready()
        try:
            # The pair IS the key, so the uniqueness needs no separate index and
            # the insert cannot half-succeed: a replayed post is a DuplicateKey,
            # which is the whole answer.
            await db[PROCESSED].insert_one(
                {"_id": f"{agent}\x00{post_id}", "seen_at": clock.now_iso()}
            )
        except DuplicateKeyError:
            return False
        return True

    async def prune_processed(self, *, before: str) -> int:
        db = await self._ready()
        # Documents from before the field existed have no date; they go too —
        # older than any retention by definition.
        result = await db[PROCESSED].delete_many(
            {"$or": [{"seen_at": {"$lt": before}}, {"seen_at": {"$exists": False}}]}
        )
        return result.deleted_count

    # -- interactions and forms ------------------------------------------------

    async def create_interaction(self, record: InteractionRecord) -> None:
        db = await self._ready()
        await db[INTERACTIONS].insert_one(to_doc(record, _id=record.token))

    async def take_interaction(self, token: str) -> InteractionRecord | None:
        db = await self._ready()
        # One-shot: find and delete in a single operation, so a replayed click
        # cannot fire the interaction twice.
        doc = await db[INTERACTIONS].find_one_and_delete({"_id": token})
        return from_doc(InteractionRecord, doc) if doc else None

    async def create_form(self, record: FormRecord) -> None:
        db = await self._ready()
        await db[FORMS].insert_one(to_doc(record, _id=record.token))

    async def get_form(self, token: str) -> FormRecord | None:
        db = await self._ready()
        doc = await db[FORMS].find_one({"_id": token})
        return from_doc(FormRecord, doc) if doc else None

    async def delete_form(self, token: str) -> None:
        db = await self._ready()
        await db[FORMS].delete_one({"_id": token})

    # -- the agent registry ----------------------------------------------------

    async def upsert_agent(self, info: AgentInfo) -> None:
        db = await self._ready()
        await db[AGENTS].update_one(
            {"_id": info.name},
            {"$set": {
                "name": info.name, "role": info.role, "description": info.description,
                "username": info.username, "user_id": info.user_id,
                "updated_at": clock.now_iso(),
            }},
            upsert=True,
        )

    async def list_agents(self) -> list[AgentInfo]:
        db = await self._ready()
        cursor = db[AGENTS].find({}).sort([("name", 1)])
        return [
            AgentInfo(
                name=doc["name"], role=doc["role"], description=doc["description"],
                username=doc["username"], user_id=doc["user_id"],
            )
            async for doc in cursor
        ]
