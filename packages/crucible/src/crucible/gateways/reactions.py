"""A reaction as a command: what the two chat adapters share when an emoji on a
message is the way a person asks an agent for something.

A command in this engine is not a path of its own — a slash command, a message
shortcut and now a reaction all become one synthetic message through
``InteractionDispatcher.invoke_command``, in the conversation of the message
they point at, and the agent's own instructions say what the word means. The
adapters differ only in how the platform tells them about the reaction and how
they find the message under it; this module holds the rest: the normalized
reaction, the emoji name as the map spells it, the text the command arrives as,
and the one rule about who may trigger one.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from crucible.ports.chat.directory import AgentDirectory

DEFAULT_QUOTE_LIMIT = 1500

# Slack appends a skin tone to an emoji's name ("+1::skin-tone-2"); the map is
# written without one, and a thumbs-up is a thumbs-up whatever its colour.
_SKIN_TONE = "::skin-tone-"


@dataclass(frozen=True)
class ReactionCommands:
    """What an agent's gateway is handed to treat reactions as commands: the
    emoji -> command word map, and how much of the reacted message is quoted
    under the word. An empty map means no reaction is a command."""

    commands: Mapping[str, str] = field(default_factory=dict)
    quote_limit: int = DEFAULT_QUOTE_LIMIT

    def __bool__(self) -> bool:
        return bool(self.commands)


@dataclass(frozen=True)
class Reaction:
    """One emoji a person put on a message: which emoji (as the map spells
    it), who, and the message — by the id the platform addresses it with (a
    Slack ``ts``, a Mattermost post id)."""

    emoji: str
    user_id: str
    channel_id: str
    message_id: str


def emoji_name(raw: str) -> str:
    """A platform's emoji name as the command map spells it: lower-case, no
    colons, no skin tone."""
    name = raw.strip().strip(":").lower()
    tone = name.find(_SKIN_TONE)
    return name[:tone] if tone >= 0 else name


def command_text(command: str, quoted: str, limit: int) -> str:
    """What the agent reads: the command word, then the message the reaction
    was put on, quoted — a reaction inside a thread points at one message,
    while the conversation the turn runs in is the whole thread. The quote is
    cut at ``limit`` characters: the message is in the thread the agent sees
    anyway, and the quote only says which one."""
    text = f"/{command}"
    quoted = quoted.strip()
    if not quoted:
        return text
    if len(quoted) > limit:
        quoted = quoted[: max(limit - 1, 0)] + "…"
    lines = "\n".join(f"> {line}" for line in quoted.splitlines())
    return f"{text}\n{lines}"


def reacted_by_an_agent(
    user_id: str, own_user_id: str, directory: AgentDirectory | None
) -> bool:
    """Whether the reaction is one of ours: the agent's own (it marks a message
    it is working on with one), or another agent's in the same deployment.
    Neither is a person asking for something."""
    if user_id == own_user_id:
        return True
    return directory is not None and user_id in directory.agent_user_ids()
