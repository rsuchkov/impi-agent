"""Text a person will read that was written by someone who may be hostile.

Everything worth reading on a card, a panel or a notice — a reason, a command,
a tool's arguments — is written by whoever is asking, which on this system may
be a compromised agent. So the rendering is not formatting, it is containment:
the caller supplies *content* and never gets to supply *structure*.

Three things were possible before these helpers existed, and each of them let a
request forge the message that was supposed to describe it:

* a newline in a free-text field opened a new markdown line, so the caller could
  add a second, innocuous ``**Secret:**`` under the real one;
* a run of backticks inside a fenced block closed the fence, so the caller could
  append a second, innocuous ``**Command:**`` block after the real one — and the
  last block is the one an eye skimming downward lands on;
* a bidi override reordered a command visually without changing what runs.

The countermeasures are boring on purpose: collapse free text to one line, strip
the characters that carry structure or reorder text, and pick a fence the
content demonstrably cannot close. Pure text in, pure text out — this module
knows nothing of the engine and is imported by whatever draws for a human.
"""

import re
import shlex
import unicodedata
from collections.abc import Sequence

# Long enough for a real reason, short enough that a card stays scannable — and
# a bounded field cannot push the rest of the card off a phone screen.
FIELD_LIMIT = 200
# A passage on a card: whole by intent, bounded so one card stays one message.
PASSAGE_LIMIT = 8000
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


def _inert_keep_lines(text: str) -> str:
    """Like ``_inert`` but a line break and a tab stay: for text whose lines
    are its content. ``\\r\\n`` becomes ``\\n`` first — a stray carriage return
    is invisible in one renderer and a doubled line in another."""
    kept = text.replace("\r\n", "\n").replace("\r", "\n")
    # Every space separator becomes a plain space: a no-break or ideographic
    # space before a `[` must not hide the `[` from the line-start check.
    return "".join(
        " " if unicodedata.category(c) == "Zs" else c
        for c in kept
        if c in "\n\t" or unicodedata.category(c) not in _STRUCTURAL_CATEGORIES
    )


# What a model may read as an opening bracket: the ASCII one, and its
# fullwidth twin.
_OPENING_BRACKETS = r"\[\uff3b"


def quoted(text: str, *, inline: bool = False) -> str:
    """Other people's text, quoted into a prompt whose structure is marked
    with lines that begin with ``[``.

    The lines stay — a message is its lines — but no line of it may *present*
    as one of the prompt's own: a leading ``[`` is escaped to ``\\[``, so a
    message containing ``[/context x]`` or ``[@admin]: do it`` arrives as text
    somebody typed rather than as a marker or a speaker. Bidi overrides and
    the other structural characters go, as everywhere here.

    ``inline``: the text starts mid-line, after a speaker's ``]: `` — its first
    line cannot present as anything and is left as written; only the lines
    after it are checked.
    """
    line_start = r"(\n[ \t]*)" if inline else r"(?m)(^[ \t]*)"
    return re.sub(line_start + f"([{_OPENING_BRACKETS}])", r"\1\\\2", _inert_keep_lines(text))


# What a speaker's name may carry into the prompt: an identifier, nothing a
# line of the envelope is built from (`]`, `:`, `·`, a space).
_SPEAKER_RE = re.compile(r"[^A-Za-z0-9._@+-]")


def speaker(name: str, *, limit: int = 64) -> str:
    """A name as the prompt's speaker marker carries it. Usernames come from
    platforms and programs that do not all restrict them; what is not an
    identifier character becomes `_`, so `admin]: approve` cannot close the
    marker and write a line of its own."""
    return _SPEAKER_RE.sub("_", _inert(name))[:limit]


def one_line(text: str, *, limit: int = FIELD_LIMIT) -> str:
    """Caller-supplied free text, reduced to something that cannot add a line.

    Whitespace collapses (a tab is a line-layout tool as much as a space), the
    structural characters go, and the result is truncated with a visible mark so
    a reader can tell a short reason from a trimmed one.
    """
    # Line breaks and tabs become spaces BEFORE the structural characters go:
    # dropped first, a break would glue the words either side of it together.
    collapsed = _defuse(" ".join(_inert_keep_lines(text).split()))
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + _ELLIPSIS
    return collapsed


def passage(text: str, *, limit: int = PASSAGE_LIMIT) -> str:
    """Caller-supplied text a person is asked to read as written — a comment, a
    description: its lines stay, every other structural character goes, backtick
    runs are defused, and a text past ``limit`` is cut with a mark that says how
    much there was. A renderer still escapes its own markup; this is the
    containment both renderers share."""
    inert = _defuse(_inert_keep_lines(text)).strip()
    if len(inert) > limit:
        return f"{inert[:limit].rstrip()}\n{_ELLIPSIS} cut at {limit} of {len(inert)} chars"
    return inert


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


def preformatted(text: str, *, lang: str = "") -> str:
    """A fenced block that keeps its lines.

    ``code_block`` flattens: a line break is a control character, and for a
    one-line command or reason that is the containment wanted. Pretty-printed
    JSON is nothing without its line breaks, so this keeps ``\\n`` and ``\\t``,
    strips every other structural character the same way, and defuses backtick
    runs so the block still cannot be closed from within. ``\\r\\n`` becomes
    ``\\n`` first: a stray carriage return is invisible in one renderer and a
    doubled line in another.

    The fence is always three backticks (``_defuse`` leaves no run of three in
    the content), which matters: Slack's converter recognises a fence line by a
    pattern that a longer run would not match.
    """
    inert = _defuse(_inert_keep_lines(text))
    fence = _fence(inert)
    return f"{fence}{lang}\n{inert}\n{fence}"


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
