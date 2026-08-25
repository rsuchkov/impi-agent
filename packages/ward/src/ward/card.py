"""How a request for a credential is put to a human.

Everything general — the registry of waiting requests, the three answers, the
window ladder, the safe rendering — lives in ``crucible.approvals``. What is
left here is the wording: which fields a secret request shows, and in what
order.
"""

from crucible.approvals.card import command_line, render_card


def approval_text(
    agent: str, *, references: tuple[str, ...], reason: str, command: tuple[str, ...]
) -> str:
    """The message a human reads before deciding.

    Every value except the agent's own name comes from the caller, so the card
    goes through ``render_card``, which lets a caller supply content and never
    structure — see the reasoning in ``approvals/card.py``.

    The command is not decoration. A caller allowed to bind a secret into a
    child process is also allowed to bind it into ``sh -c 'echo $TOKEN'``, so
    the argv shown here is the only thing standing between an approval and an
    exfiltration. It is also only a *claim*: nothing forces the caller to run
    what it said it would.
    """
    label = "Secret" if len(references) == 1 else "Secrets"
    return render_card(
        f"🔐 **{agent}** is asking for a secret.",
        [(label, ", ".join(references)), ("Reason", reason)],
        block_label="Command",
        block=command_line(command),
    )


def verdict_text(
    verdict: str, agent: str, *, references: tuple[str, ...], command: tuple[str, ...]
) -> str:
    """What the card is rewritten to once it has been answered.

    It carries the command as well as the verdict, because this message is the
    history: months later "assistant was allowed github-token for 15 minutes"
    does not say what for, and `gh release create v1.2.0` does.

    Same containment as the request it replaces — the argv is still the
    caller's text, and a card that stopped escaping it once answered would just
    move the forgery one click later.
    """
    label = "Secret" if len(references) == 1 else "Secrets"
    return render_card(
        f"🔐 {verdict} — asked by **{agent}**.",
        [(label, ", ".join(references))],
        block_label="Command",
        block=command_line(command),
    )


def notice_text(
    agent: str,
    *,
    references: tuple[str, ...],
    command: tuple[str, ...],
    rules: tuple[str, ...],
    repeats: int = 1,
) -> str:
    """What an approver is told after a secret was handed over without them.

    Same containment as the card it replaces. A rule making a command automatic
    does not make the command trustworthy — the argv is still the caller's text,
    and this message is now the only place a human sees it.

    ``repeats`` folds a run of grants into the message already posted: a task on
    a schedule would otherwise fill the conversation, and a notice nobody reads
    is the same as no notice at all.
    """
    label = "Secret" if len(references) == 1 else "Secrets"
    fields = [
        (label, ", ".join(references)),
        ("Rule" if len(rules) == 1 else "Rules", ", ".join(rules)),
    ]
    if repeats > 1:
        # Plain text, not a field the caller can influence: it is the engine's
        # own count.
        fields.append(("Times", f"{repeats} so far"))
    return render_card(
        f"🤖 **{agent}** took a secret automatically.",
        fields,
        block_label="Command",
        block=command_line(command),
    )
