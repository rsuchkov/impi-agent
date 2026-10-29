"""The application's half of a Slack app's Home and agent panel.

Slack sends ``app_home_opened`` when a person opens the app's Home or its agent
panel and ``app_context_changed`` as what they look at changes; what to show
there is the application's — the engine has no Home of its own. The gateway
takes both events, so an app that shows nothing gets no "unhandled request" in
its log, and hands them to a ``SlackHome`` when the application gives one.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

# How the hook publishes a Home view: ``views.publish`` for the person who
# opened it, the view as Slack takes it (``type: home``, blocks,
# ``private_metadata`` …). The hook decides what to publish, when, and what to
# publish instead when Slack refuses a view (``invalid_blocks``).
PublishHome = Callable[[dict], Awaitable[None]]


class SlackHome(Protocol):
    async def opened(self, user_id: str, tab: str, publish: PublishHome) -> None:
        """The Home ``tab`` (``home`` or ``messages``) was opened by ``user_id``:
        publish a view with ``publish``, or nothing."""
        ...

    async def context_changed(self, user_id: str, context: dict) -> None:
        """What the person is looking at in Slack changed — the event's
        ``app_context`` as Slack sent it."""
        ...
