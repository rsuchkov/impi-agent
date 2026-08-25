"""Which commands may take a secret without asking a human.

A policy has two settings today and nothing between them: ask every time, or
never ask at all. An agent that runs one known script on a schedule fits
neither — it either wears the operator out or gets a credential for anything it
cares to do with it. A rule is that middle: this secret, for this command, no
card.

**What a rule is worth, exactly.** ``secret-exec`` replaces itself with the argv
it declared, so for a caller that goes through it the declared command is the
command that runs: to put a token into ``curl`` a model must say ``curl``, and
that will not match. The rule binds the model.

It does not bind an attacker. The certificate an agent proves itself with is
readable by everything in its container, and the protocol is HTTPS and JSON, so
anything there can claim any argv it likes. A rule narrows what an honest caller
may do quietly; against a compromised container it is worth nothing, and the
docs say so in those words.

Matching is by argv element, never by a joined string. Joining invites quoting
to decide security questions — ``--flag "a b"`` and ``--flag a b`` render alike
and are not alike — and a caller controls its own quoting.
"""

import json
import shlex

# The one wildcard: as the last element it stands for "and any arguments after
# this". Anywhere else a rule is refused rather than read literally.
#
# The reason is that quoting cannot be trusted to tell them apart: `shlex.split`
# leaves `'*'` and `*` identical, so a rule whose meaning turned on the quotes
# would be a rule where quoting decides an authorization question — the same
# thing avoided by matching argv elements instead of a joined line. The cost is
# that a command taking a literal `*` cannot be described by a rule, and must be
# asked about. That is the safe direction to fail in.
ANY_REST = "*"


class RuleError(ValueError):
    """A rule that cannot be stored, with the reason an operator can act on."""


def parse(text: str) -> tuple[str, ...]:
    """One written rule -> its argv.

    Shell quoting, so a rule can name an argument that contains a space, and so
    that writing a rule feels like writing the command it describes.
    """
    try:
        argv = tuple(shlex.split(text))
    except ValueError as exc:
        raise RuleError(f"cannot read that rule ({exc}): {text!r}") from exc
    if not argv:
        raise RuleError("an empty rule matches nothing — remove it instead")
    if argv == (ANY_REST,):
        # It would match every command, which is `approval: never` wearing a
        # rule's clothes: the policy would look restricted and would not be.
        raise RuleError(
            "a rule of just `*` matches every command — that is `--approval never`, "
            "and saying so plainly is better than a rule that only looks like a limit"
        )
    if ANY_REST in argv[:-1]:
        raise RuleError(
            f"`{ANY_REST}` is only meaningful as the last element (it means "
            f"'and any arguments after this'): {text!r}"
        )
    return argv


def matches(rule: tuple[str, ...], command: tuple[str, ...]) -> bool:
    """Whether ``command`` is what ``rule`` describes.

    Element by element. A trailing ``*`` takes the rest — including nothing at
    all, so ``python x.py *`` covers ``python x.py``. Without it the lengths
    must agree: an extra argument is a different command.
    """
    if not rule:
        return False
    if rule[-1] == ANY_REST:
        head = rule[:-1]
        return command[: len(head)] == head
    return command == rule


def matching(rules: tuple[tuple[str, ...], ...], command: tuple[str, ...]) -> str:
    """The first rule that covers ``command``, written back out, or "".

    Written back rather than returned as argv because every caller wants it as
    text — the ledger row that says why a secret was handed over, and the notice
    a human reads afterwards.
    """
    for rule in rules:
        if matches(rule, command):
            return unparse(rule)
    return ""


def unparse(rule: tuple[str, ...]) -> str:
    """A rule as an operator wrote it. ``*`` stays bare — quoting it would make
    the one element that is not a literal look like one."""
    return " ".join(part if part == ANY_REST else shlex.quote(part) for part in rule)


def encode(rules: tuple[tuple[str, ...], ...]) -> str:
    """Rules for the store: JSON, one rule per line.

    JSON rather than the written form, so a token containing a space survives
    the round trip without the store having to know about shell quoting.
    """
    return "\n".join(json.dumps(list(rule)) for rule in rules)


def decode(stored: str) -> tuple[tuple[str, ...], ...]:
    """Back from the store. A line that will not parse is dropped rather than
    raised on: a policy that cannot be read at all would refuse every request,
    and a rule that got corrupted should cost a card, not an outage."""
    rules = []
    for line in stored.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            argv = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(argv, list) and argv and all(isinstance(p, str) for p in argv):
            rules.append(tuple(argv))
    return tuple(rules)
