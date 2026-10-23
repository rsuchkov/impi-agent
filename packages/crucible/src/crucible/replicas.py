"""ReplicaHeartbeat: how a replica says it is alive.

The lease ``replica:<id>`` (``ports.replica``), held with the replica's own
address and renewed while the process runs: the other replicas forward a
conversation's turns to the address the lease names, and take the
conversation over once the lease has lapsed. Released on stop, so a replica
that shuts down cleanly hands its conversations over at once rather than after
a timeout.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from crucible.ports.lease import Lease
from crucible.ports.replica import REPLICA_LEASE_PREFIX, Replica

logger = logging.getLogger(__name__)

# Alive for this long after the last renewal; renewed this often. A replica
# that misses two renewals is taken for gone.
REPLICA_TTL_S = 30.0
REPLICA_RENEW_S = 10.0


class ReplicaHeartbeat:
    def __init__(
        self, leases: Lease, replica: Replica, *,
        ttl_s: float = REPLICA_TTL_S, renew_s: float = REPLICA_RENEW_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._leases = leases
        self._replica = replica
        self._name = REPLICA_LEASE_PREFIX + replica.id
        self._ttl_s = ttl_s
        self._renew_s = renew_s
        self._sleep = sleep
        self._stopped = False

    async def beat(self) -> None:
        """Say we are alive, once."""
        if not await self._leases.acquire_lease(self._name, self._replica.url, ttl_s=self._ttl_s):
            # Another process with the same id: the ids are drawn at random and
            # long enough that this is a configuration slip, not a race.
            logger.error("replica %s: another process holds our lease", self._replica.id)

    async def run(self) -> None:
        """Renew until stopped, then let the lease go."""
        try:
            while not self._stopped:
                await self.beat()
                await self._sleep(self._renew_s)
        finally:
            await self._leases.release_lease(self._name, self._replica.url)

    def stop(self) -> None:
        self._stopped = True
