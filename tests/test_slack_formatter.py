"""Pure tests for the Slack Markdown -> mrkdwn formatter (no SDK, offline)."""

import crucible.gateways.slack.formatter as formatter
from crucible.gateways.slack.formatter import markdown_to_mrkdwn

# --- emphasis / headings / links ---------------------------------------------


def test_bold_and_italic_convert() -> None:
    assert markdown_to_mrkdwn("**bold** and *italic*") == "*bold* and _italic_"


def test_headings_become_bold_lines() -> None:
    assert markdown_to_mrkdwn("# Title") == "*Title*"
    assert markdown_to_mrkdwn("### Sub") == "*Sub*"
    assert markdown_to_mrkdwn("###### Deep") == "*Deep*"


def test_markdown_link_becomes_slack_link() -> None:
    assert markdown_to_mrkdwn("see [docs](https://example.com)") == "see <https://example.com|docs>"


def test_image_becomes_bare_url() -> None:
    assert markdown_to_mrkdwn("![alt](https://x.com/i.png)") == "<https://x.com/i.png>"


def test_strike_underline_hr_task_lists() -> None:
    assert markdown_to_mrkdwn("~~gone~~") == "~gone~"
    assert markdown_to_mrkdwn("__underlined__") == "*underlined*"
    assert markdown_to_mrkdwn("---") == "──────────"
    assert markdown_to_mrkdwn("- [ ] todo\n- [x] done") == "• ☐ todo\n• ☑ done"


def test_bullets_become_dots_ordered_kept() -> None:
    assert markdown_to_mrkdwn("- one\n  - nested\n1. first") == "• one\n  • nested\n1. first"


def test_existing_slack_tokens_survive() -> None:
    text = "ping <@U123> and <https://x.com|the name>"
    assert markdown_to_mrkdwn(text) == text


def test_blockquote_kept() -> None:
    assert markdown_to_mrkdwn("> quoted line") == "> quoted line"


# --- malformed LLM output ------------------------------------------------------


def test_triple_and_quad_asterisks_collapse_to_bold() -> None:
    assert markdown_to_mrkdwn("***both***") == "*both*"
    assert markdown_to_mrkdwn("****strong****") == "*strong*"


def test_orphan_asterisks_end_up_balanced() -> None:
    # Real weak-model output: unbalanced / dangling asterisk runs. The result
    # must never leave an odd count that Slack renders as literal stars.
    assert markdown_to_mrkdwn("Hello **world*") == "Hello *world*"
    for garbled in ("Result:**", "**Итог: готово", "a *b* c*"):
        assert markdown_to_mrkdwn(garbled).count("*") % 2 == 0


# --- tables ----------------------------------------------------------------------


def test_simple_table_flattens_to_bullets() -> None:
    text = "| Name | Count |\n|------|------|\n| Foo | 13 |\n| Bar | 2 |"
    assert markdown_to_mrkdwn(text) == (
        "• *Foo*\n    Count: 13\n• *Bar*\n    Count: 2"
    )


def test_numeric_first_column_becomes_numbered_list() -> None:
    text = "| № | Name | State |\n|---|------|-------|\n| 1 | Foo | OK |"
    assert markdown_to_mrkdwn(text) == "1. *Foo*\n    State: OK"


def test_table_cell_with_slack_link_pipe_survives() -> None:
    text = "| Name | Link |\n|------|------|\n| Foo | <https://x.com/a|b c> |"
    out = markdown_to_mrkdwn(text)
    assert "<https://x.com/a|b c>" in out


def test_pipes_in_prose_left_alone() -> None:
    assert markdown_to_mrkdwn("a | b | c") == "a | b | c"


def test_text_around_table_preserved() -> None:
    text = "before\n\n| A | B |\n|---|---|\n| x | y |\n\nafter"
    out = markdown_to_mrkdwn(text)
    assert out.startswith("before\n\n• *x*")
    assert out.endswith("\n\nafter")


# --- code protection ---------------------------------------------------------------


def test_fenced_code_block_untouched() -> None:
    code = "```python\ndef f(**kwargs):\n    return [a](b) # not a link\n# not a heading\n- not a bullet\n```"
    assert markdown_to_mrkdwn(code) == code


def test_prose_around_fence_converted_fence_kept() -> None:
    text = "**intro**\n```\n**raw** | pipe\n```\n**outro**"
    assert markdown_to_mrkdwn(text) == "*intro*\n```\n**raw** | pipe\n```\n*outro*"


def test_table_inside_fence_untouched() -> None:
    text = "```\n| A | B |\n|---|---|\n| x | y |\n```"
    assert markdown_to_mrkdwn(text) == text


def test_unclosed_fence_passes_through() -> None:
    text = "before **bold**\n```\n**inside** stays"
    assert markdown_to_mrkdwn(text) == "before *bold*\n```\n**inside** stays"


def test_inline_code_shielded_from_rules() -> None:
    assert markdown_to_mrkdwn("use `snake_case*` here") == "use `snake_case*` here"
    assert markdown_to_mrkdwn("`**not bold**` but **bold**") == "`**not bold**` but *bold*"


# --- safety ---------------------------------------------------------------------------


def test_conversion_error_returns_original(monkeypatch) -> None:
    def boom(lines):
        raise RuntimeError("regex meltdown")

    monkeypatch.setattr(formatter, "_tables_to_lists", boom)
    text = "**anything** at all"
    assert markdown_to_mrkdwn(text) == text


def test_a_bracketed_title_before_a_link_is_not_swallowed_into_it() -> None:
    """`.` matches `]`, so a lazy label still ran from the FIRST bracket in the
    line to the real link: a Confluence result whose page title carries a tag —
    and they routinely do — came out as one link labelled with the whole line."""
    out = markdown_to_mrkdwn(
        "[IPA] Team (space Information Technology) — [ссылка](https://x/y)"
    )
    assert out == "[IPA] Team (space Information Technology) — <https://x/y|ссылка>"


def test_two_links_on_one_line_stay_two_links() -> None:
    assert markdown_to_mrkdwn("см. [a](http://a) и [b](http://b)") == (
        "см. <http://a|a> и <http://b|b>"
    )


def test_brackets_without_a_link_are_left_alone() -> None:
    assert markdown_to_mrkdwn("просто [скобки] без ссылки") == "просто [скобки] без ссылки"


def test_a_bracketed_title_before_an_image_is_not_swallowed_either() -> None:
    assert markdown_to_mrkdwn("[IPA] ![shot](http://i/p.png)") == "[IPA] <http://i/p.png>"


def test_a_url_may_not_contain_spaces() -> None:
    """A space inside the parentheses means it was never a link — matching it
    would eat the rest of the line looking for a closing bracket."""
    assert markdown_to_mrkdwn("[not](a link) here") == "[not](a link) here"
