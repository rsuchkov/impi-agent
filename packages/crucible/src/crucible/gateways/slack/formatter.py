"""Markdown -> Slack mrkdwn conversion for outgoing agent prose.

Slack renders its own "mrkdwn" dialect, not Markdown: bold is single-starred,
links are <url|text>, and there are no headings or tables. Agents are asked
(via a short prompt hint) to write plain Markdown; this module converts it at
the adapter boundary, so the result doesn't depend on the model following
formatting instructions.

Pure text -> text, no SDK (mirrors rendering.py). Fenced code blocks pass
through every stage untouched, and inline code spans are shielded from the
emphasis/link rules. Conversion is best-effort: on any internal error the
original text is sent as-is — delivering the reply beats formatting it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*$")
_HR_RE = re.compile(r"^(---|\*\*\*|___)\s*$")
# Existing mrkdwn tokens that must survive verbatim: <url|text>, <@U…>, <!…>.
_ANGLE_TOKEN_RE = re.compile(r"<[^>]*>")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Line-scoped Markdown -> mrkdwn rules, applied in order (outside code only).
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(\s*)- \[ \] (.+)$"), r"\1• ☐ \2"),  # task list, unchecked
    (re.compile(r"^(\s*)- \[[xX]\] (.+)$"), r"\1• ☑ \2"),  # task list, checked
    (re.compile(r"^(\s*)- (.+)$"), r"\1• \2"),  # bullet list
    # A label may not contain brackets and a URL may not contain spaces or
    # parentheses. With `.` on both sides these matched from the FIRST bracket in
    # the line, so "[IPA] Team — [link](url)" became one link labelled
    # "IPA] Team — [link" — and page titles that carry a tag in brackets are
    # exactly what a search result is full of.
    (re.compile(r"!\[[^\][]*\]\(([^()\s]+)\)"), r"<\1>"),  # image -> bare URL
    (re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)"), r"_\1_"),  # *italic* -> _italic_
    (re.compile(r"^#{1,6} (.+)$"), r"*\1*"),  # heading -> bold line
    (re.compile(r"(?<!\*)\*\*(.+?)\*\*(?!\*)"), r"*\1*"),  # **bold** -> *bold*
    (re.compile(r"__(.+?)__"), r"*\1*"),  # __underline__ -> bold
    (re.compile(r"\[([^\][]*)\]\(([^()\s]+)\)"), r"<\2|\1>"),  # [text](url) -> <url|text>
    (re.compile(r"~~(.+?)~~"), r"~\1~"),  # ~~strike~~ -> ~strike~
]


def markdown_to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn; return the input unchanged on error."""
    try:
        return _convert(text)
    except Exception:
        logger.warning("markdown->mrkdwn conversion failed; sending as-is", exc_info=True)
        return text


def _convert(text: str) -> str:
    out: list[str] = []
    prose: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            if not in_code:
                out.extend(_convert_prose(prose))
                prose = []
            out.append(line)
            in_code = not in_code
        elif in_code:
            out.append(line)
        else:
            prose.append(line)
    out.extend(_convert_prose(prose))
    return "\n".join(out)


def _convert_prose(lines: list[str]) -> list[str]:
    if not lines:
        return []
    # Tables first (they emit multi-line bullets), then per-line rules.
    flattened = "\n".join(_tables_to_lists(lines)).split("\n")
    return [_convert_line(line) for line in flattened]


def _convert_line(line: str) -> str:
    # Shield inline code spans: emphasis/link rules must not touch `snake_case*`.
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x01{len(spans) - 1}\x01"

    masked = _INLINE_CODE_RE.sub(_stash, line)
    if _HR_RE.match(masked):
        converted = "──────────"
    else:
        # Collapse 3+ asterisk runs (***bold-italic***, malformed ****) to plain
        # bold before the rules — weak models emit these constantly.
        converted = re.sub(r"\*{3,}", "**", masked)
        for pattern, replacement in _RULES:
            converted = pattern.sub(replacement, converted)
        converted = _strip_orphan_asterisks(converted)
    return re.sub("\x01(\\d+)\x01", lambda m: spans[int(m.group(1))], converted)


def _strip_orphan_asterisks(line: str) -> str:
    """Valid mrkdwn bolds with single asterisks, so surviving ** runs and an
    unpaired dangling * are leftovers of malformed model output — drop them
    (a trailing one first, else a leading one) so no literal star renders."""
    line = re.sub(r"\*{2,}", "*", line)
    if line.count("*") % 2 == 1:
        stripped = line.rstrip()
        if stripped.endswith("*"):
            line = stripped[:-1] + line[len(stripped):]
        elif line.lstrip().startswith("*"):
            indent = line[: len(line) - len(line.lstrip())]
            line = indent + line.lstrip()[1:]
    return line


# -- Markdown tables -> lists (Slack cannot render tables) ---------------------


def _tables_to_lists(lines: list[str]) -> list[str]:
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and _is_separator(lines[i + 1])
        ):
            header = _split_row(line)
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                result.append(_row_to_bullet(_split_row(lines[j]), header))
                j += 1
            i = j
        else:
            result.append(line)
            i += 1
    return result


def _is_separator(line: str) -> bool:
    cleaned = line.strip().lstrip("|").rstrip("|").strip()
    return bool(cleaned) and "-" in cleaned and all(c in "-:| \t" for c in cleaned)


def _split_row(line: str) -> list[str]:
    cleaned = line.strip()
    cleaned = cleaned.removeprefix("|").removesuffix("|")
    # Mask <url|text> / <@U…> so a pipe inside them isn't a column break.
    tokens: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"\x00{len(tokens) - 1}\x00"

    masked = _ANGLE_TOKEN_RE.sub(_stash, cleaned)
    restore = lambda value: re.sub(  # noqa: E731
        "\x00(\\d+)\x00", lambda m: tokens[int(m.group(1))], value
    )
    return [restore(cell.strip()) for cell in masked.split("|")]


def _row_to_bullet(row: list[str], header: list[str]) -> str:
    """One table row -> a bullet (numbered when the first column is ordinal),
    remaining cells as indented `Header: value` sub-lines. Emitted as Markdown;
    the per-line rules then restyle it (- -> •, ** -> *)."""
    if not row or not any(row):
        return "-"
    first = row[0]
    if first.isdigit() and len(row) > 1 and row[1]:
        prefix = f"{first}. **{row[1]}**"
        rest_start = 2
    elif first:
        prefix = f"- **{first}**"
        rest_start = 1
    else:
        prefix = "-"
        rest_start = 1
    parts = [prefix]
    for k in range(rest_start, len(row)):
        value = row[k]
        if not value:
            continue
        key = header[k] if k < len(header) else ""
        parts.append(f"    {key}: {value}" if key else f"    {value}")
    return "\n".join(parts)
