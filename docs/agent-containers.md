# A container per agent

By default the engine runs every agent's runtime as a child process of itself.
One image, one container, one filesystem: convenient, and the reason three
things are true that an operator eventually stops being comfortable with.

- **The engine's image carries every agent's dependencies.** An agent that needs
  a JDK means the engine's image needs a JDK.
- **An agent with `bash` can read another agent's things.** Its certificate for
  the secret broker, its profile, its conversation memory. The tool allowlist is
  real — an agent without `bash` reads nothing — but a shell is what most skills
  are built on.
- **You cannot build an image for one agent.** `pi install` reaches extensions
  and skills; it does not reach `apt`.

Turning this axis on gives each agent a container of its own. The engine stops
forking runtimes and starts asking, over a private network, for one to be
started over there.

It is off by default and independent of every other axis — the chat platform,
the secret store, the browser. A deployment that never turns it on behaves
exactly as it always did.

## What it costs

Read this before turning it on; two of the three are not reversible by a flag.

- **A container per agent**, each holding a small idle process until a turn
  arrives.
- **Creating an agent from chat no longer finishes in chat.** The engine writes
  the profile and the token; bringing a container into being is a command a
  person runs on the host (`impi agent sync`). `support` will say so.
- **`compose.d/` overrides do not reach the agents.** Anything you added there
  for the `impi` service applies to the engine only. `impi doctor` says so when
  you have both.

## Turning it on

In a deployment that already runs:

```bash
echo IMPI_AGENT_CONTAINERS=1 >> ~/.impi/compose.env
impi agent migrate <agent>      # once per agent, BEFORE the first sync
impi agent sync
```

**`migrate` is not optional and not automatic.** An agent's session files are
the memory of every conversation it has had, and they live in the engine's data
volume; its container mounts a volume of its own. Skipping the copy is
indistinguishable, from a user's side, from an agent that has forgotten
everybody. The originals are left in place, so it is safe to run and safe to
check afterwards.

`sync` is the command to re-run whenever the set of agents changes, or what one
is built with changes. It:

1. asks the engine what each agent needs (it is what reads the profiles);
2. writes `~/.impi/conf/agents.compose.yaml`, a Dockerfile per agent, and a
   token per agent;
3. builds the shared agent image, then each agent's image;
4. brings everything up.

## What an agent's container holds

Its own profile, mounted **read-only at the same path the engine has it**
(`/app/agents/agents/<name>`). Paths being identical on both sides is what lets
a filename in a chat message mean one thing: the engine names a file it saved,
and the agent opens exactly that.

Then, one volume each and nobody else's:

| Path | What | Shared with |
|---|---|---|
| `/app/agents/agents/<name>` | its profile | the engine, read-only |
| `/app/skills` | the shared skill library | every agent, read-only |
| `/app/sessions` | its conversation memory | nobody |
| `/app/files/<name>` | its own files, in and out | the engine |
| `/home/impi/.pi` | the model credential | every agent — see below |

`AGENT_FILES_DIR` names the fourth one in the agent's environment. It is where
an attachment arrives and where anything the agent means to send should be
written; `/tmp` stops being a place the engine can read, and `send_file` refuses
a path it cannot reach.

**The model credential is still shared.** A subscription login lives in one
volume every agent mounts, because every agent needs it to call a model. This
axis does not change that, and it is worth being explicit: it separates agents
from each other's *data*, not from the deployment's model account.

## Giving one agent something the others do not have

Two ways, and the first covers most of it.

**Declared, in the agent's own `agent.yaml`:**

```yaml
runtime:
  tools: [read, bash]
  packages:
    apt: [ffmpeg]
    npm: [some-cli]
    pip: [pandas]
```

Package names only. Anything with a space in it is refused, because a package
list that can carry shell is a Dockerfile pretending not to be one.

**A fragment, `Dockerfile.include` in the agent's directory:**

```dockerfile
RUN curl -fsSL https://example.invalid/install.sh | sh
```

It is spliced into a generated Dockerfile, after the declared packages, running
as root. The generated file then puts the user back and sets the command — that
footer is a contract the engine relies on, which is why the whole Dockerfile is
generated rather than yours to write.

The fragment has no build context: it can `RUN` anything, but it cannot `COPY`
from the agent's directory. It does not need to — that directory is mounted at
runtime.

An agent that asks for nothing still gets an image of its own; it is one empty
layer over the shared base, and every agent is then built the same way.

## How the engine reaches it

