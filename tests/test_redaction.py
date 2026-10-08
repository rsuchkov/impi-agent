"""Keeping secrets out of logs and error reports."""

import logging
import re

import pytest

from crucible.redaction import REDACTED, ScrubbingFilter, scrub, scrub_data


@pytest.mark.parametrize(("text", "kept", "gone"), [
    # The tool server's own: by header and by variable.
    ("X-Tool-Token: 0123abcd0123abcd", "X-Tool-Token: ", "0123abcd0123abcd"),
    ("x-session-proof: f00df00df00d", "x-session-proof: ", "f00df00df00d"),
    ("TOOL_TOKEN=0123abcd TOOL_SESSION_PROOF=f00d", "TOOL_TOKEN=", "0123abcd"),
    ("TOOL_TOKEN=0123abcd TOOL_SESSION_PROOF=f00d", "TOOL_SESSION_PROOF=", "f00d"),
    # An agent's token, per-agent suffix and all.
    ("AGENTS_MM_TOKEN__SUPPORT=8xk3jd", "AGENTS_MM_TOKEN__SUPPORT=", "8xk3jd"),
    ("Authorization: Bearer eyJhbGciOi.payload.sig", "Authorization: ", "eyJhbGciOi"),
    ("got bearer abc.def.ghi back", "got bearer ", "abc.def.ghi"),
    ('{"access_token": "ya29.a0", "expires_in": 3600}', '"access_token": "', "ya29.a0"),
    ("{'clientSecret': 's3cr3t'}", "'clientSecret': '", "s3cr3t"),
    ("GET /cb?code_verifier=abc123&state=x", "code_verifier=", "abc123"),
    ("mongodb://admin:hunter2@db:27017/app", "mongodb://admin:", "hunter2"),
    ("POST /Zm9vYmFyYmF6cXV4MTIzNDU2/interact 200", "POST /", "Zm9vYmFyYmF6cXV4MTIzNDU2"),
    ("token is xoxb-1234567890-abcdefghij", "token is ", "xoxb-1234567890-abcdefghij"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
     "-----BEGIN RSA PRIVATE KEY-----", "MIIEow"),
])
def test_secrets_go_and_their_names_stay(text: str, kept: str, gone: str) -> None:
    out = scrub(text)
    assert gone not in out and kept in out and REDACTED in out


@pytest.mark.parametrize("text", [
    "max_tokens=4096 temperature=0.2",
    '{"max_tokens": 4096, "model": "gpt"}',
    "POST /interact 200",  # no secret segment
    "https://example.com/docs/page",
    "the turn used 1234 tokens",
    "the receiver has no secret: every callback is trusted",
    "interact callback: card 0844270f… form=False",
])
def test_what_only_looks_like_a_secret_is_left_alone(text: str) -> None:
    assert scrub(text) == text


def test_an_application_adds_its_own_patterns() -> None:
    out = scrub("user key-ABCDEF1234 asked", extra=[r"(key-)[A-Z0-9]+", re.compile(r"asked")])
    assert out == "user key-*** ***"


def test_an_error_report_is_scrubbed_through_and_through() -> None:
    event = {
        "request": {"headers": {"X-Tool-Token": "abc", "Cookie": "sid=1", "Accept": "json"},
                    "url": "https://h/cb?access_token=zzz"},
        "extra": {"max_tokens": 4096, "privateKey": {"n": 1}, "githubToken": "ghp_x"},
        "breadcrumbs": [("login", "password=pw1")],
    }
    out = scrub_data(event)
    assert out["request"]["headers"] == {"X-Tool-Token": REDACTED, "Cookie": REDACTED, "Accept": "json"}
    assert out["request"]["url"] == "https://h/cb?access_token=***"
    assert out["extra"] == {"max_tokens": 4096, "privateKey": REDACTED, "githubToken": REDACTED}
    assert out["breadcrumbs"] == [("login", "password=***")]


def test_a_log_record_is_scrubbed_with_its_arguments_and_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction")
    logger.addFilter(ScrubbingFilter())
    try:
        with caplog.at_level(logging.INFO, logger="test.redaction"):
            logger.info("calling with %s", "TOOL_TOKEN=deadbeef")
            try:
                raise RuntimeError("Authorization: Bearer leaked.jwt.value")
            except RuntimeError:
                logger.exception("failed")
    finally:
        logger.filters.clear()
    assert "deadbeef" not in caplog.text and "TOOL_TOKEN=***" in caplog.text
    assert "leaked.jwt.value" not in caplog.text and "Traceback" in caplog.text
