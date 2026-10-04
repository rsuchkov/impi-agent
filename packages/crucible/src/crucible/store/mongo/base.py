"""Shared plumbing for the MongoDB backend: document conversion, collection
names (and the prefix a deployment may put on them), and the lazy index build.

Records go in as their own fields — the dataclasses are already flat documents,
which is why this backend needed no reshaping of the port. Where the port's
promise costs something particular here it is written down where it happens,
not smoothed over: two places (the claim and the skip) buy atomicity with an
ordering trick instead of a transaction, and both say so.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import fields
from typing import Any, TypeVar

from pymongo.asynchronous.collection import AsyncCollection
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
TRACES = "tool_traces"

# The heartbeat is one document by construction: a reader asks for THE beat,
# so there is nothing to choose between.
HEARTBEAT_ID = 1

# What a collection prefix may be made of. Mongo itself allows more; this keeps
# a prefix something a person can read in a collection list and type into a
# shell without quoting.
_PREFIX = re.compile(r"[A-Za-z0-9_-]*")


def check_prefix(prefix: str) -> str:
    """The prefix, or a ValueError that says what is wrong with it — at
    composition, not from the first query that creates a collection."""
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(
            f"collection prefix {prefix!r} may only contain letters, digits, '_' and '-' "
            "(DB_COLLECTION_PREFIX)"
        )
    return prefix


class Collections:
    """The database seen through a collection-name prefix.

    The names above are the *logical* ones — what the code and the docs say —
    and a deployment that shares one database between several engines (or with
    an application's own collections) gives each a prefix, so ``sessions``
    is stored as ``engine_sessions``. Every access goes through ``__getitem__``
    with a logical name, which is the one place the prefix is applied; the
    backend's methods never see a physical name. Empty prefix = the names as
    they are, which is the default and what every deployment so far has.
    """

    def __init__(self, db: AsyncDatabase, prefix: str = "") -> None:
        self._db = db
        self._prefix = check_prefix(prefix)

    @property
    def prefix(self) -> str:
        return self._prefix

    def __getitem__(self, name: str) -> AsyncCollection:
        return self._db[f"{self._prefix}{name}"]


def to_doc(record: object, **extra: Any) -> dict[str, Any]:
    """A frozen record as a document. Field names are kept verbatim so a person
    reading the collection sees the same names as in the code."""
    doc = {f.name: getattr(record, f.name) for f in fields(record)}  # type: ignore[arg-type]
    doc.update(extra)
    return doc


def from_doc(cls: type[T], doc: dict[str, Any]) -> T:
    """A document back as a record, ignoring whatever else the document carries
    (`_id`, and any field a newer engine added). Reading by an explicit field
    list is what lets an older engine keep working against a newer collection."""
    return cls(**{f.name: doc[f.name] for f in fields(cls)})  # type: ignore[arg-type,call-arg]


async def create_indexes(db: Collections) -> None:
    """Every uniqueness the port relies on, as an index.

    These are not an optimisation. Each one is load-bearing: they are what makes
    `get_or_create` idempotent, a replayed post a no-op, and an occurrence
    unable to fire twice. A deployment that lost them would not slow down — it
    would double-fire.
    """
    await db[SESSIONS].create_index([("agent", 1), ("conversation_id", 1)], unique=True)
    # Looked up on its own by every tool call, so two conversations must never
    # share one. (Deployments from before the constraint drop the plain index
    # by hand first; Mongo will not change an index's options in place.)
    await db[SESSIONS].create_index([("runtime_session_id", 1)], unique=True)
    await db[SESSIONS].create_index([("last_active", -1)])
    await db[AGENTS].create_index([("name", 1)], unique=True)
    await db[TASKS].create_index([("agent", 1), ("name", 1)])
    await db[TASKS].create_index([("state", 1), ("due_at", 1)])
    await db[TASKS].create_index([("state", 1), ("next_run_at", 1)])
    await db[RUNS].create_index([("run_id", 1)], unique=True)
    # Belt and braces behind the claim's compare-and-swap: one occurrence of
    # one task gets one run, whatever the callers do.
    await db[RUNS].create_index([("task_id", 1), ("scheduled_at", 1)], unique=True)
    await db[RUNS].create_index([("notified", 1), ("status", 1), ("finished_at", 1)])
    await db[GRANTS].create_index([("kind", 1), ("principal", 1), ("scope", 1)])
    await db[AUDIT].create_index([("at", -1)])
    await db[TRACES].create_index([("finished_at", 1)])


class MongoBase:
    """Holds the connection and builds the indexes once, on first use.

    The indexes cannot be built in ``__init__``: creating them is I/O, the
    constructor is synchronous, and the store is built during composition —
    before there is a loop to run them on. So the first operation pays for
    them, under a lock so a burst of concurrent first operations builds them
    once rather than ten times.
    """

    _db: Collections
    _indexed: bool
    _index_lock: asyncio.Lock

    async def _ready(self) -> Collections:
        if self._indexed:
            return self._db
        async with self._index_lock:
            if not self._indexed:
                await create_indexes(self._db)
                self._indexed = True
        return self._db
