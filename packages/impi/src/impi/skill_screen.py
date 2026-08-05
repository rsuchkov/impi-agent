"""The `/skills` screen: browse the library and give a skill to an agent.

An engine screen, not an agent turn — listing what is installed and editing a
profile are facts and edits, so a model in the loop would only add latency and a
chance to invent a skill that isn't there. Every click redraws this one message.

State: ``page`` (which slice of the library) and ``skill`` (the one being looked
at). ``value`` carries what the last control returned.
"""

import logging
from pathlib import Path

from crucible.interactions.screens import ScreenState, View, screen_action
from crucible.ports.chat.types import Card, Choice
from crucible.skills import (
    SkillError,
    SkillLibrary,
    assign_skill,
    assigned_skills,
    declared_tools,
    unassign_skill,
)

logger = logging.getLogger(__name__)

# Mattermost caps a dropdown at 20 options and Slack at 100; a page of 8 keeps
# the message readable on both.
PAGE_SIZE = 8

_PREV, _NEXT, _BACK = "prev", "next", "back"
_OPEN, _ASSIGN, _UNASSIGN = "open", "assign", "unassign"

# Mattermost paints the card's edge in these; Slack ignores them.
_ACCENT_HEADER = "#7a5299"
_ACCENT_ASSIGNED = "#3db887"
_ACCENT_IDLE = "#8e9297"
_ACCENT_DETAIL = "#5d89ea"


# A skill's description is written for the model that picks it, so it can run to
# a paragraph. The list shows a taste of it; the detail card shows all of it.
_SUMMARY_MAX = 160



