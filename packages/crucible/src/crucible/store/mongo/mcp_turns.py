"""The McpTurnStore facet of the MongoDB backend: the turns MCP clients' calls
opened, one document per turn, with a date beside the ISO expiry for the TTL
index (the gateway checks the expiry itself, since the TTL monitor runs when
it runs)."""

from __future__ import annotations

from datetime import datetime

from crucible.ports.mcp_turns import TURN_ENDED, TURN_FAILED, McpTurnRecord
from crucible.store.mongo.base import MCP_TURNS, MongoBase, from_doc, to_doc


class MongoMcpTurnMixin(MongoBase):
    async def save_mcp_turn(self, record: McpTurnRecord) -> None:
        db = await self._ready()
        doc = to_doc(record, expire_at=datetime.fromisoformat(record.expires_at))
        # The cancel mark is another replica's to set and nobody's to unset by a save.
        mark = doc.pop("cancel_requested")
        await db[MCP_TURNS].update_one(
            {"_id": record.turn_id}, {"$set": doc, "$setOnInsert": {"cancel_requested": mark}}, upsert=True,
        )

    async def get_mcp_turn(self, turn_id: str) -> McpTurnRecord | None:
        db = await self._ready()
        doc = await db[MCP_TURNS].find_one({"_id": turn_id})
        return from_doc(McpTurnRecord, doc) if doc else None

    async def latest_mcp_turn(self, conversation: str) -> McpTurnRecord | None:
        db = await self._ready()
        doc = await db[MCP_TURNS].find_one({"conversation": conversation}, sort=[("started_at", -1)])
        return from_doc(McpTurnRecord, doc) if doc else None

    async def find_mcp_turn(self, subject: str, client: str, request_digest: str) -> McpTurnRecord | None:
        if not request_digest:
            return None
        db = await self._ready()
        doc = await db[MCP_TURNS].find_one(
            {"subject": subject, "client": client, "request_digest": request_digest, "state": {"$nin": sorted(TURN_ENDED)}},
            sort=[("started_at", -1)],
        )
        return from_doc(McpTurnRecord, doc) if doc else None

    async def request_mcp_cancel(self, turn_id: str) -> bool:
        db = await self._ready()
        result = await db[MCP_TURNS].update_one({"_id": turn_id}, {"$set": {"cancel_requested": True}})
        return result.matched_count == 1

    async def fail_mcp_turns(self, replica: str, reason: str, now: str, expires_at: str) -> int:
        db = await self._ready()
        result = await db[MCP_TURNS].update_many(
            {"replica": replica, "state": {"$nin": sorted(TURN_ENDED)}},
            {"$set": {
                "state": TURN_FAILED, "step": "", "answer": reason, "is_error": True, "updated_at": now,
                "expires_at": expires_at, "expire_at": datetime.fromisoformat(expires_at),
            }},
        )
        return result.modified_count

    async def purge_mcp_turns(self, now: str) -> int:
        db = await self._ready()
        result = await db[MCP_TURNS].delete_many({"expires_at": {"$lte": now}})
        return result.deleted_count
