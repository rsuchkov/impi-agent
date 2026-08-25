"""What the interactions layer asks of collaborators an application may bring.

Optional, all of it. The engine passes nothing here and behaves exactly as it
did before these existed; an application that answers its own commands passes an
implementation and takes over one specific decision.
"""

from collections.abc import Mapping
from typing import Protocol

from crucible.store.base import FormRecord


class FormHandler(Protocol):
    """Answer a modal form the application opened, instead of an agent.

    A form's values normally become a synthetic message in the conversation it
    was opened in — that is what a tool asking a human for input wants. An
    application that answers its own commands wants the opposite: the values are
    for it, and turning them into a message would be wrong twice over when one
    of them is a credential.

    Which handler answers a form is decided when the form is WRITTEN
    (``FormRecord.handler``), not by asking handlers whether a token is theirs.
    So this is only ever called for a form that named it, and it does not report
    ownership — an exception here is logged and the values are dropped, never
    passed on to somebody else.

    The submission arrives from the platform, so ``user_id`` is the only thing
    saying who filled it in. A handler that guards anything checks it here
    rather than trusting the click that opened the dialog.
    """

    async def handle(
        self, record: FormRecord, values: Mapping[str, str], user_id: str
    ) -> None: ...


class FormHandlers:
    """The handlers an application registered, keyed by the name a form names.

    The same shape as ``ScreenRegistry``: a registry keyed by a word, and the
    payload says which entry. A form whose ``handler`` is empty — every form an
    agent opens — never reaches this at all.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, FormHandler] = {}

    def register(self, name: str, handler: FormHandler) -> None:
        self._handlers[name] = handler

    def get(self, name: str) -> FormHandler | None:
        return self._handlers.get(name)

    def __bool__(self) -> bool:
        return bool(self._handlers)
