"""Rendering a card a human is about to make a security decision from.

The layout — a title, labelled one-line fields, at most one code block — is the
engine's; every value in it is made inert on the way in by ``crucible.containment``,
where the hardening and its rationale live.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from crucible.containment import code_span, one_line, passage, preformatted
from crucible.ports.chat.types import ApprovalCard, PreviewRow

# How a card's ``title`` and ``tool`` read when a channel draws them as rows:
# the one-line account of the call first, the tool's id last. A program gets
# them as fields of their own and never sees these words.
ACTION_LABEL = "Action"
TOOL_LABEL = "Tool"


def render_card(
    title: str, fields: Sequence[tuple[str, str]], *, block_label: str = "", block: str = ""
) -> str:
    """A card: a title, labelled one-line fields, and at most one code block.

    The labels are the caller's *structure* and come from the engine; the values
    are the caller's *content* and are made inert on the way in. A field with no
    value is dropped rather than shown empty.
    """
    lines = [title, ""]
    lines += [f"**{label}:** {code_span(value)}" for label, value in fields if value]
    if block:
        lines.append(f"**{one_line(block_label)}:**" if block_label else "")
        lines.append(preformatted(block))
    return "\n".join(lines)


def card_rows(card: ApprovalCard) -> tuple[PreviewRow, ...]:
    """The rows a chat draws: the title as ``Action`` ahead of the fields, and
    the tool's id as ``Tool`` after them — the latter only where the headline
    does not already say it (a tool named by its display name), since the
    operator and the audit speak in ids and a person should not read the same
    word twice."""
    rows: list[PreviewRow] = []
    if card.title:
        rows.append(PreviewRow(ACTION_LABEL, card.title))
    rows.extend(card.fields)
    if card.tool and card.tool not in card.headline:
        rows.append(PreviewRow(TOOL_LABEL, card.tool))
    return tuple(rows)


# The most a verbatim block gets on a card: whole by intent (K19), bounded so the
# card stays one message on the platform with the smaller cap.
BLOCK_LIMIT = 15000


@dataclass(frozen=True)
class CardLayout:
    """How a chat draws an ``ApprovalCard``, decided once for every platform.
    With a ``title`` from the preview it is the heading and the engine's
    headline shows only once the question is over, as the verdict; without one
    the heading is the agent and the engine's headline, as it always was. The
    agent's name signs the card small at the bottom when the heading does not
    carry it; the tool's id signs it always. Every value here is the caller's
    content made inert — one line, or a passage with its lines — and a renderer
    adds its own markup escaping and nothing else."""

    icon: str
    agent: str
    title: str  # "" when the preview gave none
    headline: str  # the engine's words — the question, or the verdict once answered
    facts: tuple[tuple[str, str], ...]  # (label, one-line value; a change as before → value)
    passages: tuple[tuple[str, str], ...]  # (label, text with its lines)
    block_label: str
    block: str  # with its lines, defused; "" for none
    tool: str
    expires_at: str

    @property
    def verdict(self) -> str:
        """The engine's words to show under a title once the question is over."""
        return self.headline if self.title and self.closed else ""

    @property
    def signature(self) -> tuple[str, ...]:
        """What signs the card small at the bottom: the agent when the heading
        does not name it, and the tool's id."""
        parts = [self.agent] if self.title else []
        if self.tool:
            parts.append(self.tool)
        return tuple(parts)

    closed: bool = False


def card_layout(card: ApprovalCard) -> CardLayout:
    facts: list[tuple[str, str]] = []
    passages: list[tuple[str, str]] = []
    for row in card.fields:
        label = one_line(row.label)
        if row.passage:
            if row.before:
                passages.append((f"{label} (before)", passage(row.before)))
            if row.value:
                passages.append((label, passage(row.value)))
            continue
        if not (row.value or row.before):
            continue  # nothing to show; a change to nothing still is one
        value = f"{one_line(row.before)} → {one_line(row.value)}" if row.before else one_line(row.value)
        facts.append((label, value))
    return CardLayout(
        icon=card.icon,
        agent=one_line(card.agent),
        title=one_line(card.title),
        headline=one_line(card.headline),
        facts=tuple(facts),
        passages=tuple(passages),
        block_label=one_line(card.block_label),
        block=passage(card.block, limit=BLOCK_LIMIT) if card.block else "",
        tool=one_line(card.tool),
        expires_at=one_line(card.expires_at),
        closed=bool(card.verdict),
    )


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_\[\]#>!|~<])")
# A mention a chat platform would act on (`@channel`, `@here`, a person): the
# `@` is followed by a zero-width space, which reads the same and pings nobody.
_MENTION = re.compile(r"@(?=\w)")
_UNPINGED_AT = "@\u200b"


def plain_markdown(text: str) -> str:
    """Caller text as Markdown shows it literally: every character Markdown
    could read as structure — emphasis, a link, an image, a heading, a fence,
    raw HTML — is escaped with a backslash, and a mention is made inert. For a
    heading or a passage, which cannot be code spans and must still be the
    caller's words."""
    return _MENTION.sub(_UNPINGED_AT, _MARKDOWN_SPECIALS.sub(r"\\\1", text))


def _quote(text: str) -> str:
    """A passage as a Markdown quote: one ``> `` per line, every line shown
    literally (``plain_markdown``) and what could still read as a list at a
    line's start escaped too — so the quote stays a quote and carries no link,
    image or mention a person could be led by."""
    lines = []
    for line in text.split("\n"):
        line = plain_markdown(line)
        stripped = line.lstrip()
        if stripped[:1] in ("-", "+") or stripped[:2] == "1.":
            line = line[: len(line) - len(stripped)] + "\\" + stripped
        lines.append(f"> {line}")
    return "\n".join(lines)


def card_markdown(card: ApprovalCard) -> str:
    """An ``ApprovalCard`` as Markdown — what a chat platform shows, and the
    text form any channel may fall back to. Facts stay in code spans: Markdown
    would turn a value's ``[text](url)`` into a link on the card a person
    decides from; a title in the heading is escaped character by character
    instead; a passage is a quote, its lines kept and their starts escaped."""
    layout = card_layout(card)
    if layout.title:
        lines = [f"{layout.icon} **{plain_markdown(layout.title)}**"]
        if layout.verdict:
            lines.append(layout.verdict)
    else:
        lines = [f"{layout.icon} **{layout.agent}** {layout.headline}"]
    lines.append("")
    lines += [f"**{label}:** {code_span(value)}" for label, value in layout.facts]
    for label, text in layout.passages:
        lines += [f"**{label}:**", _quote(text)]
    if layout.block:
        if layout.block_label:
            lines.append(f"**{layout.block_label}:**")
        lines.append(preformatted(layout.block))
    signature = list(layout.signature)
    if layout.expires_at:
        signature.append(f"Answer by {layout.expires_at}")
    if signature:
        lines += ["", f"_{' · '.join(signature)}_"]
    return "\n".join(lines)
