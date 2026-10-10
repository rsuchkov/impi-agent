"""People's connected accounts and the connects they have started — the
ConnectionStore and ConnectionLinkStore facets of the SQLite store.

The tokens arrive sealed and stay opaque here. The refresh lease is a column
beside the record: claimed by an UPDATE that matches only while the record is
still the credential the caller saw and no lease runs, so of two claims at
once exactly one changes a row.
"""

import asyncio
from dataclasses import fields

from crucible.store import clock
from crucible.store.base import ConnectionLinkRecord, ConnectionRecord
from crucible.store.sqlite.base import SqliteBase

_CONNECTIONS_SCHEMA = """
-- One per person and provider. lease_until is '' while nobody refreshes: ''
-- sorts before every timestamp, so a lapsed lease and none read alike.
CREATE TABLE IF NOT EXISTS connections (
  owner            TEXT NOT NULL,
  provider         TEXT NOT NULL,
  access_sealed    TEXT NOT NULL,
  refresh_sealed   TEXT NOT NULL DEFAULT '',
  expires_at       TEXT NOT NULL DEFAULT '',
  account_id       TEXT NOT NULL DEFAULT '',
  account_username TEXT NOT NULL DEFAULT '',
  account_name     TEXT NOT NULL DEFAULT '',
  account_email    TEXT NOT NULL DEFAULT '',
  scope            TEXT NOT NULL DEFAULT '',
  connect_id       TEXT NOT NULL,
  version          INTEGER NOT NULL DEFAULT 0,
  lease_until      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (owner, provider)
);

-- Single use, short-lived; keyed by a digest of the state, never the state.
CREATE TABLE IF NOT EXISTS connection_links (
  state_digest    TEXT PRIMARY KEY,
  owner           TEXT NOT NULL,
  provider        TEXT NOT NULL,
  verifier_sealed TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  return_to       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_connection_links_expires ON connection_links (expires_at);
"""

# Column order comes from the dataclasses, so SQL and records cannot drift.
_FIELDS = tuple(f.name for f in fields(ConnectionRecord))
_COLUMNS = ", ".join(_FIELDS)
_LINK_FIELDS = tuple(f.name for f in fields(ConnectionLinkRecord))
_LINK_COLUMNS = ", ".join(_LINK_FIELDS)


def _record(row: tuple) -> ConnectionRecord:
    return ConnectionRecord(**dict(zip(_FIELDS, row, strict=True)))


def _link(row: tuple) -> ConnectionLinkRecord:
    return ConnectionLinkRecord(**dict(zip(_LINK_FIELDS, row, strict=True)))


def _values(record: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, name) for name in names)


def _credential_clause(connect_id: str | None, version: int | None) -> tuple[str, list]:
    clause, args = "", []
    if connect_id is not None:
        clause += " AND connect_id = ?"
        args.append(connect_id)
    if version is not None:
        clause += " AND version = ?"
        args.append(version)
    return clause, args