The agent's container runs `runtime-relay` — the same shape as the browser
relay, and named for it: a small front door that starts what is behind it on
request and relays it.

One WebSocket per session, on a network only the engine and that agent share.
Two things follow from that network being private, and both are worth knowing
before something looks broken: an agent cannot reach another agent's host at
all, and it can no longer reach the chat server directly either. Chat is the
engine's to do, through the tools — a skill that called the chat API itself was
working by accident and stops working here.

The engine sends **names** — the agent, a session id, the tool allowlist, skills
as a mounted root plus a path — and the host resolves them against its own
mounts. Absolute paths are never sent: the two sides do not share a filesystem,
and a path that happened to exist on both would be the worst outcome available.

The connection carries a per-agent token. The private network is not enough on
its own: the agent's own shell is on that network too, and without a token it
could ask its host for a runtime with an allowlist nobody granted it.

On rootless podman each agent's container is given the same user mapping the
engine has, so a volume they share has one owner rather than two. Without it the
agent cannot write the directory the engine reads, and cannot read the 0600
certificate issued to it — a failure that looks like a permission bug and is
really a namespace one.

The tool server moves with this. It binds `0.0.0.0` instead of loopback and
agents are told to call `http://impi:8422`; the per-agent tool token is still
what authenticates a call and says which agent is making it. Loopback stops
being the boundary — the private network and the token are.

## What did not change

The agent's behaviour. The command line the host builds is the same one the
engine builds when it runs a runtime itself, and a test holds the two side by
side, because an agent that answered differently for having moved would make
this axis impossible to trust.

`impi reload` still works: it drops idle sessions and the next turn re-reads the
mounted profile. A **rebuild** is only needed when `runtime.packages` or
`Dockerfile.include` changed — that is `impi agent sync`.

## The engine's own agents stay in the engine

`support` ships inside the engine's image and has no profile out in the agents
directory to mount. It keeps running in the engine, which is also where it is
useful: it reads the engine's docs and answers questions about the deployment.

## The environment an agent gets

Three sources, and the first is the one that changes when you turn this on.

A runtime started as a child of the engine inherits **the engine's whole
environment**, and always has. A runtime started in the agent's own container
inherits **that container's** environment instead, plus only what the engine
explicitly grants for this spawn: the model settings, its own `AGENT_NAME`,
`TOOL_URL`, `TOOL_TOKEN` and `AGENT_FILES_DIR`, and the session id. Files a
variable points at — the tool manifest — travel as content and are rewritten to
the host's own path, because a path only means something where it was written.

That is a real improvement (the engine's `.env` no longer sits in every agent's
process) and a real migration hazard, because it cuts both ways: **anything an
agent quietly relied on from the engine's environment is gone.** A skill reading
`BROWSER_CDP_URL`, or a variable an operator added to the `impi` service in
`compose.d/`, worked before and stops working here until it is added to that
agent's own service. Nothing announces it — the agent simply behaves as if the
thing were not configured.

## When something doesn't work

- **An agent stopped answering, and the log says "not reachable"** — its
  container is down. `impi agent logs <agent>`, then `impi start`.
- **An agent answers, but says it has no memory of the conversation** — the
  migration was skipped. `impi agent migrate <agent>`, then restart the engine.
  The old files are still in the engine's data volume.
- **Every tool call fails** — the tool server is still on loopback. The
  generated overlay sets `TOOL_SERVER_HOST` and `TOOL_PUBLIC_URL`; if a drop-in
  overrides them, it wins. The engine says so at boot.
- **"protocol N, the engine speaks M"** — a half-finished update. Rebuild the
  agent images: `impi agent sync`.
- **An agent runs in the engine even though the axis is on** — no token for it
  yet, which means it was never synced. The engine logs it by name at boot.
- **"Permission denied" on a file the agent wrote, or the agent cannot read its
  own broker certificate** — on rootless podman the agent's container needs the
  same user mapping the engine gets, or the two see different owners on every
  volume they share. `impi agent sync` writes it; a generated file from before
  that was the case, or one edited by hand, will not have
  `userns_mode: keep-id:…` on the agent services. Re-run the sync.
- **A skill says something is "not configured" that plainly is** — it is reading
  a variable from the engine's environment, which the agent no longer inherits.
  See the section above; add it to that agent's service.
- **A skill will not load** — it is outside the two mounted roots (the agent's
  own directory and the shared library). An absolute path to somewhere else in
  `agent.yaml` cannot work here; install it into the library instead.
