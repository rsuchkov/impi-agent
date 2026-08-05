"""Engine screens: a command the engine answers itself, and clicks that redraw
the same message instead of starting a turn."""

from pathlib import Path

from crucible.interactions import InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.screens import (
    ScreenRegistry,
    ScreenState,
    View,
    screen_action,
    state_from_context,
)
from crucible.skills import SkillLibrary
from crucible.store.sessions import SqliteSessionStore
from impi.skill_screen import PAGE_SIZE, SkillScreen
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of
from tests.test_skills import AGENT_YAML, _write_skill


def _text(view) -> str:
    """Everything a view renders, for asserting on content."""
    return "\n".join(c.text for c in view.cards)


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


class CountingScreen:
    command = "demo"

    def __init__(self) -> None:
        self.renders: list[ScreenState] = []

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        self.renders.append(state)
        page = int(state.data.get("page") or 0)
        return View.of(
            f"page {page}",
            (screen_action(state.with_data(page=page + 1), id="next", label="Next"),),
        )


def _dispatcher(store, chat, spy, screens):
    return InteractionDispatcher(
        store, presence_of(chat, sink=spy), PendingUiRequests(), store,
        screens=screens, callback_url="http://x/interact",
    )


# --- the mechanism ---------------------------------------------------------------


def test_state_round_trips_through_an_action() -> None:
    state = ScreenState(screen="demo", agent="assistant", data={"page": 3})
    action = screen_action(state, id="next", label="Next")

    decoded = state_from_context(action.context)

    assert decoded == state
    assert state_from_context({"token": "t"}) is None  # a plain widget isn't a screen


async def test_a_command_with_a_screen_never_reaches_an_agent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat, spy = FakeChat(), SinkSpy()
    screens = ScreenRegistry()
    screens.register(CountingScreen())
    dispatcher = _dispatcher(store, chat, spy, screens)
    try:
        opened = await dispatcher.open_screen(
            "assistant", "/demo", channel_id="ch1", conversation_id="ch1",
            kind="channel", user_id="u1",
        )

        assert opened is True
        assert spy.submitted == []  # no turn: the engine answered
        ref, cards, _url = chat.posted_cards[0]
        assert cards[0].text == "page 0" and ref.channel_id == "ch1"
        assert cards[0].actions[0].context["screen"] == "demo"
    finally:
        await store.close()


async def test_an_unknown_command_is_left_to_the_agent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    dispatcher = _dispatcher(store, FakeChat(), SinkSpy(), ScreenRegistry())
    try:
        assert await dispatcher.open_screen(
            "assistant", "/summarize", channel_id="ch1", conversation_id="ch1",
            kind="channel", user_id="u1",
        ) is False
    finally:
        await store.close()


