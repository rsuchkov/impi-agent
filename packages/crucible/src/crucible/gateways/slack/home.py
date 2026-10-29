"""The application's half of a Slack app's Home and agent panel.

Slack sends ``app_home_opened`` when a person opens the app's Home or its agent
panel and ``app_context_changed`` as what they look at changes; what to show
there is the application's — the engine has no Home of its own. The gateway
takes both events, so an app that shows nothing gets no "unhandled request" in
its log, and hands them to a ``SlackHome`` when the application gives one.
"""

from typing import Protocol


class SlackHome(Protocol):
    async def opened(self, user_id: str, tab: str) -> list[dict] | None:
        """The Home ``tab`` (``home`` or ``messages``) was opened by ``user_id``:
        the blocks to publish as the Home view, or None to publish nothing."""
        ...

    async def context_changed(self, user_id: str, context: dict) -> None:
        """What the person is looking at in Slack changed — the event's
        ``app_context`` as Slack sent it."""
        ...
