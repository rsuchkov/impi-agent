"""Who may ask for which secret, and on what terms.

The one part of secret handling that does not generalize: windows and the ledger
are shared with everything else a human can authorize (see
``store/approvals.py``), but a subject list and an approval mode are ideas only a
secret has — a tool gate has no notion of "which agents may".

No value is stored here, only permission. Values live in the backend.
"""

import asyncio
import sqlite3
import threading
from dataclasses import fields

from crucible.store.base import SecretPolicyRecord

_POLICIES_SCHEMA = """
-- Timestamps are UTC ISO8601 (seconds), so string order is time order.
CREATE TABLE IF NOT EXISTS secret_policies (
  name        TEXT PRIMARY KEY,
  approval    TEXT NOT NULL,
  max_grant_s INTEGER NOT NULL DEFAULT 0,
  subjects    TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
"""

# Column order comes from the dataclass, so SQL and record cannot drift.
_POLICY_FIELDS = tuple(f.name for f in fields(SecretPolicyRecord))
_POLICY_COLUMNS = ", ".join(_POLICY_FIELDS)
_POLICY_PLACEHOLDERS = ", ".join("?" * len(_POLICY_FIELDS))


def _policy(row: tuple) -> SecretPolicyRecord:
    return SecretPolicyRecord(**dict(zip(_POLICY_FIELDS, row, strict=True)))


class SecretPolicyStoreMixin:
    """The SecretPolicyStore facet of the SQLite store."""

    # Declared, not created: both belong to the store this is mixed into.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def _create_secret_tables(self) -> None:
        self._conn.executescript(_POLICIES_SCHEMA)

    def get_policy_sync(self, name: str) -> SecretPolicyRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_POLICY_COLUMNS} FROM secret_policies WHERE name = ?", (name,)
            ).fetchone()
        return _policy(row) if row else None

    def list_policies_sync(self) -> list[SecretPolicyRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_POLICY_COLUMNS} FROM secret_policies ORDER BY name"
            ).fetchall()
        return [_policy(row) for row in rows]

    def put_policy_sync(self, policy: SecretPolicyRecord) -> None:
        # Upsert on the name: the operator CLI is the only writer and is
        # expected to be idempotent, and created_at must survive an edit.
        assignments = ", ".join(
            f"{name} = excluded.{name}" for name in _POLICY_FIELDS if name != "created_at"
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO secret_policies ({_POLICY_COLUMNS}) "
                f"VALUES ({_POLICY_PLACEHOLDERS}) "
                f"ON CONFLICT (name) DO UPDATE SET {assignments}",
                tuple(getattr(policy, name) for name in _POLICY_FIELDS),
            )
            self._conn.commit()

    def delete_policy_sync(self, name: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM secret_policies WHERE name = ?", (name,))
            self._conn.commit()
        return cursor.rowcount == 1

    # -- async facade (SecretPolicyStore port) --------------------------------

    async def get_policy(self, name: str) -> SecretPolicyRecord | None:
        return await asyncio.to_thread(self.get_policy_sync, name)

    async def list_policies(self) -> list[SecretPolicyRecord]:
        return await asyncio.to_thread(self.list_policies_sync)

    async def put_policy(self, policy: SecretPolicyRecord) -> None:
        await asyncio.to_thread(self.put_policy_sync, policy)

    async def delete_policy(self, name: str) -> bool:
        return await asyncio.to_thread(self.delete_policy_sync, name)
