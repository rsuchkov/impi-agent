"""Which kind of failure a notice reports."""

import pytest

from crucible.ports.agent.errors import (
    MODEL_UNAVAILABLE_MESSAGE,
    NOTICE_AGENT_ERROR,
    NOTICE_CONTEXT,
    NOTICE_CREDENTIALS,
    NOTICE_MODEL_UNAVAILABLE,
    NOTICE_QUOTA,
    NOTICE_TIMEOUT,
    AgentError,
    AgentTimeout,
    notice_for,
)


@pytest.mark.parametrize("text", [
    "pi LLM error: 503 <html><body>Service Temporarily Unavailable</body></html>",
    "pi LLM error: 502 Bad Gateway",
    "pi LLM error: Connection error.",
    "pi LLM error: Request timed out.",
    "pi LLM error: 529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}",
    "pi LLM error: fetch failed (ECONNREFUSED 10.0.0.5:443)",
    "500 Internal Server Error",
])
def test_a_model_service_that_fails_or_cannot_be_reached_is_not_our_fault(text: str) -> None:
    """The request was fine; the service behind the model was not. "Something
    broke on my side" sent people — and the operator — to the wrong place."""
    assert notice_for(AgentError(text)) == (NOTICE_MODEL_UNAVAILABLE, MODEL_UNAVAILABLE_MESSAGE)


@pytest.mark.parametrize(("text", "code"), [
    # A more specific kind wins, even in a 5xx-shaped body.
    ("pi LLM error: 500 insufficient_quota", NOTICE_QUOTA),
    ("pi LLM error: 503 No API key found for provider", NOTICE_CREDENTIALS),
    ("pi LLM error: 500 prompt is too long", NOTICE_CONTEXT),
    # A number that is not a status: a size, an id.
    ("pi LLM error: tool output of 5000 bytes was rejected", NOTICE_AGENT_ERROR),
    ("pi process exited unexpectedly (code 1): TypeError in session 512a", NOTICE_AGENT_ERROR),
])
def test_other_failures_keep_their_kind(text: str, code: str) -> None:
    assert notice_for(AgentError(text))[0] == code


def test_a_turn_that_ran_out_of_time_is_still_a_timeout() -> None:
    assert notice_for(AgentTimeout("turn exceeded 300s"))[0] == NOTICE_TIMEOUT
