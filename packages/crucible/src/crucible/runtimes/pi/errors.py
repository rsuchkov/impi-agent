"""Errors for the pi runtime driver.

These subclass the runtime-agnostic ``crucible.ports.agent`` errors so flows can catch
``AgentError``/``AgentTimeout`` without knowing the runtime is pi."""

from crucible.ports.agent.errors import AgentError, AgentTimeout


class PiError(AgentError):
    """Base class for all pi-driver failures."""


class PiProtocolError(PiError):
    """A line from pi could not be parsed, or violated the JSONL framing."""


class PiProcessError(PiError):
    """The pi subprocess died, exited non-zero, or its pipe broke."""


class PiTimeout(PiError, AgentTimeout):
    """A prompt did not produce a final result within the allotted time."""


class PiHostError(PiError):
    """The host that was supposed to run the process could not be reached, or
    refused the spawn. Distinct from a process that died: nothing started, and
    the fix is a deployment one (the agent's container is down, its token is
    wrong, its protocol version does not match)."""
