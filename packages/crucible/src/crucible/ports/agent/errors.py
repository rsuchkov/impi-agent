"""Runtime-agnostic agent errors + the user-facing fallback texts shown when a
run fails. A concrete runtime subclasses these so flows can catch the generic
types without depending on the runtime."""


class AgentError(Exception):
    """Base class for any agent-runtime failure."""


class AgentTimeout(AgentError):
    """A turn did not produce a final result within the allotted time."""


# Fallback texts surfaced to the user when the runtime fails or has nothing.
LLM_FALLBACK_MESSAGE = "Sorry, the model is temporarily unavailable — please try again in a minute."
INTERNAL_ERROR_MESSAGE = "Sorry, something broke on my side. Please try again later."
