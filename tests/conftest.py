"""Shared fixtures. The store backends live here because they are the one thing
several test files need to agree on.

A second store implementation is only trustworthy if it answers the same
questions the first one does, so the behaviour tests run against every backend
this environment can reach rather than against the one that happens to be
easiest. What stays backend-specific — a PRAGMA, a transaction, a file being
reopened — belongs in that backend's own test file, not here.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from crucible.store.base import Store
from crucible.store.sessions import SqliteSessionStore

# A Mongo backend needs a real server: the claim protocol rests on atomicity
# that only a server provides, and a fake would agree with us for the wrong
# reason. Tests here are offline by contract, so the Mongo pass is opt-in —
# `make test-mongo` starts one and sets this.
MONGO_URL_ENV = "MONGO_TEST_URL"


class StoreBackend:
    """Opens stores over ONE underlying storage, so a test can close a store and
    reopen it and still be looking at the same data."""

    name: str

    def open(self) -> Store:
        raise NotImplementedError

    def cleanup(self) -> None:
        return


class SqliteBackend(StoreBackend):
    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self) -> Store:
        return SqliteSessionStore(self._path)


class MongoBackend(StoreBackend):
    name = "mongo"

    def __init__(self, url: str, database: str) -> None:
        self._url = url
        self._database = database

    def open(self) -> Store:
        from crucible.store.mongo import MongoSessionStore

        return MongoSessionStore(self._url, self._database)

    def cleanup(self) -> None:
        from pymongo import MongoClient

        with MongoClient(self._url) as client:
            client.drop_database(self._database)


def _backends() -> list[str]:
    names = ["sqlite"]
    if os.environ.get(MONGO_URL_ENV):
        names.append("mongo")
    return names


@pytest.fixture(params=_backends())
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreBackend]:
    """The backend under test. Parameterised, so every behaviour test in the
    conformance files runs once per backend the environment can reach."""
    if request.param == "sqlite":
        backend: StoreBackend = SqliteBackend(tmp_path / "db.sqlite")
    else:
        # A database per test: these tests create, claim and delete by fixed
        # ids, and sharing one would make them depend on each other's order.
        backend = MongoBackend(
            os.environ[MONGO_URL_ENV], f"crucible_test_{uuid.uuid4().hex}"
        )
    try:
        yield backend
    finally:
        backend.cleanup()


@pytest.fixture
async def store(stores: StoreBackend) -> AsyncIterator[Store]:
    """An open store, closed afterwards. Most tests want only this."""
    opened = stores.open()
    try:
        yield opened
    finally:
        await opened.close()
