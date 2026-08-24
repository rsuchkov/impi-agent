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
