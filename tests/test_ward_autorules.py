"""Which commands a rule covers (ward/autorules.py).

Pure functions, so this is the cheap place to be exhaustive — and the place it
matters most: a rule decides whether a credential is handed over without anyone
watching, so what it does NOT cover has to be as certain as what it does.
"""

import pytest

from ward.autorules import (
    RuleError,
    decode,
    encode,
    matches,
    matching,
    parse,
    unparse,
)


def rule(text: str) -> tuple[str, ...]:
    return parse(text)


# -- what a rule covers -------------------------------------------------------------


def test_an_exact_command_matches_itself() -> None:
    assert matches(rule("python kadence.py"), ("python", "kadence.py"))


def test_a_trailing_star_takes_any_arguments() -> None:
    covered = rule("python kadence.py *")
    assert matches(covered, ("python", "kadence.py", "--sync"))
    assert matches(covered, ("python", "kadence.py", "--sync", "--verbose"))
    # Including none: "and any arguments" reads as "zero or more".
    assert matches(covered, ("python", "kadence.py"))


@pytest.mark.parametrize(
    "command",
    [
        ("python", "kadence.py", "--sync"),  # an extra argument is another command
        ("python", "other.py"),
        ("python",),
        ("python3", "kadence.py"),  # a different interpreter
        ("/usr/bin/python", "kadence.py"),  # spelled by path
        ("sh", "-c", "python kadence.py"),  # through a shell
        (),
    ],
)
def test_without_a_star_nothing_else_matches(command: tuple[str, ...]) -> None:
    assert not matches(rule("python kadence.py"), command)


def test_the_prefix_must_match_element_by_element() -> None:
    """Not a string comparison: a caller controls its own quoting, so a joined
    command would let quoting decide an authorization question."""
    covered = rule("python kadence.py *")
    assert not matches(covered, ("python kadence.py",))  # one element, not two
    assert not matches(covered, ("python", "kadence.pyx"))


def test_a_command_taking_a_literal_star_cannot_be_ruled_about() -> None:
    """Quoting cannot be trusted to say which `*` is a wildcard — shlex leaves
    `'*'` and `*` identical — and a token that decides an authorization question
    must not be ambiguous. So the rule is refused and the command gets a card,
    which is the safe direction to fail in."""
    for text in ("ls '*' here", "ls * here"):
        with pytest.raises(RuleError):
            parse(text)


def test_an_empty_rule_covers_nothing() -> None:
    assert not matches((), ("python",))


# -- what may be written ------------------------------------------------------------


def test_quoting_survives_so_an_argument_may_contain_a_space() -> None:
    assert rule("say 'hello world' *") == ("say", "hello world", "*")


def test_a_bare_star_is_refused() -> None:
    """It matches everything, which is `approval: never` in a rule's clothes:
    the policy would look restricted and would not be."""
    with pytest.raises(RuleError) as raised:
        parse("*")
    assert "never" in str(raised.value)


def test_a_star_in_the_middle_is_refused() -> None:
    with pytest.raises(RuleError) as raised:
        parse("python * --sync")
    assert "last element" in str(raised.value)


@pytest.mark.parametrize("text", ["", "   ", "'unbalanced"])
def test_a_rule_that_says_nothing_usable_is_refused(text: str) -> None:
    with pytest.raises(RuleError):
        parse(text)


# -- naming the rule that fired -----------------------------------------------------


def test_the_first_covering_rule_is_named() -> None:
    """The ledger records why a secret was handed over, so the answer has to be
    the rule itself rather than "some rule matched"."""
    rules = (rule("python other.py *"), rule("python kadence.py *"))
    assert matching(rules, ("python", "kadence.py", "--sync")) == "python kadence.py *"
    assert matching(rules, ("curl", "https://evil")) == ""


def test_a_named_rule_reads_as_it_was_written() -> None:
    assert unparse(rule("say 'hello world' *")) == "say 'hello world' *"
    assert unparse(rule("python kadence.py")) == "python kadence.py"


# -- the round trip through the store ------------------------------------------------


def test_rules_survive_storage_including_awkward_arguments() -> None:
    rules = (rule("python kadence.py *"), rule("say 'hello world'"))
    assert decode(encode(rules)) == rules


def test_a_corrupted_line_costs_a_card_rather_than_an_outage() -> None:
    """A policy that could not be read at all would refuse every request. A rule
    that got mangled should mean the command is asked about, nothing worse."""
    stored = encode((rule("python kadence.py *"),)) + "\nnot json at all\n"
    assert decode(stored) == (("python", "kadence.py", "*"),)


def test_nothing_stored_is_no_rules() -> None:
    assert decode("") == ()
    assert matching(decode(""), ("python", "kadence.py")) == ""
