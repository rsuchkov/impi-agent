"""What every SQLite facet shares: one connection and the lock that guards it.

Declared here and created by the composing store's ``__init__``. A facet is a
mixin over that connection, and this is the one place that says what a facet
may assume it has been given — the same shape ``mongo/base.py`` gives its own
facets with the client and the lazily built indexes.
"""

import sqlite3
import threading


class SqliteBase:
    _conn: sqlite3.Connection
    _lock: threading.Lock
