# Web browsing

An optional axis: a real Chrome, headless, in a container of its own, that
agents drive to read pages the way a person would. Off by default; the installer
asks, and a running deployment turns it on with one line in `compose.env`.

It exists because a great deal of the web is not readable with `curl`: pages
that render in JavaScript, sites that want a session, anything behind a form.
The agent gets a browser rather than an HTTP client.

## What this protects, and what it doesn't

Worth reading before you turn it on.

**A browser is the component most likely to be handed hostile input.** Every
page it loads is code somebody else wrote, and the agent chooses which pages.
Two things make that survivable, and neither of them is inside the tool the
agent runs:

- **The profile starts empty and belongs to nobody.** This is deliberately not
  the operator's browser. No session of theirs, no cookie, no saved password is
  reachable from it. Whatever an agent signs into is all it ever holds.
- **The browser sits on a network of its own.** Everything else in the
  deployment — the chat server, the secret store — is on the project's default
  network, and the browser is not on it. A page cannot be used as a hop into
  `mattermost:8065` or `ward:8425`, because those names do not resolve from
  there. The engine joins both networks, so it is the only thing that reaches
  the browser at all.

**What it does not protect** is the browser from the agents. Chrome's debugging
port is total control of the browser: an agent with `bash` can speak CDP to it
directly, so any check inside `playwright-cli` — a refused scheme, a refused
address — is a guardrail against accident, not a boundary. The two above are the
boundary. Read them as the whole of it.

**One browser serves every agent.** Tabs, cookies and local storage are shared
deployment-wide: a session one agent signs into, the next inherits. Per-agent
isolation would take a browser context per agent and is not implemented; until
it is, treat the browser as a shared room.

Chrome's own sandbox is engaged — the image never passes `--no-sandbox`, which
would drop the renderer isolation that matters most here, since the renderer is
what parses the untrusted HTML. That needs four `CLONE_NEW*` flags Docker's
default seccomp profile denies, so `deploy/seccomp/chrome.json` relaxes exactly
those and nothing else. If Chrome ever fails to start because a nested user
namespace was refused, the answer is to report it, never to add `--no-sandbox`.

## Turning it on

In a deployment that already runs:

```bash
echo IMPI_BROWSER=1 >> ~/.impi/compose.env
impi start                       # `restart` is the engine only
impi skill install --bundled web-browsing --yes
impi skill assign web-browsing <agent>
impi reload
```

The first `impi start` builds the browser image, which takes a few minutes:
Chrome is about 1.5 GB. While no page is open the container costs a few
megabytes — the relay alone — and Chrome starts on the first connection.

## What an agent runs

`playwright-cli`, which ships in the engine's image. It attaches to the running
browser rather than launching one, so no browser binaries are installed beside
the engine:

```bash
playwright-cli attach --cdp="$BROWSER_CDP_URL"
playwright-cli goto https://example.com
playwright-cli snapshot
playwright-cli click e15
playwright-cli detach
```

`BROWSER_CDP_URL` is in the engine's environment when this axis is on, and
absent when it is not — which is how an agent tells "no browser here" from "the
browser is broken".

`snapshot` returns the page as an accessibility tree with a ref on every
element, and `click`/`fill`/`hover` take those refs. That is the part worth
knowing: the model acts on something the page demonstrably has, instead of
guessing a CSS selector.

`detach` leaves the browser running for the other agents. `close` would shut it
down for all of them.

The bundled **web-browsing** skill is what teaches an agent this, including the
things `--help` does not say: that the browser is shared, what it cannot do, and
what not to type into it.

## Limits worth stating

- **No downloads.** The container's filesystem is read-only and its `/tmp` is a
  tmpfs the engine cannot see, so a downloaded file exists only inside the
  browser. Getting one out would take a volume mounted into both containers.
- **No `file://`**, no local paths: the browser sees the web, not the host.
- **The user agent does not say "headless".** The image rebuilds the normal
  product token, because headless Chrome is otherwise refused outright by a lot
  of sites. It is a deception, and a small one — but the skill tells agents to
  answer honestly when a site asks who they are.
- **Cookies survive a restart.** The profile is a volume. `impi` never clears
  it; removing the `browser-profile` volume is how you reset it.

## Configuration

All of it lives in `compose.env` — the engine itself has no settings for any of
this, the same way it has none for the secret store.

| Variable | Default | What it does |
|---|---|---|
| `IMPI_BROWSER` | `0` | Whether the browser containers are part of the stack |
| `IMPI_BROWSER_CHROME_VERSION` | the pin in the Dockerfile | Empty takes current stable — the way out when the pin expires |
| `IMPI_BROWSER_IDLE_TIMEOUT` | `5m` | How long Chrome stays up after the last client leaves |
| `IMPI_BROWSER_WINDOW_SIZE` | `1440,900` | Viewport |
| `IMPI_BROWSER_MEM_LIMIT` | `2g` | Container memory, including the 1 GB `/dev/shm` |

## When something doesn't work

- **`BROWSER_CDP_URL` is unset in the engine** — the overlay did not merge.
  Check `IMPI_BROWSER=1` in `compose.env`; the file list is derived on every
  call, so fixing the value and running `impi start` is enough.
- **The name `browser` does not resolve** — the engine is not on the browser's
  network. The overlay lists `networks: [default, browser]` on the engine for
  exactly this reason: naming any network takes a service off the implicit
  default.
- **Connection refused** — `impi logs browser`.
- **Chrome never opens its port** — either the seccomp profile did not apply
  (its path in the overlay is absolute through `IMPI_HOME`; a relative one
  resolves against the wrong directory), or a nested user namespace was denied,
  which is the rootless-podman case.
- **The image will not build, `apt-get` cannot find Chrome** — the pin expired.
  Set `IMPI_BROWSER_CHROME_VERSION=` (empty) in `compose.env`, or bump the
  `ARG CHROME_VERSION` in `deploy/Dockerfile.browser`.
- **The agent has the skill but never browses** — `impi skill list` shows who
  has it, and a running engine needs `impi reload`. A conversation already in
  flight keeps its old configuration until its session resets.

## Local development

`make run` runs the engine on the host, where `playwright-cli` is not installed
and there is no browser container on a compose network. To work on this
outside the stack, install the CLI (`npm i -g @playwright/cli`), start the
browser on its own, and point `BROWSER_CDP_URL` at it. The overlay publishes no
host port on purpose, so that is a deliberate step rather than something that
happens to be reachable.
