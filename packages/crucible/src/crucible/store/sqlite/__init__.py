"""The SQLite backend: the inventory in one file, no server, no dependency.

One class composes the facets — sessions, tasks, approvals, traces — over a
single connection; each facet owns its own schema. One module per port facet,
the same layout as ``store/mongo/``.
"""

from crucible.store.sqlite.sessions import SqliteSessionStore

__all__ = ["SqliteSessionStore"]
