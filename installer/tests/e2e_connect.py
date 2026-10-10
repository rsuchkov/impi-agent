"""The person in the connected-accounts e2e leg: presses the buttons a person
would, and opens the link they would be handed.

Runs inside the engine's container, where Mattermost, the callback listener
and the fake provider are all reachable:

    python e2e_connect.py <mode> <mm url> <admin token> <team> <channel> <agent user id> <provider>

Modes:
- ``refused``: ``/connections`` in a channel answers privately and posts nothing;
- ``panel``: ``/connections`` in the direct message, Connect, the link, the
  panel redrawn as connected — then Disconnect;
- ``offer``: the Connect offer the engine posted when a tool found the person
  not connected, Connect, the link, the offer redrawn as connected.

Prints ``OK <step>`` or ``FAIL <step>: why`` per step; exits 1 on a failure.
"""

import asyncio
import json
import re
import sys
import time

import aiohttp

MODE, MM, TOKEN, TEAM, CHANNEL, AGENT, PROVIDER = sys.argv[1:8]
API = MM.rstrip("/") + "/api/v4"
failed = False


def report(step: str, ok: bool, why: str = "") -> None:
    global failed
    print(f"OK {step}" if ok else f"FAIL {step}: {why}", flush=True)
    failed = failed or not ok


async def agent_posts(mm: aiohttp.ClientSession, since: int) -> list[dict]:
    async with mm.get(f"{API}/channels/{CHANNEL}/posts", params={"since": since}) as resp:
        posts = (await resp.json())["posts"].values()
    return sorted((p for p in posts if p["user_id"] == AGENT), key=lambda p: p["create_at"])


def action(post: dict, label: str) -> dict | None:
    for attachment in post.get("props", {}).get("attachments") or []:
        for found in attachment.get("actions") or []:
            if found.get("name") == label:
                return found
    return None


async def wait_for(probe, seconds: float = 30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await probe()
        if found:
            return found
        await asyncio.sleep(1)
    return None


async def post_text(mm: aiohttp.ClientSession, post_id: str) -> str:
    async with mm.get(f"{API}/posts/{post_id}") as resp:
        return json.dumps(await resp.json(), ensure_ascii=False)


async def command(mm: aiohttp.ClientSession, text: str) -> dict:
    # The team too: a direct message belongs to none, and without it Mattermost
    # finds no team command to run there — a person's client always sends it.
    body = {"channel_id": CHANNEL, "team_id": TEAM, "command": text}
    async with mm.post(f"{API}/commands/execute", json=body) as resp:
        return await resp.json()


async def press(mm: aiohttp.ClientSession, post: dict, label: str) -> bool:
    found = action(post, label)
    if found is None:
        return False
    async with mm.post(f"{API}/posts/{post['id']}/actions/{found['id']}", json={}) as resp:
        return resp.status == 200


async def connect(mm: aiohttp.ClientSession, post: dict) -> None:
    """Connect on ``post``: the link arrives privately, the browser follows it,
    and the same post says it worked."""
    async with mm.ws_connect(API.replace("http", "ws", 1) + "/websocket") as ws:
        await ws.send_json({"seq": 1, "action": "authentication_challenge", "data": {"token": TOKEN}})
        report("Connect pressed", await press(mm, post, "Connect"))

        async def link() -> str | None:
            try:
                message = await ws.receive_json(timeout=2)
            except (asyncio.TimeoutError, TypeError, ValueError):
                return None
            if message.get("event") != "ephemeral_message":
                return None
            text = json.loads(message["data"]["post"]).get("message", "")
            match = re.search(r"https?://\S+", text)
            return match.group(0) if match else None

        url = await wait_for(link, 20)
    report("the link came privately", url is not None, "no ephemeral message with a link")
    if url is None:
        return
    report("the link is not in the post", url not in await post_text(mm, post["id"]))
    async with aiohttp.ClientSession() as browser, browser.get(url) as resp:
        page = await resp.text()
    report("the callback page says connected", resp.status == 200 and "is connected" in page,
           f"{resp.status} {page[:200]!r}")

    async def redrawn() -> bool:
        return f"Connected to {PROVIDER} as" in await post_text(mm, post["id"])

    report("the same message says it worked", bool(await wait_for(redrawn, 20)), "the post was not redrawn")


async def main() -> None:
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as mm:
        since = int(time.time() * 1000) - 1000
        if MODE == "refused":
            answer = await command(mm, "/connections")
            report("refused outside a direct message", "only in a direct message" in json.dumps(answer), str(answer))
            await asyncio.sleep(3)
            report("nothing posted in the channel", not await agent_posts(mm, since))
            return

        if MODE == "panel":
            await command(mm, "/connections")

        # The panel is the one just asked for; the offer, the one a tool posted
        # — told apart by its heading, since a panel offers Connect too.
        async def with_connect() -> dict | None:
            posts = [
                p for p in await agent_posts(mm, since if MODE == "panel" else 0)
                if action(p, "Connect") and (MODE == "panel" or f"Connect {PROVIDER}" in json.dumps(p))
            ]
            return posts[-1] if posts else None

        post = await wait_for(with_connect, 40)
        report(f"the {MODE} offers Connect", post is not None, "no post with a Connect button")
        if post is None:
            return
        await connect(mm, post)

        if MODE == "panel":
            async with mm.get(f"{API}/posts/{post['id']}") as resp:
                current = await resp.json()
            report("Disconnect pressed", await press(mm, current, "Disconnect"))

            async def ended() -> bool:
                return f"Disconnected from {PROVIDER}." in await post_text(mm, post["id"])

            report("the panel says disconnected", bool(await wait_for(ended, 20)), "the post was not redrawn")


asyncio.run(main())
sys.exit(1 if failed else 0)
