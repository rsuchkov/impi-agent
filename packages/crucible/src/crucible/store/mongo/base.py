"""Shared plumbing for the MongoDB backend: document conversion, collection
names, and the lazy index build.

Records go in as their own fields — the dataclasses are already flat documents,
which is why this backend needed no reshaping of the port. What differs from
SQLite is written down where it happens, not smoothed over: two places (the
claim and the skip) buy atomicity with an ordering trick instead of a
transaction, and both say so.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import Any, TypeVar

from pymongo.asynchronous.database import AsyncDatabase

T = TypeVar("T")

SESSIONS = "sessions"
INTERACTIONS = "pending_interactions"
FORMS = "pending_forms"
PROCESSED = "processed_posts"
AGENTS = "agents"
TASKS = "tasks"
RUNS = "task_runs"
HEARTBEAT = "scheduler_heartbeat"
GRANTS = "approval_grants"
AUDIT = "approval_audit"

# The heartbeat is one document by construction, the way it is one row in
# SQLite: a reader asks for THE beat, so there is nothing to choose between.
HEARTBEAT_ID = 1


def to_doc(record: object, **extra: Any) -> dict[str, Any]:
    """A frozen record as a document. Field names are kept verbatim so a person
    reading the collection sees the same names as in the code."""
    doc = {f.name: getattr(record, f.name) for f in fields(record)}  # type: ignore[arg-type]
    doc.update(extra)
    return doc


def from_doc(cls: type[T], doc: dict[str, Any]) -> T:
    """A document back as a record, ignoring whatever else the document carries
    (`_id`, and any field a newer engine added). Reading by an explicit field
    list is what lets an older engine keep working against a newer collection —
    the same promise the SQLite backend makes by naming its columns."""
    return cls(**{f.name: doc[f.name] for f in fields(cls)})  # type: ignore[arg-type,call-arg]


async def create_indexes(db: AsyncDatabase) -> None:
    """Every uniqueness the SQLite schema states as a constraint.

    These are not an optimisation. Each one is load-bearing: they are what makes
    `get_or_create` idempotent, a replayed post a no-op, and an occurrence
    unable to fire twice. A deployment that lost them would not slow down — it
    would double-fire.
    """
    await db[SESSIONS].create_index([("agent", 1), ("conversation_id", 1)], unique=True)
    await db[SESSIONS].create_index([("runtime_session_id", 1)])
    await db[SESSIONS].create_index([("last_active", -1)])
    await db[AGENTS].create_index([("name", 1)], unique=True)
    await db[TASKS].create_index([("agent", 1), ("name", 1)])
    await db[TASKS].create_index([("state", 1), ("due_at", 1)])
    await db[TASKS].create_index([("state", 1), ("next_run_at", 1)])
    await db[RUNS].create_index([("run_id", 1)], unique=True)
    # Belt and braces behind the claim's compare-and-swap, exactly as in SQLite:
    # one occurrence of one task gets one run, whatever the callers do.
    await db[RUNS].create_index([("task_id", 1), ("scheduled_at", 1)], unique=True)
    await db[RUNS].create_index([("notified", 1), ("status", 1), ("finished_at", 1)])
    await db[GRANTS].create_index([("kind", 1), ("principal", 1), ("scope", 1)])
    await db[AUDIT].create_index([("at", -1)])


class MongoBase:
    """Holds the connection and builds the indexes once, on first use.

    The indexes cannot be built in ``__init__``: creating them is I/O, the
    constructor is synchronous, and the store is built during composition —
    before there is a loop to run them on. So the first operation pays for
    them, under a lock so a burst of concurrent first operations builds them
    once rather than ten times.
    """

    _db: AsyncDatabase
    _indexed: bool
    _index_lock: asyncio.Lock

    async def _ready(self) -> AsyncDatabase:
        if self._indexed:
            return self._db
        async with self._index_lock:
            if not self._indexed:
                await create_indexes(self._db)
                self._indexed = True
        return self._db