def _short(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _SUMMARY_MAX:
        return text
    return text[:_SUMMARY_MAX].rsplit(" ", 1)[0] + "…"


# The trigger word the library browser binds to (SKILLS_COMMAND overrides it).
DEFAULT_COMMAND = "skills"


def _split_target(value: str) -> tuple[str, str]:
    """"assign:greek-drill:tutor" -> ("greek-drill", "tutor")."""
    parts = value.split(":", 2)
    return (parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")


class SkillScreen:
    """``/skills`` — the library, paged, with assignment per agent."""

    def __init__(
        self,
        library: SkillLibrary,
        agents_path: str | Path,
        *,
        command: str = DEFAULT_COMMAND,
        reload=None,
    ) -> None:
        # The trigger word this screen owns: register the platform's slash command
        # under it and it lands here instead of on an agent. Normalized, so
        # "/Skills" in a config file still binds.
        self.command = command.lstrip("/").strip().lower() or DEFAULT_COMMAND
        self._library = library
        self._agents_path = Path(agents_path)
        # Called after an assignment so the agent picks it up on its next turn.
        self._reload = reload

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        value = str(state.data.get("value") or "")
        page = int(state.data.get("page") or 0)
        selected = str(state.data.get("skill") or "")

        if value in (_PREV, _NEXT):
            page = max(0, page + (1 if value == _NEXT else -1))
            selected = ""
        elif value == _BACK:
            selected = ""
        elif value.startswith(f"{_OPEN}:"):
            selected = value.split(":", 1)[1]
        elif value.startswith(f"{_ASSIGN}:") or value.startswith(f"{_UNASSIGN}:"):
            # verb:skill:agent — the control carries its own subject, so a
            # control in the list acts without a selection step first.
            verb = value.split(":", 1)[0]
            skill_name, agent = _split_target(value)
            note = self._apply(skill_name, agent, assign=verb == _ASSIGN)
            return self._detail(state.with_data(skill=skill_name), skill_name, note=note)

        state = state.with_data(page=page, skill=selected)
        return self._detail(state, selected) if selected else self._index(state, page)

    # -- views ------------------------------------------------------------------

    def _index(self, state: ScreenState, page: int) -> View:
        skills = self._library.list()
        if not skills:
            return View.of(
                "### 📚 Skills\nThe library is empty.\n"
                "Install one with `impi skill install <source>`, or ask the support agent.",
                accent=_ACCENT_IDLE,
            )
        pages = max(1, -(-len(skills) // PAGE_SIZE))
        page = min(page, pages - 1)
        window = skills[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        users = self._users()
        base = state.with_data(page=page, skill="", value="")

        header_actions = []
        if page > 0:
            header_actions.append(screen_action(base, id="prev", label="◀ Previous", value=_PREV))
        if page + 1 < pages:
            header_actions.append(screen_action(base, id="next", label="Next ▶", value=_NEXT))
        cards = [
            Card(
                text=(
                    f"### 📚 Skills\n"
                    f"**{len(skills)}** installed"
                    + (f"  ·  page **{page + 1}** of **{pages}**" if pages > 1 else "")
                ),
                actions=tuple(header_actions),
                accent=_ACCENT_HEADER,
            )
        ]
        # One card per skill, its buttons beside it — no picking from a dropdown
        # to find out what something is.
        for skill in window:
            holders = users.get(skill.name, [])
            free = [a for a in self._agents() if a not in holders]
            controls = [
                screen_action(base, id="open", label="Details",
                              value=f"{_OPEN}:{skill.name}")
            ]
            if free:
                controls.append(screen_action(
                    base, id="give", label="Give it to…", kind="select",
                    options=tuple(
                        Choice(label=a, value=f"{_ASSIGN}:{skill.name}:{a}") for a in free
                    ),
                ))
            cards.append(
                Card(
                    text=(
                        f"**{skill.name}**"
                        + (f"  `{skill.version}`" if skill.version else "")
                        + f"\n{_short(skill.description)}\n"
                        + (f"👤 {', '.join(holders)}" if holders else "_not given to anyone yet_")
                    ),
                    # Both controls on the skill's own card: a control on a card of
                    # its own reads as belonging to nothing.
                    actions=tuple(controls),
                    accent=_ACCENT_ASSIGNED if holders else _ACCENT_IDLE,
                )
            )
        return View(cards=tuple(cards))

    def _detail(self, state: ScreenState, name: str, *, note: str = "") -> View:
        try:
            skill = self._library.get(name)
        except SkillError:
            return self._index(state.with_data(skill="", value=""), int(state.data.get("page") or 0))
        holders = self._users().get(skill.name, [])
        base = state.with_data(skill=skill.name, value="")

        lines = [
            f"### {skill.name}" + (f"  `{skill.version}`" if skill.version else ""),
            skill.description,
            "",
        ]
        if skill.requires_tools:
            lines.append(f"**Needs tools:** {', '.join(f'`{t}`' for t in skill.requires_tools)}")
        if skill.tags:
            lines.append(f"**Tags:** {', '.join(skill.tags)}")
        lines.append(f"**Source:** {skill.source.describe() if skill.source else '_local_'}")
        lines.append(
            f"**Given to:** {', '.join(f'**{h}**' for h in holders) if holders else '_nobody_'}"
        )
        if note:
            lines += ["", "---", note]

        free = [a for a in self._agents() if a not in holders]
        actions = []
        if free:
            actions.append(screen_action(
                base, id="assign", label="Give it to…", kind="select",
                options=tuple(
                    Choice(label=a, value=f"{_ASSIGN}:{skill.name}:{a}") for a in free
                ),
            ))
        if holders:
            actions.append(screen_action(
                base, id="unassign", label="Take it from…", kind="select",
                options=tuple(
                    Choice(label=h, value=f"{_UNASSIGN}:{skill.name}:{h}") for h in holders
                ),
            ))
        actions.append(screen_action(base, id="back", label="◀ All skills", value=_BACK))
        return View(
            cards=(Card(text="\n".join(lines), actions=tuple(actions), accent=_ACCENT_DETAIL),)
        )

    # -- the edit ---------------------------------------------------------------

    def _apply(self, skill_name: str, agent: str, *, assign: bool) -> str:
        """Write the profile, then reload. Returns the line to show the user."""
        manifest = self._agents_path / "agents" / agent / "agent.yaml"
        if not manifest.is_file():
            return f"⚠️ no agent `{agent}`"
        try:
            if assign:
                changed = assign_skill(manifest, skill_name)
            else:
                changed = unassign_skill(manifest, skill_name)
        except SkillError as exc:
            return f"⚠️ {exc}"
        if not changed:
            return f"_{agent} was already{'' if assign else ' not'} using {skill_name}._"
        if self._reload:
            self._reload()
        if not assign:
            return f"✅ removed **{skill_name}** from **{agent}**."
        missing = self._missing_tools(skill_name, manifest)
        if missing:
            # Assigned, but it cannot actually run — say so where the user is.
            return (
                f"✅ gave **{skill_name}** to **{agent}**.\n"
                f"⚠️ that agent doesn't allow the tools it needs ({', '.join(missing)}) — "
                f"add them to `runtime.tools`."
            )
        return f"✅ gave **{skill_name}** to **{agent}** — live on its next turn."

    def _missing_tools(self, skill_name: str, manifest: Path) -> list[str]:
        try:
            needs = self._library.get(skill_name).requires_tools
        except SkillError:
            return []
        allowed = declared_tools(manifest)
        return [t for t in needs if t not in allowed]

    # -- the agents directory ---------------------------------------------------

    def _manifests(self) -> list[Path]:
        return sorted(self._agents_path.glob("agents/*/agent.yaml"))

    def _agents(self) -> list[str]:
        return [m.parent.name for m in self._manifests()]

    def _users(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for manifest in self._manifests():
            try:
                for name in assigned_skills(manifest):
                    out.setdefault(name, []).append(manifest.parent.name)
            except SkillError as exc:  # a profile we can't parse must not blank the screen
                logger.warning("skills screen: %s", exc)
        return out
