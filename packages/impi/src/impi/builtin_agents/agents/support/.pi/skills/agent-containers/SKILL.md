---
name: agent-containers
description: The axis that gives each agent a container of its own — whether this deployment has it on, what it changes about creating and changing agents, how to give one agent a package or a toolchain the others do not have, and why an agent might be running in the engine anyway. Use whenever the operator asks about agent isolation, per-agent images, a JDK or apt package for one agent, `impi agent sync`, or an agent that has "no memory" or "cannot be reached".
---

# One container per agent

A deployment can run each agent's runtime in a container of its own instead of
as a child of the engine. Reference: `$IMPI_ROOT/docs/agent-containers.md`. Read
it before answering anything detailed; this skill is the operating procedure.

## Is it on here?

Ask before advising — the answer changes what creating an agent involves.

- `AGENT_HOSTS_ENABLED` in the engine's environment is the engine's own answer.
- `grep IMPI_AGENT_CONTAINERS ~/.impi/compose.env` is the deployment's — the
  operator runs it; `1` means on.
- `impi doctor` lists which agents have containers rendered.

## What it changes for you

**Creating an agent no longer finishes in chat.** You write the profile and the
token; a container is something a person on the host brings into being with
`impi agent sync`. Say that plainly and do not promise the agent will start
answering — it will not until they run it. `create_agent` returns the exact
command in its `hint`; pass it on verbatim.

**Editing an agent still reloads.** The profile is mounted, so `impi reload`
covers prompt, tools and skills exactly as before. Only two things need a
rebuild, because they are the agent's image rather than its configuration:
`runtime.packages` and `Dockerfile.include`. Those need `impi agent sync`.

**Engine-owned agents are unaffected.** You are one. `support` ships inside the
engine's image and keeps running there; it has no profile in the agents
directory to mount.

## Giving one agent something the others do not have

This is the reason an operator usually asks about the axis, and it is a real
capability nothing else offers — `pi install` reaches extensions and skills, not
`apt`.

Declared, in that agent's `agent.yaml`, which is what to reach for first:

```yaml
runtime:
  tools: [read, bash]
  packages:
    apt: [ffmpeg]
    npm: [some-cli]
    pip: [pandas]
```

Package names only. A name with a space is refused on purpose — a package list
that can carry shell is a Dockerfile pretending not to be one.

When that is not enough, a `Dockerfile.include` beside `agent.yaml`:

```dockerfile
RUN curl -fsSL https://example.invalid/install.sh | sh
```

It runs as root, spliced into a generated Dockerfile which then puts the user
back. Tell the operator two things about it: it cannot `COPY` from the agent's
directory (there is no build context, and the directory is mounted at runtime
anyway), and it is theirs to review — you are writing a build step that runs as
root on their machine, so show it and let them confirm.

Either way, finish with `impi agent sync`.

## What to say about the isolation

Say what it does and does not buy, rather than "it is isolated":

- **Each agent holds its own certificate for the secret broker, and only its
  own.** Without the axis, every agent's identity is in one directory that any
  agent with `bash` can read. This is the change that matters most.
- **Session files and files are one volume per agent.** No agent can read
  another's conversation memory.
- **Each agent is on a network with only the engine on it** — and the broker and
  the browser where those exist. So an agent cannot reach another agent's host,
  and cannot reach the chat server directly either: a skill that called the chat
  API itself was working by accident and stops here. Chat goes through the tools.
- **The model credential is still shared.** Every agent mounts the same
  subscription login, because every agent needs it to call a model. Do not
  describe agents as fully separated without saying this.

## When something doesn't work

- **"not reachable" in the engine's log** — that agent's container is down.
  `impi agent logs <agent>`, then `impi start`.
- **An agent answers but has forgotten every conversation** — the migration was
  skipped. `impi agent migrate <agent>` copies its session files into its own
  volume; the originals are still in the engine's data volume, so it is
  recoverable. This is the mistake to warn about BEFORE anyone turns the axis on.
- **An agent runs in the engine even though the axis is on** — it has no token,
  which means it was never synced. The engine names it at boot. `impi agent sync`.
- **Every tool call from one agent fails** — the tool server is still bound to
  loopback. The generated overlay sets it; a drop-in in `compose.d/` that names
  the engine service can override it back.
- **"protocol N, the engine speaks M"** — a half-finished update. `impi agent sync`
  rebuilds the agent images.
- **A skill will not load** — it is outside the two mounted roots (the agent's
  own directory and the shared library). An absolute path to somewhere else
  cannot work here; install it into the library instead.
- **A file the agent wrote will not send** — it wrote to `/tmp`, which is its
  own container's and not the engine's. `$AGENT_FILES_DIR` is the shared one.
- **A skill behaves as if something were not configured** — it is reading a
  variable from the engine's environment. A runtime in its own container does
  NOT inherit that; it gets its container's environment plus what the engine
  grants per spawn (model settings, `AGENT_NAME`, `TOOL_URL`, `TOOL_TOKEN`,
  `AGENT_FILES_DIR`, session id). Anything else — `BROWSER_CDP_URL`, a variable
  an operator added to the `impi` service — has to be added to that agent's own
  service. This is the failure mode that announces nothing, so suspect it early.

## What you cannot do

Turn the axis on, run `impi agent sync`, or build anything. Those are the
operator's, on the host, and deliberately: reaching a container runtime from in
here would undo the separation the containers exist to create. You advise, you
write profiles, and you say which command to run.
