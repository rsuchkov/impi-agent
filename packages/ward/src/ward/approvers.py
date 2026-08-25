"""Who this deployment trusts, as platform user ids.

One list, two uses, and they are the same trust: the people who may answer a
request for a credential are the people who may administer the broker from
chat. Keeping them separate would mean a deployment where somebody can approve
handing out `prod-db-password` but not open the store that holds it, which is a
distinction without a difference.

The configured entries may be usernames or ids. A username the directory
recognizes becomes its id; anything else is taken to be an id already, so a
platform with no username lookup still works. Resolved once and cached — the
list is small and stable, and a directory lookup in the path of every request
for a credential is a directory outage in the path of every request.
"""

import logging

from crucible.ports.chat.admin import ChatAdmin

logger = logging.getLogger(__name__)


class Approvers:
    """The configured list, resolved lazily against the chat directory."""

    def __init__(self, configured: str, admin: ChatAdmin | None) -> None:
        self._configured = configured
        self._admin = admin
        self._resolved: frozenset[str] | None = None

    async def ids(self) -> frozenset[str]:
        if self._resolved is not None:
            return self._resolved
        entries = [e.strip().lstrip("@") for e in self._configured.split(",") if e.strip()]
        found: set[str] = set()
        for entry in entries:
            known = await self._admin.resolve_username(entry) if self._admin else None
            found.add(known or entry)
        self._resolved = frozenset(found)
        if not self._resolved:
            logger.warning(
                "no approvers configured — every request that needs a human will be "
                "refused, and the chat operator surface answers nobody"
            )
        return self._resolved

    async def allows(self, user_id: str) -> bool:
        """Whether this platform user may answer and administer.

        An empty list allows nobody, deliberately: a deployment that forgot to
        name anyone gets a broker that refuses, not one that trusts the room.
        """
        return bool(user_id) and user_id in await self.ids()
