"""Windows a human left open, and the ledger of everything that was asked.

A mixin over the same connection and lock as the session inventory, like the
scheduler and secret-policy facets. Neither table is secret-shaped: they are
keyed by ``(kind, principal, scope)``, so the same two answer "may this agent
use that credential" and "may this agent run that tool". A window and a ledger
row are the same idea either way, and having two copies of them would be how the
two consumers drift apart.

Nothing here holds a value or a capability — only a permission and a record.
"""

import asyncio
import sqlite3
import threading
from dataclasses import fields

from crucible.store.base import ApprovalAudit, ApprovalGrant

_APPROVALS_SCHEMA = """
-- A window, not a capability. revoked_at is '' while live: '' sorts before
-- every timestamp, so "revoked" is a non-empty test rather than a NULL check.
CREATE TABLE IF NOT EXISTS approval_grants (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  principal  TEXT NOT NULL,
  scope      TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_grants_live
  ON approval_grants (kind, principal, scope, expires_at);

-- Append-only. Every request lands here, including the refused ones and the
-- ones naming something that does not exist — the caller is told the same thing
-- either way, so probing is only visible from this table.
CREATE TABLE IF NOT EXISTS approval_audit (
  id          TEXT PRIMARY KEY,
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,
  principal   TEXT NOT NULL,
  scope       TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT '',
  decision    TEXT NOT NULL,
  approver    TEXT NOT NULL DEFAULT '',
  grant_id    TEXT NOT NULL DEFAULT '',
  request_id  TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON approval_audit (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_scope ON approval_audit (kind, scope, at DESC);
"""

# Column order comes from the dataclasses, so SQL and records cannot drift.
_GRANT_FIELDS = tuple(f.name for f in fields(ApprovalGrant))
_GRANT_COLUMNS = ", ".join(_GRANT_FIELDS)
_GRANT_PLACEHOLDERS = ", ".join("?" * len(_GRANT_FIELDS))
_AUDIT_FIELDS = tuple(f.name for f in fields(ApprovalAudit))
_AUDIT_COLUMNS = ", ".join(_AUDIT_FIELDS)
_AUDIT_PLACEHOLDERS = ", ".join("?" * len(_AUDIT_FIELDS))


def _grant(row: tuple) -> ApprovalGrant:
    return ApprovalGrant(**dict(zip(_GRANT_FIELDS, row, strict=True)))


def _audit(row: tuple) -> ApprovalAudit:
    return ApprovalAudit(**dict(zip(_AUDIT_FIELDS, row, strict=True)))


def _values(record: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, name) for name in names)


class ApprovalStoreMixin:
    """The ApprovalStore facet of the SQLite store."""

    # Declared, not created: both belong to the store this is mixed into.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def _create_approval_tables(self) -> None:
        """Create this facet's tables. The composing store calls it while it
        holds the lock on open, so the schema stays the mixin's own business."""
        self._conn.executescript(_APPROVALS_SCHEMA)

    # -- windows --------------------------------------------------------------

    def create_grant_sync(self, grant: ApprovalGrant) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO approval_grants ({_GRANT_COLUMNS}) "
                f"VALUES ({_GRANT_PLACEHOLDERS})",
                _values(grant, _GRANT_FIELDS),
            )
            self._conn.commit()

    def live_grant_sync(
        self, kind: str, principal: str, scope: str, *, now: str
    ) -> ApprovalGrant | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_GRANT_COLUMNS} FROM approval_grants "
                "WHERE kind = ? AND principal = ? AND scope = ? "
                "AND revoked_at = '' AND expires_at > ? "
                # The newest wins: asking again before the old window closes
                # extends the permission rather than being ignored.
                "ORDER BY expires_at DESC LIMIT 1",
                (kind, principal, scope, now),
            ).fetchone()
        return _grant(row) if row else None

    def list_grants_sync(
        self, *, now: str, kind: str = "", include_dead: bool = False
    ) -> list[ApprovalGrant]:
        where, args = [], []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if not include_dead:
            where.append("revoked_at = '' AND expires_at > ?")
            args.append(now)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_GRANT_COLUMNS} FROM approval_grants{clause} "
                "ORDER BY granted_at DESC, id DESC",
                args,
            ).fetchall()
        return [_grant(row) for row in rows]

    def revoke_grant_sync(self, grant_id: str, *, now: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE approval_grants SET revoked_at = ? WHERE id = ? AND revoked_at = ''",
                (now, grant_id),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def revoke_scope_sync(self, kind: str, scope: str, *, now: str) -> int:
        """Close every window over one scope — what deleting the thing the
        windows point at has to do, or an agent keeps reaching something whose
        permission is gone."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE approval_grants SET revoked_at = ? "
                "WHERE kind = ? AND scope = ? AND revoked_at = ''",
                (now, kind, scope),
            )
            self._conn.commit()
        return cursor.rowcount

    # -- ledger ---------------------------------------------------------------

    def record_decision_sync(self, record: ApprovalAudit) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO approval_audit ({_AUDIT_COLUMNS}) "
                f"VALUES ({_AUDIT_PLACEHOLDERS})",
                _values(record, _AUDIT_FIELDS),
            )
            self._conn.commit()

    def list_audit_sync(
        self, *, limit: int = 50, kind: str = "", principal: str = "", scope: str = ""
    ) -> list[ApprovalAudit]:
        where, args = [], []
        for column, value in (("kind", kind), ("principal", principal), ("scope", scope)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(
                # rowid breaks ties, and it has to be rowid rather than id: `at`
                # has second resolution, a request and the approval answering it
                # land inside one second routinely, and the id is a random token
                # — ordering by it would be stable but arbitrary. rowid is
                # insertion order.
                f"SELECT {_AUDIT_COLUMNS} FROM approval_audit{clause} "
                "ORDER BY at DESC, rowid DESC LIMIT ?",
                args,
            ).fetchall()
        return [_audit(row) for row in rows]

    # -- async facade (ApprovalStore port) -------------------------------------

    async def create_grant(self, grant: ApprovalGrant) -> None:
        await asyncio.to_thread(self.create_grant_sync, grant)

    async def live_grant(
        self, kind: str, principal: str, scope: str, *, now: str
    ) -> ApprovalGrant | None:
        return await asyncio.to_thread(self.live_grant_sync, kind, principal, scope, now=now)

    async def list_grants(
        self, *, now: str, kind: str = "", include_dead: bool = False
    ) -> list[ApprovalGrant]:
        return await asyncio.to_thread(
            self.list_grants_sync, now=now, kind=kind, include_dead=include_dead
        )

    async def revoke_grant(self, grant_id: str, *, now: str) -> bool:
        return await asyncio.to_thread(self.revoke_grant_sync, grant_id, now=now)

    async def revoke_scope(self, kind: str, scope: str, *, now: str) -> int:
        return await asyncio.to_thread(self.revoke_scope_sync, kind, scope, now=now)

    async def record_decision(self, record: ApprovalAudit) -> None:
        await asyncio.to_thread(self.record_decision_sync, record)

    async def list_audit(
        self, *, limit: int = 50, kind: str = "", principal: str = "", scope: str = ""
    ) -> list[ApprovalAudit]:
        return await asyncio.to_thread(
            self.list_audit_sync, limit=limit, kind=kind, principal=principal, scope=scope
        )
