"""The MongoDB implementation of the store port.

Imported only when a deployment asks for it — see the factory in
``crucible.store`` — so the ``pymongo`` dependency stays optional for everyone
who runs on the default SQLite.
"""

from crucible.store.mongo.sessions import MongoSessionStore

__all__ = ["MongoSessionStore"]
