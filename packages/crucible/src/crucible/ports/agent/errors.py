"""Runtime-agnostic agent errors + the user-facing text shown when a run fails.

A concrete runtime subclasses these so flows can catch the generic types without
depending on the runtime. Two of them exist only to be told apart in chat: the
engine being out of capacity and the runtime being unreachable are not the
model's fault, and saying "the model is unavailable" sends somebody to look in
the wrong place.
"""

import re


class AgentError(Exception):
    """Base class for any agent-runtime failure."""


class AgentTimeout(AgentError):
    """A turn did not produce a final result within the allotted time."""


class AgentBusy(AgentTimeout):
    """No capacity to start a turn — every slot is taken. A timeout in the sense
    the caller cares about (nothing ran, try later), but nothing is wrong with
    the model or the runtime."""


class AgentUnavailable(AgentError):
    """Whatever runs the agent could not be reached or started. A deployment
    fault, not a model one: the fix is bringing something back up."""


# Fallback texts surfaced to the user when the runtime fails or has nothing.
LLM_FALLBACK_MESSAGE = "Sorry, the model is temporarily unavailable — please try again in a minute."
INTERNAL_ERROR_MESSAGE = "Sorry, something broke on my side. Please try again later."
QUOTA_MESSAGE = (
    "The model refused this turn: the usage limit for this deployment is spent. "
    "It will answer again once the quota resets or somebody tops it up."
)
CREDENTIALS_MESSAGE = (
    "The model would not accept this deployment's credentials — somebody has to "
    "sign in again before I can answer."
)
CONTEXT_MESSAGE = (
    "This conversation has grown past what the model will accept in one go. "
    "Start a new one and I will have room again."
)
BUSY_MESSAGE = (
    "I am at capacity right now — every runtime slot is busy with another "
    "conversation. Try again in a minute."
)
UNAVAILABLE_MESSAGE = (
    "The runtime that answers for me is not reachable, so nothing ran. This one "
    "is for whoever looks after the deployment."
)

# What a provider's own wording looks like. Matching text is a heuristic and is
# meant to be: the string comes from somebody else's API and nobody versions it.
# Getting a match wrong costs a less specific sentence, never a wrong action —
# every branch here says "this turn did not happen" and differs only in what to
# do about it, so the fallback below is always a safe answer.
_QUOTA = re.compile(
    r"usage limit|quota|rate.?limit|too many requests|\b429\b|insufficient_quota|"
    r"credit balance|billing",
    re.I,
)
_CREDENTIALS = re.compile(
    r"no api key|api key|unauthor|\b401\b|authentication|invalid[_ ]api|"
    r"not logged in|/login|expired token",
    re.I,
)
_CONTEXT = re.compile(
    r"context[_ ]length|maximum context|context window|too many tokens|"
    r"prompt is too long",
    re.I,
)


def message_for(error: Exception) -> str:
    """The sentence a person should see for this failure.

    The cause is never shown verbatim. A notice is an ordinary message in the
    conversation, so everyone present reads it, and a provider's error carries
    model names, account identifiers and whatever the runtime left on stderr.
    The detail belongs in the log, where the operator is; what belongs here is
    which KIND of thing went wrong, because that is what decides who fixes it.
    """
    if isinstance(error, AgentUnavailable):
        return UNAVAILABLE_MESSAGE
    if isinstance(error, AgentBusy):
        return BUSY_MESSAGE
    text = str(error)
    if _QUOTA.search(text):
        return QUOTA_MESSAGE
    if _CREDENTIALS.search(text):
        return CREDENTIALS_MESSAGE
    if _CONTEXT.search(text):
        return CONTEXT_MESSAGE
    if isinstance(error, AgentTimeout):
        return LLM_FALLBACK_MESSAGE
    return INTERNAL_ERROR_MESSAGE
