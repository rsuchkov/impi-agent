"""Cards a conversation holds on a gateway outside any turn of that gateway's.

A confirmation raised by a turn running elsewhere is forwarded to every
gateway the conversation is live in; a gateway with no turn of its own there
holds it until the verdict comes, and a client asks the conversation what it
is waiting on. The port is what the gateway writes and reads; the store
implements it, so every replica of the gateway shows the same cards.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HeldCard:
    """One such card. ``conversation`` is the gateway's own key; ``post_id`` is
    the gateway's, minted unique across every gateway (a random id, never a
    platform's post id), so a post id alone names one card; the card and its
    controls travel as JSON (``ports.chat.interactions.approval_card_to_json``
    / ``actions_to_json``), the way a form does."""

    gateway: str
    conversation: str
    post_id: str
    agent: str
    card: str
    actions: str
    held_at: str


class HeldCards(Protocol):
    async def hold_card(self, record: HeldCard) -> None:
        """Keep the card; the same post again replaces it (rewritten, still asking)."""
        ...

    async def held_cards(self, gateway: str, conversation: str) -> list[HeldCard]:
        """The conversation's cards on that gateway, oldest first."""
        ...

    async def held_card(self, post_id: str) -> HeldCard | None: ...

    async def release_card(self, post_id: str) -> None:
        """The question is over: the verdict came, or it was withdrawn."""
        ...
