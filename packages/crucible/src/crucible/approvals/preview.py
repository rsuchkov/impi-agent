"""What a human is shown before saying yes to a tool call — as the system
knows it, not as the model described it.

The card a confirmation is decided from is the last line of defence against a
call the model was talked into: arguments are the model's account of what it
wants, and the model reads text other people wrote. A preview is built by the
tool itself from what it can see — the record as it stands, the files that
would land, the name behind an id — so the person deciding sees ``In Progress
→ Done`` rather than ``{"transition": "31"}``. Plain data here; each channel
draws it its own way, and ``approvals.card`` is the one that knows Markdown.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewRow:
    """One labelled line. ``before`` is the current value when the call would
    change it, so the card can show the change rather than only the target."""

    label: str
    value: str
    before: str = ""


@dataclass(frozen=True)
class CallPreview:
    """What the call would do: a one-line title, labelled rows, and whether it
    deserves a warning (an irreversible step, a file that will run)."""

    title: str
    rows: tuple[PreviewRow, ...] = ()
    danger: bool = False
