"""The McpGrantStore facet of the MongoDB backend: authorizations in progress
and the grants they came to. A document carries its expiry twice — the ISO
string every backend keeps, and a date for the TTL index; the server checks
the expiry itself, since the TTL monitor runs when it runs. A rotation is one
``find_one_and_replace`` whose filter holds only while the refresh token is
still the one the caller saw: atomic on the server."""

from __future__ import annotations

from datetime import datetime

from crucible.ports.mcp_grants import McpAuthRequest, McpGrant
from crucible.store.mongo.base import (
    MCP_AUTH_REQUESTS,
    MCP_GRANTS,
    MongoBase,
    from_doc,
    to_doc,
)


def _expire(record: McpAuthRequest | McpGrant) -> datetime:
    return datetime.fromisoformat(record.expires_at)


class MongoMcpGrantMixin(MongoBase):
    async def put_mcp_request(self, record: McpAuthRequest) -> None:
        db = await self._ready()
        await db[MCP_AUTH_REQUESTS].replace_one(
            {"_id": record.id}, to_doc(record, _id=record.id, expire_at=_expire(record)), upsert=True
        )

    async def get_mcp_request(self, request_id: str) -> McpAuthRequest | None:
        db = await self._ready()
        doc = await db[MCP_AUTH_REQUESTS].find_one({"_id": request_id})
        return from_doc(McpAuthRequest, doc) if doc else None

    async def take_mcp_request_by_code(self, code_hash: str) -> McpAuthRequest | None:
        if not code_hash:
            return None
        db = await self._ready()
        doc = await db[MCP_AUTH_REQUESTS].find_one_and_delete({"code_hash": code_hash})
        return from_doc(McpAuthRequest, doc) if doc else None

    async def delete_mcp_request(self, request_id: str) -> None:
        db = await self._ready()
        await db[MCP_AUTH_REQUESTS].delete_one({"_id": request_id})

    async def save_mcp_grant(self, record: McpGrant) -> None:
        db = await self._ready()
        await db[MCP_GRANTS].replace_one(
            {"_id": record.id}, to_doc(record, _id=record.id, expire_at=_expire(record)), upsert=True
        )

    async def replace_mcp_grant(self, record: McpGrant, *, expected_refresh_hash: str) -> bool:
        db = await self._ready()
        previous = await db[MCP_GRANTS].find_one_and_replace(
            {"_id": record.id, "refresh_hash": expected_refresh_hash},
            to_doc(record, _id=record.id, expire_at=_expire(record)),
        )
        return previous is not None

    async def get_mcp_grant_by_access(self, access_hash: str) -> McpGrant | None:
        db = await self._ready()
        doc = await db[MCP_GRANTS].find_one({"access_hash": access_hash})
        return from_doc(McpGrant, doc) if doc else None

    async def get_mcp_grant_by_refresh(self, refresh_hash: str) -> McpGrant | None:
        if not refresh_hash:
            return None
        db = await self._ready()
        doc = await db[MCP_GRANTS].find_one(
            {"$or": [{"refresh_hash": refresh_hash}, {"previous_refresh_hash": refresh_hash}]}
        )
        return from_doc(McpGrant, doc) if doc else None

    async def mcp_grants_of(self, subject: str) -> list[McpGrant]:
        db = await self._ready()
        cursor = db[MCP_GRANTS].find({"subject": subject}).sort("signed_in_at", 1)
        return [from_doc(McpGrant, doc) async for doc in cursor]

    async def delete_mcp_grant(self, grant_id: str) -> None:
        db = await self._ready()
        await db[MCP_GRANTS].delete_one({"_id": grant_id})

    async def purge_mcp_auth(self, now: str) -> int:
        db = await self._ready()
        requests = await db[MCP_AUTH_REQUESTS].delete_many({"expires_at": {"$lte": now}})
        grants = await db[MCP_GRANTS].delete_many({"expires_at": {"$lte": now}})
        return requests.deleted_count + grants.deleted_count
