"""Where an agent's runtime process runs.

One port, two implementations, and the wiring that picks between them per agent:

- :mod:`base`   — the ``RuntimeHost`` port and ``HostRouter``.
- :mod:`local`  — a child of this process. The default, and what a deployment
  that has not turned agent containers on keeps doing.
- :mod:`remote` — a host in the agent's own container, reached over a WebSocket.
- :mod:`wire`   — the vocabulary that connection speaks.

Both implementations honour the same ``SpawnRequest`` and produce the same
command line; a contract test holds them to each other, because an agent that
behaved differently for having moved would make the whole arrangement
untrustworthy.
"""

from crucible.runtimes.pi.hosts.base import HostRouter, RuntimeHost
from crucible.runtimes.pi.hosts.local import LocalHost
from crucible.runtimes.pi.hosts.remote import RemoteHost, RemoteTransport

__all__ = [
    "HostRouter",
    "LocalHost",
    "RemoteHost",
    "RemoteTransport",
    "RuntimeHost",
]