class ConnectionStoreMixin(SqliteBase):
    """The ConnectionStore and ConnectionLinkStore facets of the SQLite store."""

    def _create_connection_tables(self) -> None:
        self._conn.executescript(_CONNECTIONS_SCHEMA)

    # -- connections ------------------------------------------------------------

    def get_connection_sync(self, owner: str, provider: str) -> ConnectionRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM connections WHERE owner = ? AND provider = ?",
                (owner, provider),
            ).fetchone()
        return _record(row) if row else None

    def list_connections_sync(self, owner: str) -> list[ConnectionRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM connections WHERE owner = ? ORDER BY provider", (owner,),
            ).fetchall()
        return [_record(row) for row in rows]

    def save_connection_sync(self, record: ConnectionRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO connections ({_COLUMNS}, lease_until) "
                f"VALUES ({', '.join('?' * len(_FIELDS))}, '')",
                _values(record, _FIELDS),
            )
            self._conn.commit()

    def claim_refresh_sync(
        self, owner: str, provider: str, *, connect_id: str, version: int, now: str, until: str
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE connections SET lease_until = ? "
                "WHERE owner = ? AND provider = ? AND connect_id = ? AND version = ? "
                "AND lease_until <= ?",
                (until, owner, provider, connect_id, version, now),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def release_refresh_sync(self, owner: str, provider: str, *, connect_id: str, version: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE connections SET lease_until = '' "
                "WHERE owner = ? AND provider = ? AND connect_id = ? AND version = ?",
                (owner, provider, connect_id, version),
            )
            self._conn.commit()

    def replace_connection_sync(self, record: ConnectionRecord, *, expected_version: int) -> bool:
        assignments = ", ".join(f"{name} = ?" for name in _FIELDS)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE connections SET {assignments}, lease_until = '' "
                "WHERE owner = ? AND provider = ? AND connect_id = ? AND version = ?",
                (*_values(record, _FIELDS), record.owner, record.provider, record.connect_id, expected_version),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def delete_connection_sync(
        self, owner: str, provider: str, *, connect_id: str | None = None, version: int | None = None
    ) -> bool:
        clause, args = _credential_clause(connect_id, version)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM connections WHERE owner = ? AND provider = ?{clause}",
                (owner, provider, *args),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    # -- links ----------------------------------------------------------------------

    def put_link_sync(self, link: ConnectionLinkRecord) -> None:
        with self._lock:
            # Links nobody came back for go when the next one is put: nothing
            # else reads them, and a sweep of its own would be one more loop.
            self._conn.execute("DELETE FROM connection_links WHERE expires_at <= ?", (clock.now_iso(),))
            self._conn.execute(
                f"INSERT INTO connection_links ({_LINK_COLUMNS}) VALUES ({', '.join('?' * len(_LINK_FIELDS))})",
                _values(link, _LINK_FIELDS),
            )
            self._conn.commit()

    def take_link_sync(self, state_digest: str) -> ConnectionLinkRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_LINK_COLUMNS} FROM connection_links WHERE state_digest = ?", (state_digest,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM connection_links WHERE state_digest = ?", (state_digest,))
            self._conn.commit()
        return _link(row)

    # -- async ------------------------------------------------------------------------

    async def get_connection(self, owner: str, provider: str) -> ConnectionRecord | None:
        return await asyncio.to_thread(self.get_connection_sync, owner, provider)

    async def list_connections(self, owner: str) -> list[ConnectionRecord]:
        return await asyncio.to_thread(self.list_connections_sync, owner)

    async def save_connection(self, record: ConnectionRecord) -> None:
        await asyncio.to_thread(self.save_connection_sync, record)

    async def claim_refresh(
        self, owner: str, provider: str, *, connect_id: str, version: int, now: str, until: str
    ) -> bool:
        return await asyncio.to_thread(
            lambda: self.claim_refresh_sync(
                owner, provider, connect_id=connect_id, version=version, now=now, until=until,
            )
        )

    async def release_refresh(self, owner: str, provider: str, *, connect_id: str, version: int) -> None:
        await asyncio.to_thread(
            lambda: self.release_refresh_sync(owner, provider, connect_id=connect_id, version=version)
        )

    async def replace_connection(self, record: ConnectionRecord, *, expected_version: int) -> bool:
        return await asyncio.to_thread(
            lambda: self.replace_connection_sync(record, expected_version=expected_version)
        )

    async def delete_connection(
        self, owner: str, provider: str, *, connect_id: str | None = None, version: int | None = None
    ) -> bool:
        return await asyncio.to_thread(
            lambda: self.delete_connection_sync(owner, provider, connect_id=connect_id, version=version)
        )

    async def put_link(self, link: ConnectionLinkRecord) -> None:
        await asyncio.to_thread(self.put_link_sync, link)

    async def take_link(self, state_digest: str) -> ConnectionLinkRecord | None:
        return await asyncio.to_thread(self.take_link_sync, state_digest)
