"""The callback receiver's secret path: the one thing standing in for a
signature on a platform's callback."""

import logging
from pathlib import Path

import aiohttp
import pytest

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import (
    InteractionDispatcher,
    InteractionsServer,
    MappingPresence,
)
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.store.sqlite import SqliteSessionStore
from tests.fakes.presence import presence_of
from tests.test_commands import SinkSpy

SECRET = "k2Xy8qLmN0pRsTuVwZ"


async def _server(port: int, store, *, secret: str) -> InteractionsServer:
    dispatcher = InteractionDispatcher(
        store, presence_of(object(), sink=SinkSpy()), PendingUiRequests(), store  # type: ignore[arg-type]
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}),
        host="127.0.0.1", port=port, secret=secret,
    )
    await server.start()
    return server


async def _post(port: int, path: str, payload: dict) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}{path}", json=payload) as resp:
            return resp.status


async def test_nothing_answers_off_the_secret_path(tmp_path: Path) -> None:
    """Bare paths, a wrong secret, a right secret on another endpoint's shape:
    all 403. Only the exact path the platform was handed reaches a handler."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _server(8701, store, secret=SECRET)
    click = {"user_id": "u1", "context": {"token": "nope", "value": "Yes"}}
    try:
        assert await _post(8701, "/interact", click) == 403
        assert await _post(8701, "/dialog", {"state": "x", "submission": {}}) == 403
        assert await _post(8701, f"/{SECRET[:-1]}x/interact", click) == 403
        assert await _post(8701, f"/{SECRET}/interact", click) == 200  # routed: buttons retired
        assert await _post(8701, f"/{SECRET}/dialog", {"state": "x", "submission": {}}) == 200
    finally:
        await server.stop()
        await store.close()


async def test_the_command_route_also_lives_under_the_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A command off the path is refused by the guard, before the command token
    is even looked at; on the path it reaches the command handler."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _server(8702, store, secret=SECRET)
    try:
        with caplog.at_level(logging.WARNING, logger="crucible.interactions.server"):
            async with aiohttp.ClientSession() as s:
                async with s.post("http://127.0.0.1:8702/command/assistant", data={}) as r:
                    assert r.status == 403
                caplog.clear()
                async with s.post(f"http://127.0.0.1:8702/{SECRET}/command/assistant", data={}) as r:
                    assert r.status == 403  # the command handler: no tokens configured
        assert "secret path" not in caplog.text  # the guard let it through
    finally:
        await server.stop()
        await store.close()


async def test_a_mismatch_is_logged_with_the_fix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _server(8703, store, secret=SECRET)
    try:
        with caplog.at_level(logging.WARNING, logger="crucible.interactions.server"):
            await _post(8703, "/interact", {"user_id": "u1", "context": {}})
        assert "docs/commands.md" in caplog.text
        assert SECRET not in caplog.text
    finally:
        await server.stop()
        await store.close()


async def test_the_startup_log_masks_the_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    with caplog.at_level(logging.INFO, logger="crucible.interactions.server"):
        server = await _server(8704, store, secret=SECRET)
    try:
        assert "/<secret>/interact" in caplog.text
        assert SECRET not in caplog.text
        assert "trusted on arrival" not in caplog.text
    finally:
        await server.stop()
        await store.close()


async def test_without_a_secret_the_bare_routes_answer_and_a_warning_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    with caplog.at_level(logging.WARNING, logger="crucible.interactions.server"):
        server = await _server(8705, store, secret="")
    try:
        assert "INTEGRATIONS_SECRET" in caplog.text
        assert await _post(8705, "/interact", {"user_id": "u1", "context": {"token": "nope"}}) == 200
    finally:
        await server.stop()
        await store.close()


async def test_the_right_secret_on_a_wrong_route_is_not_a_refusal_and_is_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A GET on the right URL, a trailing slash, an unknown endpoint under the
    secret: aiohttp's 404/405, never a 403 — and the secret stays out of the log
    whatever happens."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _server(8706, store, secret=SECRET)
    try:
        with caplog.at_level(logging.WARNING, logger="crucible.interactions.server"):
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:8706/{SECRET}/interact") as r:
                    assert r.status == 405
                async with s.post(f"http://127.0.0.1:8706/{SECRET}/nothing", json={}) as r:
                    assert r.status == 404
                async with s.post("http://127.0.0.1:8706/wrong-but-long-enough/interact", json={}) as r:
                    assert r.status == 403
        assert SECRET not in caplog.text
        assert "callback to /wrong-but-long-enough/interact refused" in caplog.text
    finally:
        await server.stop()
        await store.close()


async def test_the_access_log_never_writes_the_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """aiohttp logs every request's path; on this receiver the path carries the
    secret, so the access log masks it."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _server(8707, store, secret=SECRET)
    try:
        with caplog.at_level(logging.INFO, logger="aiohttp.access"):
            await _post(8707, f"/{SECRET}/interact", {"user_id": "u1", "context": {"token": "x"}})
        access = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
        assert access and SECRET not in "\n".join(access)
        assert "/<secret>/interact" in access[0]
    finally:
        await server.stop()
        await store.close()
