"""The MongoDB backend: the session inventory, plus the interaction, form,
dedup and agent facets, composed with the task and approval mixins into the one
class an application constructs.

Mirrors ``store/sessions.py`` method for method on purpose — the two are meant
to be readable side by side, because the conformance suite holds them to the
same answers.
"""

from __future__ import annotations

import asyncio

from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from crucible.ports.chat.directory import AgentInfo
from crucible.store import clock
from crucible.store.base import (
    FormRecord,
    InteractionRecord,
    SessionRecord,
    derive_runtime_session_id,
)
from crucible.store.mongo.approvals import MongoApprovalMixin
from crucible.store.mongo.base import (
    AGENTS,
    FORMS,
    INTERACTIONS,
    PROCESSED,
    SESSIONS,
    from_doc,
    to_doc,
)
from crucible.store.mongo.tasks import MongoTaskMixin


class MongoSessionStore(MongoTaskMixin, MongoApprovalMixin):
    """The whole inventory on MongoDB — every facet of the ``Store`` port.

    Async all the way down: ``pymongo`` ships its own async client, so unlike
    the SQLite backend there is no thread to hop to and no sync core underneath.
    """

    def __init__(self, url: str, database: str) -> None:
        self._client: AsyncMongoClient = AsyncMongoClient(url)
        self._db = self._client[database]
        self._indexed = False
        self._index_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.close()

    # -- sessions -------------------------------------------------------------

    async def get_or_create(
        self, agent: str, channel_id: str, conversation_id: str, kind: str,
        user_id: str = "",
    ) -> tuple[SessionRecord, bool]:
        db = await self._ready()
        now = clock.now_iso()
        key = {"agent": agent, "conversation_id": conversation_id}
        result = await db[SESSIONS].update_one(
            key,
            {"$setOnInsert": to_doc(SessionRecord(
                agent=agent, channel_id=channel_id, conversation_id=conversation_id,
                kind=kind, runtime_session_id=derive_runtime_session_id(agent, conversation_id),
                created_at=now, last_active=now, last_user_id=user_id,
            ))},
            upsert=True,
        )
        created = result.upserted_id is not None
        # Refresh the last user on an existing record too, so a tool called
        # mid-turn addresses THIS turn's user, not the one who opened the
        # conversation. ($setOnInsert keeps the `created` flag clean.)
        if not created and user_id:
            await db[SESSIONS].update_one(key, {"$set": {"last_user_id": user_id}})
        doc = await db[SESSIONS].find_one(key)
        assert doc is not None  # we just upserted it
        return from_doc(SessionRecord, doc), created

    async def touch(self, agent: str, conversation_id: str, user_id: str = "") -> None:
        db = await self._ready()
        changes = {"last_active": clock.now_iso()}
        if user_id:
            changes["last_user_id"] = user_id
        await db[SESSIONS].update_one(
            {"agent": agent, "conversation_id": conversation_id}, {"$set": changes}
        )

    async def list(self, agent: str | None = None) -> list[SessionRecord]:
        db = await self._ready()
        query = {"agent": agent} if agent is not None else {}
        cursor = db[SESSIONS].find(query).sort([("last_active", -1)])
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
            await db[PROCESSED].insert_one({"_id": f"{agent}\x00{post_id}"})
        except DuplicateKeyError:
            return False
        return True

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
