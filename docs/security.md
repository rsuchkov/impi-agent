# Security

What the engine protects, what it does not, and what a deployment has to add.
The other pages each say this for their own corner — the broker, the browser,
a container per agent — and this one puts the corners together, so that an
operator deciding whether to let an agent near something can read one page.

Two facts shape everything below.

- **The runtime has no model of permissions of its own.** `pi` runs with the
  authority of the user it is started as; whatever its process can reach, the
  agent can reach, and a prompt-injected agent reaches it on somebody else's
  behalf. Its own security guidance says so and recommends isolating it from
  the outside. The engine is that outside: the tool allowlist, the
  confirmation gate, the broker and the containers are the permissions the
  runtime does not have.
- **The agents run unattended and read text strangers wrote.** A channel, a
  ticket, an attachment, a page in the browser — any of it may contain an
  instruction, and the model cannot reliably tell an instruction from
  information. The design therefore assumes the model *will* be talked into
  something, and arranges that what it can be talked into is bounded and
  visible: a tool it was not given does not exist; a tool that matters asks a
  human first; a credential never passes through the model at all.

## The boundaries

Four lines are drawn. Each is listed with what crosses it, what enforces it,
and what it does not hold against.

### The model and the engine

What crosses: the prompt the engine builds (who is speaking, replayed history,
an attachment's path), the tools the agent's profile allows, the results those
tools return. The model never sees a credential (the broker binds a value into
a process, see [secrets.md](secrets.md)), and never sees a result it was not
meant to act on without a marker: a tool that returns other people's text
declares `returns_untrusted`, and the engine wraps what it returns so the
boundary is visible where the text is.

Enforced by: `--tools` is the single allowlist over the runtime's built-ins,
extension tools and the engine's typed tools — a tool not named does not exist
for that agent; `--no-skills` plus an explicit list, so nothing is discovered
by walking directories. A typed tool that declares `requires_confirmation` is
confirmed **in the engine's tool server**, before it runs, never in the
runtime: the extension's token is in the agent's own environment, and a shell
in that environment could call the server directly, so a gate anywhere else
would be one that can be walked around.

Does not hold against: the model doing something harmful *with a tool it
legitimately has*. `bash` is `bash`. A prompt-injected agent with a shell and
no confirmation on the shell does what the text told it to, inside whatever
its process can reach — which is what the next three boundaries are about.

### The agent's process and the engine

What crosses: tool calls, over HTTP, from the runtime's bridge extension to
the engine's tool server; the answer to a confirmation; files in the
per-agent file root.

Enforced by: a per-agent **token** (which agent is calling) and a per-session
**proof** (which conversation), both issued by the engine at spawn and carried
as headers; the server checks the token against the agent's allowlist and
believes the session id only beside its proof, compared in constant time. The
proof is revoked when the process goes, so a copy that outlived it buys
nothing. See the tool section of [architecture.md](architecture.md).

Does not hold against: a process that can read **another process's
environment**. Both secrets live in the runtime's environment, and a shell run
as the same user in the same container reads every neighbour's
`/proc/<pid>/environ`. So within one container the secrets stop a claim made
from *knowledge* — a model naming a conversation it was never told about — and
not a neighbour with a shell. That is the next boundary.

### What an agent's process can see of the tool server

The tool server knows a caller by two secrets in the runtime process's
environment: the agent's token and the session's proof. They defeat a claim
made from knowledge — a model naming a conversation it was never told about,
an agent with only typed tools naming any — and nothing more: any process of
the same user in the same container reads `/proc/<pid>/environ` of every
other, `read` and `grep` included, not only `bash`. So in the default
arrangement an agent with a shell can take a neighbouring **conversation's**
proof (and act in its channel, as its user, with whatever its turn's scope
carries) and a neighbouring **agent's** token (and call the server with that
agent's allowlist).

What the engine does about it today:

- it says so at startup and in `impi health`, by name, whenever more than one
  agent runs in its process space and any of them has a shell-like built-in
  (`bash`, `read`, `grep`, `edit`);
- every process it starts proves itself, a memoryless run included, and a
  call that names no session is refused — a shell calling the server by hand
  with a token it found has to also name a live session and its proof;
- an agent's process is handed only the environment the engine grants it, so
  what a neighbour can read there is the tool server's two secrets and the
  session id, not the engine's own tokens.

What it does not do, and the options weighed: an identity attested by the
kernel instead of a secret (a unix socket, the peer's pid mapped to the agent
and session the engine spawned it for — the right next step for the default
arrangement, not built); a user id per conversation (heavy under rootless
podman); `hidepid` on `/proc` (hides other users' processes, not the same
user's); a secret delivered over a channel a neighbour cannot read (there is
none a same-user process cannot). The boundary that holds today is
[a container per agent](agent-containers.md), and the warning above exists
so that a deployment which needs that boundary is told, not left to find out.

### One agent and another

What crosses, by default: everything. Every agent's runtime is a child of the
engine, in the engine's container, as the engine's user. An agent with `bash`
or `read` can read another agent's profile and system prompt, its conversation
memory, its broker certificate, the engine's own configuration file with every
chat token in it, and — above — its tool token and session proofs. The tool
allowlist is the only thing between agents in this arrangement, and it is a
real one: an agent without `bash` and `read` reads nothing. But a shell is
what most skills are built on.

Enforced by, when turned on: [a container per agent](agent-containers.md).
Each agent then has its own filesystem, its own volumes, its own network with
the engine, its own broker certificate and nobody else's; the engine's
environment no longer sits in every agent's process. The runtime's own
credential for the model provider is the one thing still shared.

The honest summary, for an agent that has a shell:

| it can reach | default | a container per agent |
|---|---|---|
| its own profile, memory, files | yes | yes |
| another agent's profile and memory | yes | no |
| another agent's broker certificate | yes | no |
| the engine's `.env` (every chat token), with `read` | yes | no |
| the engine's tokens in its own environment | no (granted names only) | no |
| another process's tool token / session proof | yes | only its own processes |
| the chat server directly (not through tools) | yes | no |
| the model provider's credential | yes | yes (shared) |
| the host's filesystem outside the mounts | no | no |

If agents of different trust share a deployment — one reads public channels,
another holds a release token — the default arrangement is not a boundary
between them. Turn the containers on, or do not give the first one a shell.

### The deployment and the outside

What crosses: chat traffic (the engine holds a bot token per agent and calls
the platform; Mattermost calls back with button clicks and slash commands;
Slack delivers them down a socket the app holds), model traffic (the runtime
to the provider), the engine's own doors for your programs (the WebSocket and
HTTP gateways, token-authenticated), and whatever the runtime's shell reaches
on the network.

