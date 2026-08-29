---
name: browsing
description: Help with the browser axis — giving an agent a real Chrome it can drive, turning the axis on in a deployment that already runs, and working out why an agent says it cannot browse. Use whenever the operator asks about browsing, a web page, scraping, screenshots of a site, Chrome, Playwright, or an agent that needs to read something on the web.
---

# A real browser the agents drive

A deployment can run a real headless Chrome in a container of its own, and
agents drive it with `playwright-cli` over `bash`. It renders JavaScript and
holds a session, so it reaches pages `curl` cannot.

Reference: `$IMPI_ROOT/docs/browsing.md`. Read it before answering anything
detailed — especially anything about isolation; this skill is the operating
procedure, not the whole story.

## What you cannot do here

You have no browser yourself. `playwright-cli` is on this image's PATH, but it
is not in your allowlist and the axis may not even be on. You **advise** and
**diagnose**; the operator runs the commands, and the browsing is another
agent's to do.

## Is it even on?

It is off by default and it is its own axis — independent of the chat platform
and of the secret store. Two ways to tell:

- `grep IMPI_BROWSER ~/.impi/compose.env` — `1` means on.
- `impi doctor` prints a `browser:` line with the Chrome version when the whole
  path works, and says what is wrong when it does not.

An agent that has the skill but no browser sees no `BROWSER_CDP_URL` in its
environment. That is how it tells "no browser here" from "the browser is
broken" — and it is the answer to give when an operator asks why an agent
refused to browse.

## Turning it on

In a deployment that already runs, four steps and the first is slow — the image
is Chrome, about a gigabyte:

```bash
echo IMPI_BROWSER=1 >> ~/.impi/compose.env
impi start                 # builds the image, then brings the container up
impi skill assign web-browsing <agent>
impi reload
```

The skill itself may already be installed — `list_skills` says. If it is not,
that is yours to do: `install_skill("web-browsing", bundled=True)`. Nothing is
fetched from anywhere; it ships in the image.

`impi start`, not `impi restart` — restart is the engine alone and would not
create the browser container.

## Giving it to an agent

The agent needs **`bash`** in its `runtime.tools`, because that is how
`playwright-cli` is run. Without it the skill is inert and the agent will say so
in a way that reads like a bug. Check the profile before promising anything:

```yaml
runtime:
  tools: [read, bash]
  skills:
    - registry:web-browsing
```

## What to tell an operator about the risk

Say this plainly when the subject comes up rather than waiting to be asked — a
browser is the component most likely to be handed hostile input:

- **One browser serves every agent.** Tabs, cookies and local storage are
  shared deployment-wide. A session one agent signs into, the next inherits.
- **The profile starts empty and belongs to nobody.** It is deliberately not
  the operator's browser: none of their sessions or saved passwords are in it.
- **The browser is on a network of its own**, so a page cannot be used as a hop
  into the chat server or the secret store. That, and the empty profile, are the
  boundary — anything `playwright-cli` itself refuses is a guardrail against
  accident, since an agent with `bash` can speak CDP straight to the port.

If an operator wants an agent signed into something, the credential should come
from the secret broker, not from a chat message — see the `ward` skill. Never
suggest pasting a password for an agent to type.

## When something doesn't work

- **The agent says there is no browser** — `BROWSER_CDP_URL` is unset in the
  engine, so the overlay did not merge. Check `IMPI_BROWSER=1` and run
  `impi start`.
- **The agent has the skill but never browses** — it is probably missing `bash`,
  or the engine has not re-read the profile (`impi reload`). A conversation
  already in flight keeps its old configuration until its session resets.
- **Connection refused, or Chrome never opens its port** — `impi logs browser`,
  then `docs/browsing.md`. Never advise `--no-sandbox`: the renderer is what
  parses a stranger's HTML, and that isolation is the point.
- **The image will not build and `apt-get` cannot find Chrome** — a pinned
  version expired. Google's repository carries only the current one. Clear
  `IMPI_BROWSER_CHROME_VERSION` in `compose.env`.
- **An agent's screenshot will not send** — it has to write to an absolute path
  inside its own directory (`playwright-cli screenshot
  --filename="$AGENT_FILES_DIR/page.png"`) and the agent needs `send_file`. The
  bundled skill says so; an agent writing to `/tmp` has an old copy, and that
  one stops working entirely once the agents have containers of their own.

## What it costs

About a gigabyte of disk for the image. While no page is open the container is
just a small relay — Chrome is started on the first connection and stopped once
nothing has been attached for `IMPI_BROWSER_IDLE_TIMEOUT` (5 minutes by
default), so an idle deployment does not pay for it.
