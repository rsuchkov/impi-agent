"""Rendering a card a human is about to make a security decision from.

The layout — a title, labelled one-line fields, at most one code block — is the
engine's; every value in it is made inert on the way in by ``crucible.containment``,
where the hardening and its rationale live.
"""

from collections.abc import Sequence

from crucible.containment import code_block, code_span, one_line
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
        lines.append(f"**{block_label}:**" if block_label else "")
        lines.append(code_block(block))
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


def card_markdown(card: ApprovalCard) -> str:
    """An ``ApprovalCard`` as Markdown — what a chat platform shows, and the
    text form any channel may fall back to. The agent's name and the engine's
    headline are structure; every field value and the block are content."""
    headline = f"{card.icon} **{card.agent}** {one_line(card.headline)}"
    fields = [
        (one_line(row.label), f"{row.before} → {row.value}" if row.before else row.value)
        for row in card_rows(card)
    ]
    text = render_card(headline, fields, block_label=card.block_label, block=card.block)
    if card.expires_at:
        text += f"\n\n_Answer by {one_line(card.expires_at)}_"
    return text
