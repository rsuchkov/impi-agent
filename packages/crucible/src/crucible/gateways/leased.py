"""LeasedGateway: a gateway that runs in exactly one process of a deployment.

A chat platform that pushes events over a connection the engine opens — Slack's
Socket Mode, a bot's websocket — hands each event to one of the open
connections by its own choice, not by the person it concerns. Two engine
replicas each holding such a connection would split one conversation's
messages between two processes, each with its own runtime memory and its own
waiting cards. So the connection is opened only by the replica that holds the
gateway's lease (``ports.lease.Leases``, which the store implements): the others wait, and take over when the
holder stops renewing — a crash, a rollout. Sending needs no connection and
works from every replica, so answers, cards and widgets are posted from
wherever the turn runs.

Transparent to the composition root: it is a ``Gateway`` like the one it wraps,
and ``run`` returns when the wrapped gateway's ``run`` does.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from crucible.ports.chat.gateway import AgentIdentity, Gateway
from crucible.ports.lease import Leases

logger = logging.getLogger(__name__)

# How long a lease is taken for, and how often it is renewed while held — and
# how often a waiting replica asks again. A holder that misses two renewals in a
# row has lost the lease; a waiting one takes over within one ttl of that.
LEASE_TTL_S = 30.0
LEASE_RENEW_S = 10.0


class LeasedGateway:
    def __init__(
        self, gateway: Gateway, leases: Leases, *, name: str, owner: str,
        ttl_s: float = LEASE_TTL_S, renew_s: float = LEASE_RENEW_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.gateway = gateway  # the one that talks to the platform
        self._leases = leases
        self._name = name
        self._owner = owner
        self._ttl_s = ttl_s
        self._renew_s = renew_s
        self._sleep = sleep
        self._stopped = False
        self.held = False  # whether this process holds the lease right now

    async def login(self) -> AgentIdentity:
        # Who the agent is on the platform is the same whichever replica asks,
        # and asking needs no connection.
        return await self.gateway.login()

    async def run(self) -> None:
        """Hold the lease and run the gateway while it is held; wait for it
        otherwise. Returns when the gateway's own ``run`` returns or ``stop``
        was called."""
        while not self._stopped:
            if not await self._leases.acquire_lease(self._name, self._owner, ttl_s=self._ttl_s):
                await self._sleep(self._renew_s)
                continue
            self.held = True
            logger.info("lease %s taken by %s: the gateway runs here", self._name, self._owner)
            running = asyncio.ensure_future(self.gateway.run())
            keeping = asyncio.ensure_future(self._keep())
            try:
                done, _ = await asyncio.wait({running, keeping}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # Shutdown. Both tasks end here, the renewal first of all: one
                # still in flight when ``stop`` releases the lease would take
                # it again, and the next process would wait a whole ttl for it.
                running.cancel()
                keeping.cancel()
                await asyncio.gather(running, keeping, return_exceptions=True)
                raise
            self.held = False
            if running in done:
                keeping.cancel()
                await self._leases.release_lease(self._name, self._owner)
                running.result()  # a crash is the gateway's own, and the supervisor's to see
                return
            # The lease was lost (or stop() asked): close the connection, then
            # queue up again unless we were told to stop.
            running.cancel()
            try:
                await self.gateway.stop()
            except Exception:
                logger.warning("gateway %s did not stop cleanly after losing its lease", self._name, exc_info=True)
            if self._stopped:
                return
            logger.warning("lease %s lost by %s: the gateway stops here until it is ours again", self._name, self._owner)

    async def _keep(self) -> None:
        """Renew until the lease is refused or ``stop`` is called."""
        while not self._stopped:
            await self._sleep(self._renew_s)
            if self._stopped:
                return
            if not await self._leases.acquire_lease(self._name, self._owner, ttl_s=self._ttl_s):
                return

    async def stop(self) -> None:
        self._stopped = True
        # The wrapped gateway's stop is asked for whether or not it ran: a
        # client it opened at login is its to close.
        await self.gateway.stop()
        if self.held:
            await self._leases.release_lease(self._name, self._owner)
            self.held = False
