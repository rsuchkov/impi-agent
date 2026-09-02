"""The ApprovalStore facet of the MongoDB backend: windows a human left open,
and the ledger of what was asked."""

from __future__ import annotations

from crucible.store.base import ApprovalAudit, ApprovalGrant
from crucible.store.mongo.base import AUDIT, GRANTS, MongoBase, from_doc, to_doc


class MongoApprovalMixin(MongoBase):
    # -- windows --------------------------------------------------------------

    async def create_grant(self, grant: ApprovalGrant) -> None:
        db = await self._ready()
        await db[GRANTS].insert_one(to_doc(grant, _id=grant.id))

    async def live_grant(
        self, kind: str, principal: str, scope: str, *, now: str
    ) -> ApprovalGrant | None:
        db = await self._ready()
        doc = await db[GRANTS].find_one(
            {
                "kind": kind, "principal": principal, "scope": scope,
                "revoked_at": "", "expires_at": {"$gt": now},
            },
            # The newest wins: asking again before the old window closes extends
            # the permission rather than being ignored.
            sort=[("expires_at", -1)],
        )
        return from_doc(ApprovalGrant, doc) if doc else None

    async def list_grants(
        self, *, now: str, kind: str = "", include_dead: bool = False
    ) -> list[ApprovalGrant]:
        db = await self._ready()
        query: dict = {}
        if kind:
            query["kind"] = kind
        if not include_dead:
            query["revoked_at"] = ""
            query["expires_at"] = {"$gt": now}
        # _id IS the grant id, so this is SQLite's `granted_at DESC, id DESC`.
        cursor = db[GRANTS].find(query).sort([("granted_at", -1), ("_id", -1)])
        return [from_doc(ApprovalGrant, doc) async for doc in cursor]

    async def revoke_grant(self, grant_id: str, *, now: str) -> bool:
        db = await self._ready()
        result = await db[GRANTS].update_one(
            {"_id": grant_id, "revoked_at": ""}, {"$set": {"revoked_at": now}}
        )
        return result.matched_count == 1

    async def revoke_scope(self, kind: str, scope: str, *, now: str) -> int:
        db = await self._ready()
        result = await db[GRANTS].update_many(
            {"kind": kind, "scope": scope, "revoked_at": ""},
            {"$set": {"revoked_at": now}},
        )
        return result.matched_count

    # -- ledger ---------------------------------------------------------------

    async def record_decision(self, record: ApprovalAudit) -> None:
        db = await self._ready()
        # The id stays an ordinary field here, unlike a grant's. The ledger is
        # read newest-first and `at` has second resolution, so the tiebreaker
        # has to be insertion order — which is what a generated ObjectId is, and
        # what the id, a random token, is not.
        await db[AUDIT].insert_one(to_doc(record))

    async def list_audit(
        self, *, limit: int = 50, kind: str = "", principal: str = "", scope: str = ""
    ) -> list[ApprovalAudit]:
        db = await self._ready()
        query = {
            field: value
            for field, value in (("kind", kind), ("principal", principal), ("scope", scope))
            if value
        }
        cursor = (
            db[AUDIT].find(query).sort([("at", -1), ("_id", -1)]).limit(limit)
        )
        return [from_doc(ApprovalAudit, doc) async for doc in cursor]
