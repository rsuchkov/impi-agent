"""What the MCP gateway's authorization server issues — the McpGrantStore
facet of the SQLite store: authorizations in progress and the grants they
came to, keyed by digests of the tokens, never the tokens."""

import asyncio
from dataclasses import fields

from crucible.ports.mcp_grants import McpAuthRequest, McpGrant
from crucible.store.sqlite.base import SqliteBase

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_auth_requests (
  id              TEXT PRIMARY KEY,
  client          TEXT NOT NULL,
  redirect_uri    TEXT NOT NULL,
  code_challenge  TEXT NOT NULL,
  scope           TEXT NOT NULL DEFAULT '',
  resource        TEXT NOT NULL DEFAULT '',
  state           TEXT NOT NULL DEFAULT '',
  expires_at      TEXT NOT NULL,
  secret          TEXT NOT NULL,
  subject         TEXT NOT NULL DEFAULT '',
  username        TEXT NOT NULL DEFAULT '',
  code_hash       TEXT NOT NULL DEFAULT '',
  code_expires_at TEXT NOT NULL DEFAULT '',
  browser_hash    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_auth_requests_code ON mcp_auth_requests (code_hash);

CREATE TABLE IF NOT EXISTS mcp_grants (
  id                    TEXT PRIMARY KEY,
  subject               TEXT NOT NULL,
  username              TEXT NOT NULL DEFAULT '',
  client                TEXT NOT NULL,
  scope                 TEXT NOT NULL DEFAULT '',
  signed_in_at          TEXT NOT NULL,
  expires_at            TEXT NOT NULL,
  access_hash           TEXT NOT NULL,
  access_expires_at     TEXT NOT NULL,
  refresh_hash          TEXT NOT NULL,
  previous_refresh_hash TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_grants_access ON mcp_grants (access_hash);
CREATE INDEX IF NOT EXISTS idx_mcp_grants_refresh ON mcp_grants (refresh_hash);
CREATE INDEX IF NOT EXISTS idx_mcp_grants_previous ON mcp_grants (previous_refresh_hash);
"""

_REQUEST_FIELDS = tuple(f.name for f in fields(McpAuthRequest))
_REQUEST_COLUMNS = ", ".join(_REQUEST_FIELDS)
_GRANT_FIELDS = tuple(f.name for f in fields(McpGrant))
_GRANT_COLUMNS = ", ".join(_GRANT_FIELDS)


def _request(row: tuple) -> McpAuthRequest:
    return McpAuthRequest(**dict(zip(_REQUEST_FIELDS, row, strict=True)))


def _grant(row: tuple) -> McpGrant:
    return McpGrant(**dict(zip(_GRANT_FIELDS, row, strict=True)))


def _values(record: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, name) for name in names)


class McpGrantStoreMixin(SqliteBase):
    """The McpGrantStore facet of the SQLite store."""

    def _create_mcp_grant_tables(self) -> None:
        self._conn.executescript(_SCHEMA)

    # -- sync core -------------------------------------------------------------

    def put_mcp_request_sync(self, record: McpAuthRequest) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO mcp_auth_requests ({_REQUEST_COLUMNS}) "
                f"VALUES ({', '.join('?' * len(_REQUEST_FIELDS))})",
                _values(record, _REQUEST_FIELDS),
            )
            self._conn.commit()

    def get_mcp_request_sync(self, request_id: str) -> McpAuthRequest | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM mcp_auth_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _request(row) if row is not None else None

    def take_mcp_request_by_code_sync(self, code_hash: str) -> McpAuthRequest | None:
        if not code_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM mcp_auth_requests WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM mcp_auth_requests WHERE id = ?", (row[0],))
            self._conn.commit()
        return _request(row)

    def delete_mcp_request_sync(self, request_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM mcp_auth_requests WHERE id = ?", (request_id,))
            self._conn.commit()

    def save_mcp_grant_sync(self, record: McpGrant) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO mcp_grants ({_GRANT_COLUMNS}) VALUES ({', '.join('?' * len(_GRANT_FIELDS))})",
                _values(record, _GRANT_FIELDS),
            )
            self._conn.commit()

    def replace_mcp_grant_sync(self, record: McpGrant, *, expected_refresh_hash: str) -> bool:
        assignments = ", ".join(f"{name} = ?" for name in _GRANT_FIELDS if name != "id")
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE mcp_grants SET {assignments} WHERE id = ? AND refresh_hash = ?",
                (*(getattr(record, name) for name in _GRANT_FIELDS if name != "id"), record.id, expected_refresh_hash),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def get_mcp_grant_by_access_sync(self, access_hash: str) -> McpGrant | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_GRANT_COLUMNS} FROM mcp_grants WHERE access_hash = ?", (access_hash,)
            ).fetchone()
        return _grant(row) if row is not None else None

    def get_mcp_grant_by_refresh_sync(self, refresh_hash: str) -> McpGrant | None:
        if not refresh_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_GRANT_COLUMNS} FROM mcp_grants WHERE refresh_hash = ? OR previous_refresh_hash = ?",
                (refresh_hash, refresh_hash),
            ).fetchone()
        return _grant(row) if row is not None else None

    def delete_mcp_grant_sync(self, grant_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM mcp_grants WHERE id = ?", (grant_id,))
            self._conn.commit()

    def purge_mcp_auth_sync(self, now: str) -> int:
        with self._lock:
            gone = self._conn.execute("DELETE FROM mcp_auth_requests WHERE expires_at <= ?", (now,)).rowcount
            gone += self._conn.execute("DELETE FROM mcp_grants WHERE expires_at <= ?", (now,)).rowcount
            self._conn.commit()
        return gone

    # -- async facade (McpGrantStore port) ------------------------------------

    async def put_mcp_request(self, record: McpAuthRequest) -> None:
        await asyncio.to_thread(self.put_mcp_request_sync, record)

    async def get_mcp_request(self, request_id: str) -> McpAuthRequest | None:
        return await asyncio.to_thread(self.get_mcp_request_sync, request_id)

    async def take_mcp_request_by_code(self, code_hash: str) -> McpAuthRequest | None:
        return await asyncio.to_thread(self.take_mcp_request_by_code_sync, code_hash)

    async def delete_mcp_request(self, request_id: str) -> None:
        await asyncio.to_thread(self.delete_mcp_request_sync, request_id)

    async def save_mcp_grant(self, record: McpGrant) -> None:
        await asyncio.to_thread(self.save_mcp_grant_sync, record)

    async def replace_mcp_grant(self, record: McpGrant, *, expected_refresh_hash: str) -> bool:
        return await asyncio.to_thread(
            self.replace_mcp_grant_sync, record, expected_refresh_hash=expected_refresh_hash
        )

    async def get_mcp_grant_by_access(self, access_hash: str) -> McpGrant | None:
        return await asyncio.to_thread(self.get_mcp_grant_by_access_sync, access_hash)

    async def get_mcp_grant_by_refresh(self, refresh_hash: str) -> McpGrant | None:
        return await asyncio.to_thread(self.get_mcp_grant_by_refresh_sync, refresh_hash)

    async def delete_mcp_grant(self, grant_id: str) -> None:
        await asyncio.to_thread(self.delete_mcp_grant_sync, grant_id)

    async def purge_mcp_auth(self, now: str) -> int:
        return await asyncio.to_thread(self.purge_mcp_auth_sync, now)
