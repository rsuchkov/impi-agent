"""Bot-side persistent state: the session inventory, the agent registry,
scheduled work, approvals and processed-post dedup.

The store is an INVENTORY, not the source of truth — conversation memory lives
in the runtime's own session files. That is worth holding on to when choosing a
backend: moving this to a server makes the ENGINE able to lose its disk, and
does nothing for the memory, which still lives wherever the runtime writes.

Two backends implement the same port. SQLite is the default and needs nothing
installed; MongoDB is for a deployment that wants its state outside the process
and pulls in an optional dependency to get it.
"""

from pathlib import Path

from crucible.store.base import (
    MONGO,
    SQLITE,
    STORE_BACKENDS,
    ApprovalAudit,
    ApprovalGrant,
    ApprovalStore,
    InteractionRecord,
    InteractionStore,
    SchedulerHeartbeat,
    SchedulerStateStore,
    SessionRecord,
    SessionStore,
    Store,
    TaskRecord,
    TaskRunRecord,
    TaskStore,
    derive_runtime_session_id,
)
from crucible.store.sessions import SqliteSessionStore


def open_store(backend: str = SQLITE, *, name: str | Path = "", url: str = "") -> Store:
    """Build the inventory a deployment asked for.

    Two arguments, and the backend decides what they mean: ``name`` is a file
    path on SQLite and a database name on MongoDB, ``url`` is where the server
    is and is meaningless without one. That is the same shape the settings have,
    on purpose — a deployment names its database once and says what kind it is,
    rather than filling in one set of keys and leaving another set empty.

    Plain arguments rather than a settings object: the library must not know the
    shape of the application's configuration.

    A bad backend name, a missing URL, or Mongo without its package fails HERE —
    at composition, with a sentence saying what to do. The alternative is an
    ImportError out of the middle of a turn, or worse, a store that opens a
    SQLite file nobody writes and reports an empty stand.
    """
    if backend == SQLITE:
        return SqliteSessionStore(name)
    if backend != MONGO:
        raise ValueError(
            f"unknown store backend {backend!r} — "
            f"expected one of {', '.join(STORE_BACKENDS)}"
        )
    if not url:
        raise ValueError("the mongo store backend needs a connection URL (DB_URL)")
    try:
        # Deferred on purpose, and the one place in the library that is. Naming
        # `crucible.store.mongo` at the top would import pymongo for everyone,
        # which is the whole thing the extra exists to avoid. The two harms the
        # rule guards against do not apply: this crosses no layer (it is the
        # same package, so import-linter loses nothing), and the ImportError is
        # not a runtime surprise — it is caught here and answered with the
        # install command, at composition, before the engine serves anything.
        from crucible.store.mongo import MongoSessionStore  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on how it was installed
        raise RuntimeError(
            "the mongo store backend needs the 'mongo' extra: "
            "install crucible[mongo] (or add pymongo>=4.9)"
        ) from exc
    return MongoSessionStore(url, str(name))

__all__ = [
    "SessionRecord",
    "SessionStore",
    "Store",
    "InteractionRecord",
    "InteractionStore",
    "SchedulerHeartbeat",
    "SchedulerStateStore",
    "ApprovalAudit",
    "ApprovalGrant",
    "ApprovalStore",
    "TaskRecord",
    "TaskRunRecord",
    "TaskStore",
    "SqliteSessionStore",
    "open_store",
    "STORE_BACKENDS",
    "SQLITE",
    "MONGO",
    "derive_runtime_session_id",
]
