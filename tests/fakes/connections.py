"""Connected accounts kept in memory, for tests: the same semantics as the
real stores — the lease, the conditional writes, the single-use link."""

from dataclasses import replace

from crucible.store.base import ConnectionLinkRecord, ConnectionRecord


class MemoryConnectionStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ConnectionRecord] = {}
        self._leases: dict[tuple[str, str], str] = {}

    async def get_connection(self, owner: str, provider: str) -> ConnectionRecord | None:
        return self._records.get((owner, provider))

    async def list_connections(self, owner: str) -> list[ConnectionRecord]:
        return sorted((r for (o, _), r in self._records.items() if o == owner), key=lambda r: r.provider)

    async def save_connection(self, record: ConnectionRecord) -> None:
        key = (record.owner, record.provider)
        self._records[key] = record
        self._leases.pop(key, None)

    async def claim_refresh(
        self, owner: str, provider: str, *, connect_id: str, version: int, now: str, until: str
    ) -> bool:
        key = (owner, provider)
        if not self._is(key, connect_id, version):
            return False
        lease = self._leases.get(key)
        if lease is not None and lease > now:
            return False
        self._leases[key] = until
        return True

    async def release_refresh(self, owner: str, provider: str, *, connect_id: str, version: int) -> None:
        key = (owner, provider)
        if self._is(key, connect_id, version):
            self._leases.pop(key, None)

    async def replace_connection(self, record: ConnectionRecord, *, expected_version: int) -> bool:
        key = (record.owner, record.provider)
        if not self._is(key, record.connect_id, expected_version):
            return False
        self._records[key] = replace(record)
        self._leases.pop(key, None)
        return True

    async def delete_connection(
        self, owner: str, provider: str, *, connect_id: str | None = None, version: int | None = None
    ) -> bool:
        key = (owner, provider)
        record = self._records.get(key)
        if record is None:
            return False
        if (connect_id is not None and record.connect_id != connect_id) or (version is not None and record.version != version):
            return False
        del self._records[key]
        self._leases.pop(key, None)
        return True

    def _is(self, key: tuple[str, str], connect_id: str, version: int) -> bool:
        record = self._records.get(key)
        return record is not None and record.connect_id == connect_id and record.version == version


class MemoryLinkStore:
    def __init__(self) -> None:
        self._links: dict[str, ConnectionLinkRecord] = {}

    async def put_link(self, link: ConnectionLinkRecord) -> None:
        self._links[link.state_digest] = link

    async def take_link(self, state_digest: str) -> ConnectionLinkRecord | None:
        return self._links.pop(state_digest, None)
