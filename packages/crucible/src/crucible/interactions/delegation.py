"""Handing a task to another agent where the conversation is.

An agent that wants another's work — a model that knows the people's data
and may not pass it on, asking a stronger one to write the code — does not
reach into that agent: it posts the task in the thread, as it would ask a
colleague, and the other agent's own gateway opens a turn on the mention.
The answer comes back into the same thread, for everyone there to read, and
reaches the asker as a message from an agent. Asynchronous by design: the
tool call ends when the task is posted, the turn goes on, and the reply is a
later message.

What leaves the asker is exactly the text and the files it posted, which is
why the tool that calls this is confirmed: the person reads what goes to
the other agent, and its model, before it goes.
"""

import logging
from collections.abc import Sequence

from crucible.interactions.presence import AgentPresence
from crucible.interactions.service import turn_ref
from crucible.ports.chat.delegation import DelegationError
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.files import FileError, FileService
from crucible.ports.turn import TurnPlaces
from crucible.store.base import SessionStore

logger = logging.getLogger(__name__)

# The cascade counter a handed-on task starts at: one hop past the person,
# so the answer's answer stays within the loop guard's reach.
DELEGATION_HOP = 1


class ChatDelegationService:
    """``DelegationService`` over the agents' chat presence: the task is a
    reply in the turn's place, as the asking agent, addressing the other
    agent by the mention its gateway recognizes; files, when given, travel
    with it through the file service, under the same rules as ``send_file``."""

    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        directory: AgentDirectory,
        *,
        files: FileService | None = None,
        places: TurnPlaces | None = None,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._directory = directory
        self._files = files
        self._places = places

    async def delegate(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
    ) -> None:
        other = next((a for a in self._directory.list_agents() if a.name == to), None)
        if other is None:
            raise DelegationError(f"no agent named {to!r} runs here")
        if other.name == agent:
            raise DelegationError("that is you; hand the task to another agent")
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent, record.last_gateway) if record else None
        if record is None or poster is None:
            raise DelegationError("this conversation could not be resolved — nothing was posted")
        place = self._places.place(runtime_session_id) if self._places is not None else None
        ref = turn_ref(record, record.last_gateway, place)
        text = f"{poster.format_mention(other.username)} {task}".strip()
        if paths:
            if self._files is None:
                raise DelegationError("sending files is turned off in this deployment")
            try:
                # The files and the task in one message, so the other agent
                # receives them with the mention that opens its turn.
                await self._files.send(agent, runtime_session_id, list(paths), text=text)
            except FileError as exc:
                raise DelegationError(str(exc)) from exc
            logger.info("agent %s handed a task to %s with %d file(s)", agent, to, len(paths))
            return
        await poster.post_reply(ref, text, hop_depth=DELEGATION_HOP)
        logger.info("agent %s handed a task to %s", agent, to)
