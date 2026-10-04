"""A collection prefix on the server backend (crucible/store/mongo/base.py).

Two engines on one database, or an engine beside an application's own
collections, must not read each other's `sessions`. The prefix is applied in
exactly one place — the collection view every backend method goes through — so
these tests pin that place and the factory that feeds it. The one test that
needs a server is gated like the conformance suite (`make test-mongo`).
"""

import os
import uuid

import pytest

from crucible.ports.chat.types import KIND_DM
from crucible.store import open_store
from crucible.store.sqlite import SqliteSessionStore
from tests.conftest import MONGO_URL_ENV, MongoBackend

pymongo = pytest.importorskip("pymongo")


def test_the_prefix_goes_on_every_logical_name_in_one_place() -> None:
    from crucible.store.mongo.base import SESSIONS, TASKS, Collections

    db = pymongo.AsyncMongoClient("mongodb://localhost:1")["inventory"]  # never connects
    plain, shared = Collections(db), Collections(db, "engine_")
    assert plain[SESSIONS].name == "sessions"
    assert shared[SESSIONS].name == "engine_sessions" and shared[TASKS].name == "engine_tasks"
    assert shared.prefix == "engine_"


def test_the_factory_hands_the_prefix_to_the_server_backend_and_sqlite_ignores_it(
    tmp_path,
) -> None:
    from crucible.store.mongo.base import SESSIONS

    store = open_store("mongo", name="inventory", url="mongodb://localhost:1", prefix="a_")
    assert store._db[SESSIONS].name == "a_sessions"  # type: ignore[attr-defined]

    file_store = open_store("sqlite", name=tmp_path / "db.sqlite", prefix="a_")
    assert isinstance(file_store, SqliteSessionStore)


@pytest.mark.parametrize("bad", ["with space", "a.b", "a$b", "x/y"])
def test_a_prefix_that_would_need_quoting_is_refused_at_composition(bad: str) -> None:
    from crucible.store.mongo import MongoSessionStore

    with pytest.raises(ValueError, match="DB_COLLECTION_PREFIX"):
        open_store("mongo", name="inventory", url="mongodb://localhost:1", prefix=bad)
    with pytest.raises(ValueError, match="letters, digits"):
        MongoSessionStore("mongodb://localhost:1", "inventory", prefix=bad)


@pytest.mark.skipif(not os.environ.get(MONGO_URL_ENV), reason="needs a Mongo server")
async def test_two_engines_on_one_database_do_not_see_each_other() -> None:
    database = f"crucible_test_{uuid.uuid4().hex}"
    url = os.environ[MONGO_URL_ENV]
    one, two = MongoBackend(url, database, "one_"), MongoBackend(url, database, "two_")
    a, b = one.open(), two.open()
    try:
        await a.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        assert [r.conversation_id for r in await a.list()] == ["dm1"]
        assert await b.list() == []
        await b.get_or_create("assistant", "dm2", "dm2", KIND_DM)
        assert [r.conversation_id for r in await a.list()] == ["dm1"]

        with pymongo.MongoClient(url) as client:
            names = set(client[database].list_collection_names())
        assert {"one_sessions", "two_sessions"} <= names
        assert "sessions" not in names  # nothing landed under the plain name
    finally:
        await a.close()
        await b.close()
        one.cleanup()