async def test_a_click_redraws_the_same_message(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat, spy = FakeChat(), SinkSpy()
    screen = CountingScreen()
    screens = ScreenRegistry()
    screens.register(screen)
    dispatcher = _dispatcher(store, chat, spy, screens)
    try:
        await dispatcher.open_screen(
            "assistant", "demo", channel_id="ch1", conversation_id="ch1",
            kind="channel", user_id="u1",
        )
        state = chat.posted_cards[0][1][0].actions[0].context["state"]

        assert await dispatcher.redraw_screen(state, "", post_id="p1", user_id="u1") is True

        assert spy.submitted == []  # still no turn
        assert len(chat.posted_cards) == 1  # and no second message
        post_id, cards = chat.updated[0]
        assert (post_id, cards[0].text) == ("p1", "page 1")  # the state it carried
        assert cards[0].actions[0].context["screen"] == "demo"
    finally:
        await store.close()


async def test_a_click_with_unreadable_state_is_ignored(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    screens = ScreenRegistry()
    screens.register(CountingScreen())
    dispatcher = _dispatcher(store, chat, SinkSpy(), screens)
    try:
        assert await dispatcher.redraw_screen("not json", "", post_id="p1", user_id="u") is False
        assert chat.updated == []
    finally:
        await store.close()


# --- the /skills screen -----------------------------------------------------------


def _library_with(tmp_path: Path, count: int) -> SkillLibrary:
    library = SkillLibrary(tmp_path / "library")
    for i in range(count):
        _write_skill(
            library.root / f"skill-{i:02d}",
            body=f"---\nname: skill-{i:02d}\ndescription: number {i}\n---\nbody\n",
            script=False,
        )
    return library


def _agents_dir(tmp_path: Path, *names: str) -> Path:
    for name in names:
        manifest = tmp_path / "agents" / name / "agent.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(AGENT_YAML.replace("greek-teacher", name), encoding="utf-8")
    return tmp_path


async def test_skills_screen_pages_through_the_library(tmp_path: Path) -> None:
    screen = SkillScreen(_library_with(tmp_path, PAGE_SIZE + 3), _agents_dir(tmp_path, "tutor"))
    state = ScreenState(screen="skills", agent="assistant")

    first = await screen.render(state, user_id="u1")
    assert "page **1** of **2**" in _text(first)
    assert len(first.cards) == PAGE_SIZE + 1  # a header card plus one per skill
    assert [a.id for a in first.cards[0].actions] == ["next"]  # no "previous" on page 1

    second = await screen.render(state.with_data(page=0, value="next"), user_id="u1")
    assert "page **2** of **2**" in _text(second)
    assert len(second.cards) == 4  # header + the three left on the last page
    assert [a.id for a in second.cards[0].actions] == ["prev"]  # and none after the end


async def test_skills_screen_opens_a_skill_and_assigns_it(tmp_path: Path) -> None:
    reloads: list[int] = []
    agents = _agents_dir(tmp_path, "tutor", "helper")
    screen = SkillScreen(
        _library_with(tmp_path, 2), agents, reload=lambda: reloads.append(1)
    )
    state = ScreenState(screen="skills", agent="assistant")

    detail = await screen.render(state.with_data(value="open:skill-00"), user_id="u1")
    assert "skill-00" in _text(detail) and "_nobody_" in _text(detail)
    assign = next(a for a in detail.cards[0].actions if a.id == "assign")
    # The human reads the agent name; the value carries the routing.
    assert {(c.label, c.value) for c in assign.options} == {
        ("tutor", "assign:skill-00:tutor"), ("helper", "assign:skill-00:helper"),
    }

    after = await screen.render(
        state.with_data(value="assign:skill-00:tutor"), user_id="u1"
    )

    assert "gave **skill-00** to **tutor**" in _text(after)
    assert "registry:skill-00" in (agents / "agents" / "tutor" / "agent.yaml").read_text()
    assert reloads == [1]  # the agent is told to re-read its profile
    # And the same skill can be taken back.
    back = await screen.render(
        state.with_data(value="unassign:skill-00:tutor"), user_id="u1"
    )
    assert "removed **skill-00** from **tutor**" in _text(back)


async def test_skills_screen_warns_when_the_agent_lacks_the_tools(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    _write_skill(library.root / "needs-bash")  # its front matter requires read+bash
    agents = _agents_dir(tmp_path, "tutor")
    manifest = agents / "agents" / "tutor" / "agent.yaml"
    manifest.write_text(manifest.read_text().replace("    - bash\n", ""), encoding="utf-8")
    screen = SkillScreen(library, agents)

    view = await screen.render(
        ScreenState(screen="skills", agent="a").with_data(value="assign:needs-bash:tutor"),
        user_id="u1",
    )

    assert "doesn't allow the tools it needs (bash)" in _text(view)


async def test_skills_screen_says_so_when_the_library_is_empty(tmp_path: Path) -> None:
    screen = SkillScreen(SkillLibrary(tmp_path / "library"), _agents_dir(tmp_path, "tutor"))
    view = await screen.render(ScreenState(screen="skills", agent="a"), user_id="u1")
    assert "library is empty" in _text(view) and view.cards[0].actions == ()


async def test_the_screen_is_reachable_by_its_command_name() -> None:
    registry = ScreenRegistry()
    registry.register(SkillScreen(SkillLibrary("/nowhere"), "/nowhere"))
    assert registry.handles("/skills") and registry.handles("skills")
    assert not registry.handles("summarize")


async def test_the_trigger_word_is_configurable() -> None:
    # A workspace may already use /skills for something else.
    registry = ScreenRegistry()
    registry.register(
        SkillScreen(SkillLibrary("/nowhere"), "/nowhere", command="/Agent-Skills")
    )
    assert registry.handles("/agent-skills")  # normalized: no slash, lower-cased
    assert not registry.handles("skills")  # and the default no longer binds


async def test_a_long_description_is_trimmed_in_the_list_but_not_in_the_detail(tmp_path: Path) -> None:
    # Real-world skills (Anthropic's, say) describe themselves in a paragraph,
    # written for the model that routes to them — a list of those is a wall.
    library = SkillLibrary(tmp_path / "library")
    long_text = "Use this skill whenever " + "the user wants something very specific " * 8
    _write_skill(
        library.root / "docx",
        body=f"---\nname: docx\ndescription: {long_text}\n---\nbody\n",
        script=False,
    )
    screen = SkillScreen(library, _agents_dir(tmp_path, "tutor"))
    state = ScreenState(screen="skills", agent="a")

    listed = _text(await screen.render(state, user_id="u1"))
    detail = _text(await screen.render(state.with_data(value="open:docx"), user_id="u1"))

    assert "…" in listed and len(listed) < len(long_text)
    assert long_text.strip() in " ".join(detail.split())  # nothing hidden here


async def test_a_skill_carries_its_own_controls(tmp_path: Path) -> None:
    # Both controls live on the skill's card: on a card of their own they read
    # as belonging to nothing.
    screen = SkillScreen(_library_with(tmp_path, 1), _agents_dir(tmp_path, "tutor"))

    view = await screen.render(ScreenState(screen="skills", agent="a"), user_id="u1")

    card = view.cards[1]
    assert "skill-00" in card.text
    assert [a.id for a in card.actions] == ["open", "give"]
    assert card.actions[0].label == "Details"
