"""Admission by gateway: one rule for the conversations that come through one
gateway, none for the rest.

An agent reached through two gateways may need a person to sign in on one
and not the other — a chat whose session lives for months beside a system
that signs its people in itself. ``AdmissionByGateway`` asks the rule named
for the message's gateway and admits every other message as it is.
"""

from collections.abc import Mapping

from crucible.ports.admission import NotAdmitted, TurnAdmission
from crucible.ports.chat.types import IncomingMessage


class AdmissionByGateway:
    """``by_gateway``: gateway kind (``ports.chat.gateway.GATEWAY_*``) -> the
    rule for turns opened there. A gateway not named admits every turn."""

    def __init__(self, by_gateway: Mapping[str, TurnAdmission]) -> None:
        self._by_gateway = dict(by_gateway)

    async def admit(self, agent: str, msg: IncomingMessage) -> NotAdmitted | None:
        rule = self._by_gateway.get(msg.gateway)
        return await rule.admit(agent, msg) if rule is not None else None