Enforced by: tokens on every door the engine opens, including a secret segment
on the paths Mattermost calls back to (a callback carries no signature and
says who clicked, so the path is what proves it came from the platform); a
private network per agent container; the browser on a network that cannot
reach the chat server.

Does not hold against: outbound traffic from an agent's shell. Nothing in the
engine restricts egress. A deployment that needs to is a deployment that
adds a proxy or a firewall around the containers (a `compose.d/` drop-in can
set `HTTPS_PROXY`/`NO_PROXY` on the services and `pi` honours them) — the
engine will not pretend to do it from inside.

## Where data goes, and how long it stays

What leaves, and what is kept. "The provider" is whichever model endpoint the
deployment configured; the engine makes no model calls itself.

| data | goes to | kept where | for how long |
|---|---|---|---|
| messages the agent is given (its own conversation, replayed history, the sender's name) | the provider, inside the prompt | the runtime's session file, per conversation | until deleted (`impi sessions`), or `PI_SESSION_RETENTION_DAYS` when set |
| attachments people send | the provider only if the runtime shows them (images, inline); otherwise the path | the engine's data volume | `ATTACHMENT_RETENTION_DAYS`, 14 by default |
| tool results (a channel's posts, a ticket, a file's content) | the provider, as the model reads them | the session file, as part of the conversation | as above |
| tool **arguments** | nowhere beyond the engine | the inventory, as the [tool trace](tool-trace.md) | `TOOL_TRACE_RETENTION_DAYS`, 14 by default |
| tool **results** | — | **never stored** by the engine | — |
| a scheduled task's prompt | the provider, at each run | the inventory | until the task is removed |
| who said what to whom (conversation ↔ session, last user) | nowhere | the inventory | until the session is deleted |
| the broker's ledger (which agent asked for which secret, the command, the decision) | nowhere | the broker's own database | until removed by hand; never holds a value |
| the engine's log | stdout, whatever captures it | the container runtime's log | the runtime's rotation policy |

Two things to know about the session files: they are plain JSONL on the data
volume, readable by anything running as the engine's user (and by nobody else:
the directories are 0700, like the inventory's 0600), and they are the agent's
memory — deleting one is how a conversation is forgotten. `impi sessions
purge-idle --days N` is the knife, and `PI_SESSION_RETENTION_DAYS=N` makes the
engine use it at every start.

## Where the credentials live

| credential | where | readable by |
|---|---|---|
| the chat platform tokens, one per agent | `conf/.env` on the host, bind-mounted into the engine, mode 600 | the engine; by default every agent with `read` |
| the gateway tokens for your programs (`WS_SERVICE_TOKEN__*`, `HTTP_CALLER_TOKEN__*`) | `conf/.env`, in the clear | as above |
| the model provider's login | `~/.impi/pi/agent/auth.json` on the host (mode 600, directory 700), mounted into the engine as pi's home and into each agent container as that one file | the engine and every agent container |
| a custom endpoint's key (`LLM_API_KEY`) | `conf/.env`; passed to each runtime in its environment | every agent's runtime |
| the agents' broker certificates | `~/.impi/certs`, mounted into the engine (or one each into its container) | see [secrets.md](secrets.md) |
| the operator's broker certificate | `~/.impi/operator`, mounted into nothing an agent runs in | the operator |
| the store's unseal key and the broker's credential | `~/.impi/ward-recovery.txt` on the host, mode 600, mounted into nothing | the operator |
| the tool token and session proof | the runtime's environment, minted at spawn | the process; any neighbour with a shell |

The engine's own configuration is the soft spot of the default arrangement:
one file, every token, readable by any agent with a shell. The broker exists
so that the credentials that *matter* — the ones an agent uses on your behalf
— are not in that file at all.

## Hardening checklist

What is on by default, what is a choice, and what the engine leaves to you.
Read the first table as the baseline every deployment has; the second as the
decisions an operator makes.

**Always on**

| control | what it does |
|---|---|
| the tool allowlist (`runtime.tools`) | a tool not named does not exist for the agent — built-ins included |
| the process environment is granted, not inherited | a user's agent gets a base set plus the names the deployment (`RUNTIME_ENV__*`) and its profile (`runtime.env`) grant; the engine's tokens never reach it. `support` is the exception, as part of the engine |
| `PI_OFFLINE=1` on every runtime process | no version check or other startup network call from the runtime |
| explicit skills (`--no-skills` + the profile's list) | no ambient discovery; a skill's scripts run only for agents given it |
| explicit extensions (`RUNTIME_EXTENSIONS`) | an `_extensions/<name>` loads into every agent only when the operator names it; a directory any agent with `write` can add to decides nothing |
| server-side confirmation (`requires_confirmation`) | asked in the engine, before the tool runs; fails closed with no gate |
| per-agent token + per-session proof | a call names an agent and proves a conversation; constant-time checks; a call naming no session is refused |
| a tool's confirmation is addressed to the person who asked | a stranger who talked the model into a call in a shared channel cannot approve it; `TOOL_APPROVERS` names people instead |
| the shared-process warning | at startup and in `impi health`: which agents with a shell share the engine's process space, if more than one does |
| `returns_untrusted` wrapping | other people's text arrives marked as such |
| the prompt envelope is tagged per turn and quotes are escaped | a message cannot close a context block or pose as another speaker; the model is told so in its system prompt |
| contained card text | what a caller supplies is content, never structure (newlines, fences, bidi) |
| the trace keeps arguments, never results | a granted value cannot surface in the inventory |
| what is kept on disk is the owner's alone | session directories 0700, the inventory and its log 0600, tool manifests and attachments 0600; the engine runs with `umask 077` |
| the services run in boxes: read-only root, `/tmp` in memory, no capabilities, `no-new-privileges`, `pids_limit` | the engine, every agent container, the broker; the browser adds seccomp |
| non-root images; base images pinned by digest; the runtime and the browser tool from a lockfile with integrity hashes, install scripts ignored; Python dependencies locked with hashes | the image is what the release says it is, and nothing runs at build time that nobody reviewed |
| skill installs shown file by file and pinned to a commit | nothing lands unseen; `update` shows old and new; unencrypted sources (`http://`, `git://`) are refused |
| every door compares its token in constant time | the http and WebSocket gateways, the tool server, the agent host relay, the session proof |
| the log names, it does not replay | a click's token is logged by its first characters; a command's text only at DEBUG |
| the callback receivers answer only under a secret path | a Mattermost callback carries no signature and names its own user; `INTEGRATIONS_SECRET` (engine) and `WARD_CALLBACK_SECRET` (broker) are what make that claim one only the platform can make |
| no MCP, no plugin-bundled servers | the runtime's tool surface is the manifest and nothing else |

**Your decisions**

| decision | default | when to change it |
|---|---|---|
| a shell (`bash`, `read`) in an agent's allowlist | the profile's choice | give it only to agents whose input you trust, or isolate them |
| [a container per agent](agent-containers.md) | off | more than one agent, and not all of them equally trusted |
| [the secret broker](secrets.md) | off | any credential an agent should use on your behalf |
| the broker's unattended unlock | off | a scheduled task needs a secret while nobody is awake — read the trade first |
| `WARD_APPROVERS` | — | the people on it can reach every secret some agent may; keep it short |
| `TOOL_MAX_GRANT_S` / a policy's `--max-grant` | 15 min / 1 h | how long "allow for…" may leave a tool or a secret open |
| the [browser](browsing.md) | off | one browser serves every agent; cookies are shared between them |
| egress | unrestricted | a proxy or firewall around the containers when the agents must not reach arbitrary hosts |
| who answers a tool's confirmation (`TOOL_APPROVERS`) | the person who asked the agent | name the people instead when the asker should not be the one deciding |
| retention (`ATTACHMENT_RETENTION_DAYS`, `TOOL_TRACE_RETENTION_DAYS`, `PI_SESSION_RETENTION_DAYS`) | 14 / 14 / never | data classification says so |

## Known limits

Things the design does not solve, stated so nobody relies on them by accident.

- **A neighbour with a shell reads the tool server's secrets.** Within one
  container, `/proc/<pid>/environ` is readable across processes of one user;
  neither secret survives that. The boundary is the container, not the secret;
  the engine names the agents concerned at startup. A kernel-attested caller
  identity (a unix socket, the peer's pid) is the next step and is not built —
  see the section on the tool server above.
- **One browser for every agent.** Per-agent browser contexts are not
  implemented; what one agent logs into, another can use.
- **The broker binds an argv it is told.** `secret-exec` runs the command it
  declared, so for an honest caller the card shows what runs; anything else in
  the container can claim any argv. A rule narrows what an honest caller may do
  quietly and is worth nothing against a compromised container.
- **The runtime's model credential is shared.** Every agent calls the model as
  the same account; a container per agent separates data, not that.
- **The engine does not restrict egress** and does not encrypt session files
  at rest; both are the deployment's.
- **Prompt injection is bounded, not eliminated.** The allowlist, the gate and
  the broker decide what a talked-into agent can do; nothing decides whether
  it was talked into it.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) at the repository root.
