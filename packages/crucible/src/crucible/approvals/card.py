"""Rendering a card a human is about to make a security decision from.

The layout — a title, labelled one-line fields, at most one code block — is the
engine's; every value in it is made inert on the way in by ``crucible.containment``,
where the hardening and its rationale live.
"""

from collections.abc import Sequence

from crucible.containment import code_block, code_span


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
