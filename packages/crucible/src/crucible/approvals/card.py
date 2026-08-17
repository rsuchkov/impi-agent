"""Rendering a card a human is about to make a security decision from.

Everything worth reading on such a card — the reason, the command — is written
by whoever is asking, which on this system may be a compromised agent. So the
rendering is not formatting, it is containment: the caller supplies *content*
and never gets to supply *structure*.

Three things were possible before this module existed, and each of them let a
request forge the card that was supposed to describe it:

* a newline in a free-text field opened a new markdown line, so the caller could
  add a second, innocuous ``**Secret:**`` under the real one;
* a run of backticks inside a fenced block closed the fence, so the caller could
  append a second, innocuous ``**Command:**`` block after the real one — and the
  last block is the one an eye skimming downward lands on;
* a bidi override reordered a command visually without changing what runs.

The countermeasures below are boring on purpose: collapse free text to one line,
strip the characters that carry structure or reorder text, and pick a fence the
content demonstrably cannot close.
"""

import re
import shlex
import unicodedata
from collections.abc import Sequence

# Long enough for a real reason, short enough that a card stays scannable — and
# a bounded field cannot push the rest of the card off a phone screen.
FIELD_LIMIT = 200
COMMAND_LIMIT = 600
_ELLIPSIS = "…"

# Cc: control characters, newlines among them — the ones that would open a line
# the caller did not pay for. Cf: format characters, which include the bidi
# overrides that can make `rm -rf /` render as something reassuring. Zl/Zp: the
# Unicode line and paragraph separators, newlines by another name.
_STRUCTURAL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _inert(text: str) -> str:
    """Drop every character that carries structure rather than meaning."""
    return "".join(c for c in text if unicodedata.category(c) not in _STRUCTURAL_CATEGORIES)


def one_line(text: str, *, limit: int = FIELD_LIMIT) -> str:
    """Caller-supplied free text, reduced to something that cannot add a line.

    Whitespace collapses (a tab is a line-layout tool as much as a space), the
    structural characters go, and the result is truncated with a visible mark so
    a reader can tell a short reason from a trimmed one.
    """
    collapsed = _defuse(" ".join(_inert(text).split()))
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + _ELLIPSIS
    return collapsed


def _defuse(text: str) -> str:
    """Break every run of three or more backticks with spaces.

    Belt to the fence's braces. Computing a longer fence is the correct answer
    for CommonMark — and it is what Mattermost renders — but it leans on the
    rule that a fence only closes at the start of a line, and Slack's mrkdwn is
    laxer about that. Leaving no closable run in the content at all makes the
    block safe on a renderer we do not control and cannot test from here.

    Visible on purpose: the backticks are still there to read, just spaced.
    """
    return re.sub(r"`{3,}", lambda run: " ".join(run.group()), text)


def _fence(content: str, *, minimum: int = 3) -> str:
    """A backtick run longer than any inside ``content``.

    The rule CommonMark gives us: a fenced block ends at a line of at least as
    many backticks as opened it, so opening with one more than the longest run
    present makes the block unclosable from within.
    """
    longest = 0
    run = 0
    for char in content:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return "`" * max(minimum, longest + 1)


def code_span(text: str) -> str:
    """Inline code that the text cannot break out of.

    Padded with spaces when the content starts or ends with a backtick, which is
    the other way a span ends early.
    """
    inert = one_line(text)
    if not inert:
        return ""
    fence = _fence(inert, minimum=1)
    pad = " " if inert.startswith("`") or inert.endswith("`") else ""
    return f"{fence}{pad}{inert}{pad}{fence}"


def code_block(text: str) -> str:
    """A fenced block that the text cannot close early."""
    inert = _defuse(_inert(text))
    fence = _fence(inert)
    return f"{fence}\n{inert}\n{fence}"


def command_line(argv: Sequence[str], *, limit: int = COMMAND_LIMIT) -> str:
    """An argv as a shell would need it written.

    Quoting per element is not cosmetic: without it ``sh -c 'curl … -d $TOKEN'``
    and ``sh -c curl … -d $TOKEN`` look the same on a card, and only one of them
    is what will run. It also makes an argument containing a space, a quote or a
    newline visible as one argument instead of several.
    """
    rendered = " ".join(shlex.quote(_inert(part)) for part in argv)
    if len(rendered) > limit:
        rendered = rendered[: limit - 1].rstrip() + _ELLIPSIS
    return rendered


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
