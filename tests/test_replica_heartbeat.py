"""A replica says it is alive by holding its lease; stopping lets it go."""

import asyncio

from crucible.ports.replica import Replica
from crucible.replicas import ReplicaHeartbeat


class _Leases:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def acquire_lease(self, name: str, owner: str, *, ttl_s: float) -> bool:
        self.calls.append(("acquire", name, owner))
        return True

    async def release_lease(self, name: str, owner: str) -> None:
        self.calls.append(("release", name, owner))


async def test_the_heartbeat_renews_the_replicas_lease_and_releases_it_on_stop() -> None:
    leases = _Leases()
    ticks = asyncio.Event()

    async def sleep(_: float) -> None:
        ticks.set()
        await asyncio.sleep(0)

    beat = ReplicaHeartbeat(leases, Replica("a1", "http://pod-a:8428"), sleep=sleep)
    task = asyncio.create_task(beat.run())
    await ticks.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    beat.stop()
    await task
    assert leases.calls[0] == ("acquire", "replica:a1", "http://pod-a:8428")
    assert leases.calls.count(("acquire", "replica:a1", "http://pod-a:8428")) >= 2  # renewed
    assert leases.calls[-1] == ("release", "replica:a1", "http://pod-a:8428")
