"""Screens: commands the ENGINE answers, and clicks that redraw them.

Most commands belong to an agent — they become a turn. A few belong to the
engine itself (browsing the skill library, listing agents): the answer is a fact,
not a judgement, so running a model to produce it would be slower, costlier and
less reliable. Those are **screens**.

A screen is a pure function of its state: given a ``ScreenState`` it returns a
``View`` (text + actions). A click carries the next state back, the engine calls
the screen again and **rewrites the same message** — so paging through a list
never starts a turn and never posts a new message. This is the interaction kind
that sits beside the two existing ones (a blocking mid-turn request, and a
fire-and-forget widget whose click feeds the agent).
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import Action, Card, Choice, ConversationRef

# Marks a click as belonging to a screen, and names which one. The value rides in
# the action context (Mattermost echoes it; Slack packs it into the button value),
# so no store row is needed to route a redraw.
SCREEN_KEY = "screen"
STATE_KEY = "state"


@dataclass(frozen=True)
class ScreenState:
    """Where the viewer is. ``screen`` is which screen; ``data`` is its own bookkeeping
    (a page number, the selected item) — small, opaque and round-tripped through
    the platform, so a screen keeps no server-side session. ``agent`` is whose
    chat client posted the message, which is the client that must rewrite it."""

    screen: str
    agent: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def with_data(self, **updates: Any) -> "ScreenState":
        return ScreenState(screen=self.screen, agent=self.agent, data={**self.data, **updates})

    def encode(self) -> str:
        return json.dumps(
            {"n": self.screen, "a": self.agent, "d": self.data}, separators=(",", ":")
        )

    @staticmethod
    def decode(raw: str) -> "ScreenState | None":
        try:
            payload = json.loads(raw)
            return ScreenState(
                screen=str(payload["n"]),
                agent=str(payload.get("a") or ""),
                data=dict(payload.get("d") or {}),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class View:
    """What a screen renders: a message built from cards, each with its own text
    and its own controls (a row per item, its buttons beside it)."""

    cards: tuple[Card, ...] = ()

    @staticmethod
    def of(text: str, actions: tuple[Action, ...] = (), *, accent: str = "") -> "View":
        """The single-card case — a block of text with controls under it."""
        return View(cards=(Card(text=text, actions=actions, accent=accent),))


class Screen(Protocol):
    """One engine-answered command. ``command`` is the trigger word it binds to:
    a screen with ``command = "skills"`` answers ``/skills`` and nothing else, so
    the platform's slash command must use that exact word. ``render`` is called
    for the first view and for every click, with the state the click carried."""

    command: str

    async def render(self, state: ScreenState, *, user_id: str) -> View: ...


ScreenRenderer = Callable[[ScreenState, str], Awaitable[View]]


class ScreenRegistry:
    """The screens this engine answers, keyed by trigger word: a command reaches
    a screen when its word matches (``/skills`` and ``skills`` are the same key).

    An application registers what it wants to expose; anything not registered
    stays an ordinary agent command, so adding a screen never changes how the
    existing ones behave.
    """

    def __init__(self) -> None:
        self._screens: dict[str, Screen] = {}

    def register(self, screen: Screen) -> None:
        """Bind a screen to its trigger word — after this, that command never
        reaches an agent."""
        self._screens[screen.command] = screen

    def get(self, name: str) -> Screen | None:
        return self._screens.get(name.lstrip("/").strip().lower())

    def handles(self, name: str) -> bool:
        return self.get(name) is not None

    def names(self) -> tuple[str, ...]:
        """The words this engine answers itself — what a caller may ask for, and
        what to name back when it asks for something else."""
        return tuple(sorted(self._screens))

    def __bool__(self) -> bool:
        return bool(self._screens)


def screen_action(
    state: ScreenState, *, id: str, label: str, value: str = "", style: str = "",
    kind: str = "button", options: tuple[Choice, ...] = (),
) -> Action:
    """A control that redraws its screen with ``state`` instead of running a turn."""
    return Action(
        id=id,
        label=label,
        value=value,
        style=style,
        kind=kind,
        options=options,
        context={SCREEN_KEY: state.screen, STATE_KEY: state.encode()},
    )


def state_from_context(context: dict[str, Any]) -> ScreenState | None:
    """The state a click carried, or None when the click isn't a screen's."""
    if not context.get(SCREEN_KEY):
        return None
    return ScreenState.decode(str(context.get(STATE_KEY) or ""))


async def post_first_view(
    screen: Screen,
    poster: ChatClient,
    ref: ConversationRef,
    *,
    agent: str,
    user_id: str,
    callback_url: str,
) -> None:
    """Render a screen's opening view and post it as ``agent``.

    Shared by the two ways a screen is opened — a slash command, and an agent
    reaching for it mid-turn — so both produce the same message, and every click
    on it afterwards takes the same engine-only path."""
    view = await screen.render(ScreenState(screen=screen.command, agent=agent), user_id=user_id)
    await poster.post_cards(ref, list(view.cards), callback_url=callback_url)
