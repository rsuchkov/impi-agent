"""ward's own file: who may ask for which secret, and on what terms.

Everything else in this database is the library's — sessions, the interactions a
click resolves, the windows and the ledger a human's answers land in. Policies
are not: a subject list and an approval mode are ideas only a secret has, a tool
gate has no notion of "which agents may", and the engine has no business
carrying a table it never reads. So the facet lives here and is mixed into the
library's store through the hook it leaves for exactly this.

No value is stored here, only permission. Values live in the backend.
"""

import asyncio
import sqlite3
import threading
from dataclasses import dataclass, fields
from typing import Protocol

from crucible.store.sessions import SqliteSessionStore
from ward.autorules import decode


@dataclass(frozen=True)
class SecretPolicyRecord:
    """Who may ask for a secret, and on what terms.

    This is the whole authorization model: a secret with no policy is reachable
    by nobody, and ``subjects`` is an explicit allowlist rather than a deny-list,
    so a new agent starts with access to nothing. ``max_grant_s`` is the ceiling
    on a "leave it open for a while" answer — 0 means no window may be opened at
    all and every single use is asked about."""

    name: str
    approval: str  # APPROVAL_ALWAYS | APPROVAL_NEVER, from wardline.wire
    max_grant_s: int
    subjects: str  # CSV of agent names; "" = nobody
    description: str
    created_at: str
    updated_at: str
    # Commands this secret may be taken for without asking anyone, encoded by
    # `autorules.encode` — "" means every use is asked about. A rule narrows the
    # automatic case; it never widens who may ask, which is `subjects` above.
    auto_commands: str = ""

    def allows(self, agent: str) -> bool:
        return agent in {s.strip() for s in self.subjects.split(",") if s.strip()}

    @property
    def rules(self) -> tuple[tuple[str, ...], ...]:
        return decode(self.auto_commands)


class SecretPolicyStore(Protocol):
    """What the broker and the operator routes need of the policies. A Protocol
    so the broker's tests can hand it a dictionary instead of a file."""

    async def get_policy(self, name: str) -> SecretPolicyRecord | None: ...

    async def list_policies(self) -> list[SecretPolicyRecord]: ...

    async def put_policy(self, policy: SecretPolicyRecord) -> None: ...

    async def delete_policy(self, name: str) -> bool: ...


_POLICIES_SCHEMA = """
-- Timestamps are UTC ISO8601 (seconds), so string order is time order.
CREATE TABLE IF NOT EXISTS secret_policies (
  name          TEXT PRIMARY KEY,
  approval      TEXT NOT NULL,
  max_grant_s   INTEGER NOT NULL DEFAULT 0,
  subjects      TEXT NOT NULL DEFAULT '',
  description   TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  auto_commands TEXT NOT NULL DEFAULT ''
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

    def _create_policy_tables(self) -> None:
        self._conn.executescript(_POLICIES_SCHEMA)
        # Empty is "ask every time", so a policy written before rules existed
        # keeps behaving exactly as it did.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(secret_policies)")}
        if "auto_commands" not in columns:
            self._conn.execute(
                "ALTER TABLE secret_policies ADD COLUMN auto_commands TEXT NOT NULL DEFAULT ''"
            )

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


class WardStore(SqliteSessionStore, SecretPolicyStoreMixin):
    """ward's database: the library's store plus the policies.

    The library's half is not decoration — the windows and the ledger are where
    a human's answer is recorded, and the interactions tables are how a click
    finds the request it answers. What ward adds is the one thing the engine
    must not have.
    """

    def _create_app_tables(self) -> None:
        self._create_policy_tables()
