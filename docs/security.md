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

Five lines are drawn. Each is listed with what crosses it, what enforces it,
and what it does not hold against.

### The model and the engine

What crosses: the prompt the engine builds (who is speaking, replayed history,
an attachment's path, the report a turn the engine started is about — quoted
as data from its source, never as a person's words), the tools the agent's
profile allows, the results those tools return. The model never sees a credential (the broker binds a value into
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

Does not hold against: an **unconfined** process that can read another
process's environment. Both secrets live in the runtime's environment, and a
shell run as the same user in the same container reads every neighbour's
`/proc/<pid>/environ` — unless the kernel refuses it, which is what
confinement is for. That is the next boundary.

### What an agent's process can see of the tool server

The tool server knows a caller by two secrets in the runtime process's
environment: the agent's token and the session's proof. On their own they
defeat a claim made from knowledge — a model naming a conversation it was
never told about, an agent with only typed tools naming any — and nothing
more: any unconfined process of the same user in the same container reads
`/proc/<pid>/environ` of every other, `read` and `grep` included, not only
`bash`. Such a process could take a neighbouring **conversation's** proof (and
act in its channel, as its user, with whatever its turn's scope carries) and
a neighbouring **agent's** token (and call the server with that agent's
allowlist).

**Confinement closes that.** On a kernel with Landlock (Linux 5.13+), every
runtime process starts under rules the engine computes (`RUNTIME_CONFINEMENT`,
on by default) and each in a Landlock domain of its own: the kernel refuses it
another process's `environ` and `mem` outside that domain — another
conversation's process, another agent's, the engine's — and, from Linux 6.12
(Landlock ABI 6), its signals. Its own environment is still its own; what it
can read there is its own token and proof. Below Linux 6.2 (ABI 3) the kernel
cannot stop a confined process truncating a file its user may write, such as
`conf/.env`: reading it stays refused, emptying it does not. From Linux 6.7
(ABI 4) the network is confined as well: the process connects over TCP to the
ports named (`RUNTIME_NETWORK_PORTS`, those of the services its environment
points it at — the tool server, the browser, the broker — and an agent's own)
and to no other — not the deployment's database, not a hub, not the receiver —
and binds none.

What remains, and what the engine does about it:

- an agent at level `off`, and every agent on a kernel without Landlock, is
  unconfined; the engine names, at startup and in `impi doctor`, each user's
  agent that runs unconfined in its process space with a shell-like built-in
  (`bash`, `read`, `grep`, `edit`) — one is enough, since the engine itself and
  `support` (unconfined by default, as part of the engine) are always its
  neighbours;
- every process it starts proves itself, a memoryless run included, and a
  call that names no session is refused — a shell calling the server by hand
  with a token it found has to also name a live session and its proof;
- an agent's process is handed only the environment the engine grants it, so
  what an unconfined neighbour can read there is the tool server's two secrets
  and the session id, not the engine's own tokens;
- a conversation the application entitles to fewer tools than its agent has
  (`crucible.ports.entitlement`) is held to that set by the server, once the
  session id is proven, not only by the shorter manifest its process was
  started with: a call for a tool outside the set answers `not available in
  this conversation`, whatever the process claims — a model that read a tool's
  name in a ticket gets a refusal, not the tool.

The weighed alternatives — a kernel-attested caller identity over a unix
socket, a user id per conversation, `hidepid` on `/proc` — are not needed
where confinement holds. Where it cannot (no Landlock), the boundary is
[a container per agent](agent-containers.md).

### One agent and another

Every agent's runtime is, by default, a child of the engine, in the engine's
container, as the engine's user. Unconfined, an agent with `bash` or `read`
can read another agent's profile and system prompt, its conversation memory,
its broker certificate, the engine's own configuration file with every chat
token in it, and — above — its tool token and session proofs. The tool
allowlist is then the only thing between agents, and it is a real one: an
agent without `bash` and `read` reads nothing. But a shell is what most skills
are built on.

Enforced by, by default: **confinement**. The process reads the system, its
program, its own profile, the skill library and the files it is handed; it
writes only its own memory, its own files, the directories it shares
(`runtime.shared_dirs`) and its own scratch `TMPDIR` and
`HOME`; it reads the model credential and cannot write it, nor create anything
beside it (a settings file or an extension that would load into every agent).
The credential is read-only because `pi` runs a key that begins with `!` on
every model request: a process that could write the file could have a command
run in every other process reading it. The engine refreshes the login itself. At level `conversation` (the default) "its own" is one
conversation's: a process of one conversation cannot read another's memory or
attachments, even of the same agent — for a deployment where one agent talks
to many people. At `agent` an agent's conversations share its files. A skill
that needs more declares it (`runtime.confinement_read` / `confinement_write`),
or its agent is set to `off`. See [configuration.md](configuration.md).

Enforced by, when turned on: [a container per agent](agent-containers.md).
Each agent then has its own filesystem, its own volumes, its own network with
the engine, its own broker certificate and nobody else's; the engine's
environment no longer sits in every agent's process. Each process inside is
confined as well. The runtime's own credential for the model provider is the
one thing still shared.

The honest summary, for an agent that has a shell:

| it can reach | unconfined (`off`, or no Landlock) | confined (default) | a container per agent |
|---|---|---|---|
| its own profile, memory, files | yes | yes — at `conversation`, its conversation's memory and files | yes, confined as well |
| another conversation's memory, same agent | yes | no | no |
| another conversation's attachments, same agent | yes | at `agent` yes; at `conversation` no | the same |
| a directory the agent shares (`runtime.shared_dirs`) | yes, and every other shared directory under the root | its own, at every level | its own; at `off` the whole volume |
| another agent's profile and memory | yes | no | no |
| another agent's broker certificate | yes | no | no |
| the engine's `.env` (every chat token), with `read` | yes | no | no |
| the engine's own environment, via `/proc/<pid>/environ` | yes — little in it under compose, where secrets stay in `conf/.env`; everything if the deployment puts them in the environment | no | no |
| another process's tool token / session proof | yes | no | no |
| the chat server directly (not through tools) | yes | yes | a co-deployed Mattermost: no; an external or hosted one is reachable by URL like any internet host |
| the model provider's credential | yes, and rewrite it | read | read (shared) |
| the host's filesystem outside the mounts | no | no | no |

If agents of different trust share a deployment — one reads public channels,
another holds a release token — confinement keeps their files and processes
apart, and their network by port only: both reach every address on the ports
named. Turn the containers on for a network of their own, or do not give the
first one a shell.

**When one agent hands another a task.** Two things cross between them.
The task handed through `delegate` goes through the engine, not through the
chat, and only after a person has read the card — the agent it goes to, the
model that agent answers with, the task whole, the files by their paths and,
a text file, as written (a file that is not text by name and size) — and
read every time: the card offers no "allow for a while" and honours
none. The asked agent's turn runs in a conversation that exists for this
task alone (`delegated:<task id>`, a fresh session): the task and its files
are all it is given — no thread of the asker's is replayed, no earlier task
remembered — so what reaches its model is what the card showed, and nothing
else. One exception the asked agent's own profile makes:
`delegation_session: per_conversation` keeps every task from one asking
conversation in one conversation of its own (`per_conversation_key`), so the
earlier tasks of that conversation are remembered there — and outlive the
asker's memory until the application forgets them with it
(`EngineDelegationService.forget`). The files are copies taken when the person allowed, each held to the
digest of what the card showed (a file changed in between is refused) and
placed in that conversation, out of the asker's reach: the asker's own turn,
which goes on once the task is handed over, cannot rewrite what the asked
agent reads. Its answer comes back as a turn of the asker's in which the
answer is quoted as data under the agent's name, not as anybody's
instructions. The turn runs as the person who read the card (`user_id`) with
the asker's turn attributes, so the application's admission and entitlement
rules decide for that person and telemetry counts it as theirs — but with
none of what a person's own turn has: no widgets, forms, ephemeral posts,
channel administration, confirmed tools, connected accounts, files out,
scheduling or handing on again (`DELEGATED_TURN_DENIED`;
`runtime.tools_by_gateway: {delegation: [...]}` narrows further). The asked
agent runs under its own profile — its own tools, its own confinement, its
own ports, its own model key where it has one (`AGENTS_LLM_API_KEY__<AGENT>`) — and
reaches nothing of the asker's but the task (and, when it keeps one session per asking
conversation, its own earlier tasks from it); a headless agent reaches nothing
of anyone's but the tasks it is handed. A directory the two share
(`runtime.shared_dirs`) has no card: every agent naming it reads all of it,
whatever its model and wherever that model runs, so what may not reach one
of them must not be put there.

**When the asker trusts the agent** (`runtime.trusted_agents`, `ask_agent`).
The boundary is a different one: the same person, the same deployment, an
agent that knows more of one area. Nothing crosses that was not already the
person's to give — and only what the trust names: the turn secrets listed
(the same values the asker's turn holds, in memory, never stored, gone with
the asker's turn), the person's connected accounts when `connections` is on,
the conversation so far when `context` says so. No card on the hand-off; the
trusted agent's confirmed tools still ask, and the card goes to the
asker's conversation — every gateway it is live in, as the asker's own cards
do, whispered to the person where other people read as the asker's own would
be — answered by the same person who could answer the asker's. The trusted agent runs under its own profile — its own tools,
confinement, ports, key — through a door with the person's confirmation and
accounts and nothing else of a conversation's. Trust is the operator's word in
the asker's profile: an agent it names reads everything the trust hands over,
on whatever model it answers with, so name a trusted agent's secrets and
accounts as narrowly as its work allows, and trust no agent whose model the
person's data may not reach.

Does not hold against: **a mention**. With `AGENTS_REPLY_TO_AGENTS` on (the
default), an agent's gateway still opens a turn on any message by one of the
deployment's agents that mentions it — a plain reply the model wrote, a
`send_file` caption — with no card and with the thread for context: that is
not `delegate`, and a talked-into agent can address another that way. The
cascade is bounded (`AGENT_MAX_HOPS`), what the mentioned agent may do is its
own profile's, and its confirmed tools still ask a person; but what left the
first agent was not read. Turn `AGENTS_REPLY_TO_AGENTS` off where that is not
acceptable: `delegate` does not need it. Nor against a task text that
carries what it should not and a reviewer who did not read it; files in the
shared directory nobody looked at; an agent at `off`, or one in a container
without Landlock, which sees every shared directory, not only its own; and,
on one host, the runtime's shared login (above) — an agent whose model should
stay apart answers with a key of its own (`AGENTS_LLM_API_KEY__<AGENT>`) or runs in
a container of its own.

### An MCP server and the engine

What crosses: the arguments of a call the model made, the headers the server's
entry names (a deployment credential from the `MCP_` family — the only family
an entry may name — or the token of the person whose turn it is), the content
of a file the person attached in the conversation when the server's pinned
schema declares an argument as content (`contentEncoding: base64`) and a
person confirmed the card, and back, the server's answer. Nothing else: the
engine asks a server for `tools/list` and `tools/call` only, and answers a
request the server sends back with "method not found".

Enforced by: the definitions the model reads are the ones pinned when the
server was declared, not the server's current word — an untrusted server whose
tools changed has them withdrawn until a person reviews the change (`trust:
trusted` opts out, for a server your own team runs); every result comes
wrapped as a stranger's text; a tool waits for a person's card unless the
operator put it in `safe:` or a trusted server marked it read-only or not
destructive — an untrusted server's hints decide nothing, a tool it says
nothing about is destructive by the protocol's default; the token of a person is
fetched for the call and sent on a connection of that call alone; a file goes
only after a card, only one the person attached in this conversation (the
engine's own record of what arrived, with its digest, decides — not a path the
model wrote, and not a file the agent's process made or changed; the record
lives under `<attachments>/.received/`, outside every directory a confinement
level grants an agent's process — with confinement `off` the process can
rewrite it, as it can anything), and its bytes reach no log, trace or card.
The runtime never meets a server: `--no-mcp` stays on every process.

Does not hold against: what the server does with a call it was allowed to
receive — it is a service the deployment chose, as a tool's own code is. A
server on stdio is a child of the engine, not of an agent: it runs with the
engine's rights (not confined) and sees `PATH` and the variables its entry
names, nothing else of the engine's environment.

### A person's chat session and the agent

What crosses: a person's word, under the identity the chat platform vouches
for, into an agent that may act in other systems with that person's own
account (connected accounts). A chat session lives for months; the systems
behind the agent may sign their people in every day, with a second factor.
Whoever holds the chat holds the agent's hands for as long as the chat does.

Enforced by: nothing in the engine by itself — the platform's session is the
platform's. An application that wants the agent held to the systems' bar
puts a `TurnAdmission` in front of its turns (`ports.admission`,
[building-an-app.md](building-an-app.md#composition-notes-from-the-field)): no turn runs
for a person whose sign-in at the identity provider — a connection, with the
moment it was made — is older than the rule allows, and the person is sent to
sign in by a link that reaches them alone, wherever they wrote. The rule may
hold one gateway and not another (`AdmissionByGateway`): a chat beside a
system that signs its people in itself.

Does not hold against: a person who holds both the chat and the identity
provider's session, and — on its own — a tool the agent calls with the
deployment's credential rather than the person's. It bounds what a stolen
chat session is worth, to the sign-in's lifetime; it does not make the chat
platform's session short.

### The deployment and the outside

What crosses: chat traffic (the engine holds a bot token per agent and calls
the platform; Mattermost calls back with button clicks and slash commands;
Slack delivers them down a socket the app holds), model traffic (the runtime
to the provider), the engine's own doors for your programs (the WebSocket and
HTTP gateways, token-authenticated), and whatever the runtime's shell reaches
on the network. With connected accounts configured, two more: people's
browsers reach the callback listener (`CONNECTIONS_PORT`), and the engine
calls each provider with people's own tokens.

Enforced by: tokens on every door the engine opens — the replicas' own routes
on the hub's port included, where one shared `REPLICA_TOKEN` is what tells a
sister process from anyone else the balancer lets through, and a turn's secrets
are left behind when a turn is forwarded — including a secret segment
on the paths Mattermost calls back to (a callback carries no signature and
says who clicked, so the path is what proves it came from the platform); a
private network per agent container; the browser on a network that cannot
reach the chat server. The callback listener has no token — what proves a
request there is the single-use state it carries, issued to one person for ten
minutes, and the page sets no cookie and sends no referrer.

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
| messages the agent is given (its own conversation, replayed history, the sender's name) | the provider, inside the prompt | the runtime's session file, per conversation | until deleted (`impi sessions`), or `SESSION_RETENTION_DAYS` when set |
| attachments people send | the provider only if the runtime shows them (images, inline); otherwise the path | the engine's data volume | `ATTACHMENT_RETENTION_DAYS` idle days of the conversation, 14 by default; at most `ATTACHMENT_CONVERSATION_MAX_MB` per conversation |
| tool results (a channel's posts, a ticket, a file's content) | the provider, as the model reads them | the session file, as part of the conversation | as above |
| tool **arguments** | nowhere beyond the engine | the inventory, as the [tool trace](tool-trace.md) | `TOOL_TRACE_RETENTION_DAYS`, 14 by default |
| tool **results** | — | **never stored** by the engine | — |
| telemetry (a turn ended, a tool ran, what it cost — names, ids, numbers, outcome words) | the log, and with `TELEMETRY_OTLP_ENDPOINT` the tracing collector named | the collector's | the collector's retention |
| telemetry **content** — what the person wrote, what the agent answered, tool arguments — only with `TELEMETRY_CONTENT=text`, scrubbed of the turn's secrets and of what looks like one (a heuristic, as the log scrub is: a secret in prose or under an innocent name passes), cut at 8000 characters; tool results and the model's thinking never as fields, though an answer may quote one | the same log and collector | the collector's | the collector's retention |
| a scheduled task's prompt | the provider, at each run | the inventory | until the task is removed |
| who said what to whom (conversation ↔ session, last user) | nowhere | the inventory | until the session is deleted |
| the broker's ledger (which agent asked for which secret, the command, the decision) | nowhere | the broker's own database | until removed by hand; never holds a value |
| people's connected accounts: tokens (sealed), and the account's id, username, name, email and the userinfo fields `CONNECTION_ACCOUNT_EXTRA__<NAME>` names (a groups claim), readable | the provider, on each call a tool makes as them | the inventory | until they disconnect, reconnect, or a 401 ends it |
| the engine's log | stdout, whatever captures it | the container runtime's log | the runtime's rotation policy |

Two things to know about the session files: they are plain JSONL on the data
volume, a directory per conversation, readable by anything unconfined running
as the engine's user (and by nobody else: the directories are 0700, like the
inventory's 0600; a confined process reaches its own conversation's only), and they are the agent's
memory — deleting one is how a conversation is forgotten. `impi sessions
purge-idle --days N` is the knife, and `SESSION_RETENTION_DAYS=N` makes the
engine use it at every start.

## Where the credentials live

| credential | where | readable by |
|---|---|---|
| the chat platform tokens, one per agent | `conf/.env` on the host, bind-mounted into the engine, mode 600 | the engine; an unconfined agent with `read` |
| the gateway tokens for your programs (`WS_SERVICE_TOKEN__*`, `HTTP_CALLER_TOKEN__*`) | `conf/.env`, in the clear | as above |
| the model provider's login | `~/.impi/pi/agent/auth.json` on the host (mode 600, directory 700), mounted into the engine as pi's home and into each agent container as that one file, read-only | read: every runtime process; written: the engine (which refreshes the login) and unconfined processes |
| a custom endpoint's key (`LLM_API_KEY`) | `conf/.env`; passed to each runtime in its environment | every agent's runtime — or, as `AGENTS_LLM_API_KEY__<AGENT>`, that agent's alone |
| the agents' broker certificates | `~/.impi/certs`, mounted into the engine (or one each into its container) | see [secrets.md](secrets.md) |
| the operator's broker certificate | `~/.impi/operator`, mounted into nothing an agent runs in | the operator |
| the store's unseal key and the broker's credential | `~/.impi/ward-recovery.txt` on the host, mode 600, mounted into nothing | the operator |
| the tool token and session proof | the runtime's environment, minted at spawn | the process; an unconfined neighbour with a shell |
| people's connected accounts (access and refresh tokens, and a credential the person entered on the engine's form — once, in the form's body over TLS, never through the chat) | the inventory, sealed with `CONNECTIONS_KEYS` (Fernet) | the engine; the database alone reads whose and which account and the userinfo fields kept beside it, not the tokens. The key is in `conf/.env`, so an unconfined agent with `read` has both — the tokens never enter an agent's process otherwise ([connections.md](connections.md)) |
| the OAuth applications' secrets (`CONNECTION_CLIENT_SECRET__*`) | `conf/.env`, in the clear | the engine; an unconfined agent with `read` |

The engine's own configuration is the soft spot of an unconfined agent: one
file, every token, readable by any such agent with a shell. The broker exists
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
| `PI_OFFLINE=1` on every runtime process | no version check, model-catalog refresh, tool download or package install from the runtime; requests to the model's provider — prompt-cache warming among them — are not affected |
| explicit skills (`--no-skills` + the profile's list) | no ambient discovery; a skill's scripts run only for agents given it |
| explicit extensions (`RUNTIME_EXTENSIONS`) | an `_extensions/<name>` loads into every agent only when the operator names it; a directory any agent with `write` can add to decides nothing |
| server-side confirmation (`requires_confirmation`) | asked in the engine, before the tool runs; fails closed with no gate |
| per-agent token + per-session proof | a call names an agent and proves a conversation; constant-time checks; a call naming no session is refused |
| which agents a conversation may ask (`Entitlement.agents`) | the application names, per person, the agents a conversation may hand a task to; an agent outside the list does not exist for that turn — `list_agents` leaves it out and asking it is refused as asking an agent that does not run here — so a team's agent answers its team alone, and its words and model never reach anyone else. `None` leaves the agent's profile (`trusted_agents`, the roster) to decide |
| a tool's confirmation is addressed to the person who asked | a stranger who talked the model into a call in a shared channel cannot approve it; `TOOL_APPROVERS` names people instead. Where other people read, the card is shown to the asker alone (an ephemeral post, on Slack and Mattermost, on the gateway they spoke through): the call's arguments — what the model wants to do with the person's rights — are not put in front of the room; a copy on another gateway, and a Mattermost bot without `create_post_ephemeral`, still get it in the open, the latter with a warning in the log |
| a shared conversation per person (`CHANNEL_CONVERSATION_PER_AUTHOR`, `AuthorKey`) | in a channel or a thread each person who speaks to the agent has a session of their own: a batch is never several people's, a turn never runs with one person's credentials for another's words, and what the model learned with one person's rights is not retold to another. Off by default; turn it on wherever an agent acts with people's own credentials — connected accounts, turn secrets — in rooms others read |
| the model credential is read-only to every agent process | `pi` runs a key beginning with `!` on every model request, so no confined process or agent container can write the file; the engine refreshes an OAuth login an hour before it expires |
| the shared-process warning | at startup and in `impi doctor`: which user agents with a shell run unconfined in the engine's process space, and each agent's confinement level |
| `returns_untrusted` wrapping | other people's text arrives marked as such |
| the prompt envelope is tagged per turn and quotes are escaped | a message cannot close a context block or pose as another speaker; the model is told so in its system prompt |
| contained card text | what a caller supplies is content, never structure (newlines, fences, bidi) |
| the trace keeps arguments, never results | a granted value cannot surface in the inventory |
| what is kept on disk is the owner's alone | session directories 0700, the inventory and its log 0600, tool manifests and attachments 0600; the engine runs with `umask 077` |
| the services run in boxes: read-only root, `/tmp` in memory, no capabilities, `no-new-privileges`, `pids_limit` | the engine, every agent container, the broker; the browser adds seccomp |
| non-root images; base images pinned by digest; the runtime and the browser tool from a lockfile with integrity hashes, install scripts ignored; Python dependencies locked with hashes | the image is what the release says it is, and nothing runs at build time that nobody reviewed |
| skill installs shown file by file and pinned to a commit | nothing lands unseen; `update` shows old and new; unencrypted sources (`http://`, `git://`) are refused |
| every door compares its token in constant time | the http and WebSocket gateways, the tool server, the agent host relay, the session proof |
| the log names, it does not replay | a click's token is logged by its first characters; a command's text only at DEBUG; every line of the engine's and the broker's log goes through `crucible.redaction` first, which replaces what looks like a token, a proof, a password, a receiver's secret path or a private key — a heuristic that narrows a leak, not a licence to log a secret |
| the callback receivers answer only under a secret path | a Mattermost callback carries no signature and names its own user; `INTEGRATIONS_SECRET` (engine) and `WARD_CALLBACK_SECRET` (broker) are what make that claim one only the platform can make |
| no MCP in the runtime (`--no-mcp` on every process), no plugin-bundled servers; an MCP server's tools come through the engine, pinned ([mcp.md](mcp.md)) | the runtime's tool surface is the manifest and nothing else: an `mcp.json` in the runtime's own directory or in a profile's `.pi/` starts no server and adds no tool |

**Your decisions**

| decision | default | when to change it |
|---|---|---|
| a shell (`bash`, `read`) in an agent's allowlist | the profile's choice | give it only to agents whose input you trust, or isolate them |
| [confinement](configuration.md) (`RUNTIME_CONFINEMENT`, `runtime.confinement`) | `conversation`; `support` `off` | `agent` when an agent's conversations must share files; `off` only for an agent you trust with everything its user can reach |
| [a container per agent](agent-containers.md) | off | more than one agent, and not all of them equally trusted |
| [the secret broker](secrets.md) | off | any credential an agent should use on your behalf |
| the broker's unattended unlock | off | a scheduled task needs a secret while nobody is awake — read the trade first |
| `WARD_APPROVERS` | — | the people on it can reach every secret some agent may; keep it short |
| `TOOL_MAX_GRANT_S` / a policy's `--max-grant` | 15 min / 1 h | how long "allow for…" may leave a tool or a secret open |
| the [browser](browsing.md) | off | one browser serves every agent; cookies are shared between them |
| egress | unrestricted | a proxy or firewall around the containers when the agents must not reach arbitrary hosts |
| who answers a tool's confirmation (`TOOL_APPROVERS`) | the person who asked the agent | name the people instead when the asker should not be the one deciding |
| retention (`ATTACHMENT_RETENTION_DAYS`, `TOOL_TRACE_RETENTION_DAYS`, `SESSION_RETENTION_DAYS`) | 14 / 14 / never | data classification says so |

## Known limits

Things the design does not solve, stated so nobody relies on them by accident.

- **An MCP server on stdio runs as the engine.** It is a child of the engine
  process with the engine's rights and no confinement of its own — a service
  the deployment installed, trusted like the engine's code. A server that
  should see less runs over HTTP, elsewhere.
- **A trusted MCP server's words reach the model unreviewed.** `trust: trusted`
  takes the server's tool definitions as they come, at start and at every
  recheck; a server that turns hostile changes what the model is told. The
  default, untrusted, withdraws changed tools until a person looks.

- **Confinement needs Landlock.** A kernel without it (older than 5.13, or
  with Landlock left out of the boot's LSM list) runs every process
  unconfined — the engine warns at startup and in `impi doctor`. There, an
  unconfined neighbour with a shell reads the tool server's secrets, and the
  boundary is the container, not the secret. An older Landlock holds less:
  before Linux 6.2 (ABI 3) a confined process can still truncate a file its
  user may write, and before 6.12 (ABI 6) it can signal processes outside its
  domain. `impi doctor` prints the ABI.
- **Confinement restricts TCP ports, not addresses.** A confined process
  connects only to the ports named (`RUNTIME_NETWORK_PORTS`, 443 and 80 by
  default, and those of the services its environment points it at) — so the deployment's
  databases, hubs and receivers on ports of their own are out of its reach —
  but on a port it may use it reaches every address: the internet on 443, and
  anything of the deployment's that listens there. Landlock knows TCP alone:
  UDP, and so DNS, is open. Network rules need Landlock ABI 4 (Linux 6.7);
  before that the files are confined and the network is not, and the startup
  line says so.
- **An agent at `off` is unconfined**, and so is `support` unless its profile
  says otherwise; the engine names the user's agents concerned.
- **One browser for every agent.** Per-agent browser contexts are not
  implemented; what one agent logs into, another can use.
- **The broker binds an argv it is told.** `secret-exec` runs the command it
  declared, so for an honest caller the card shows what runs; anything else in
  the container can claim any argv. A rule narrows what an honest caller may do
  quietly and is worth nothing against a compromised container.
- **The runtime's login is shared.** Every agent on the deployment's login
  calls the model as the same account and can read it — use it, or copy it.
  Only the engine and unconfined processes can write the file: a confined
  process and an agent's container get it read-only, because `pi` runs a key
  that begins with `!` on every model request. An unconfined agent (`off`)
  could still plant one; `pi` is then not started anywhere until a fresh
  login replaces the file, but processes already running would read it. An
  API key is different: `AGENTS_LLM_API_KEY__<AGENT>` puts a key in one agent's
  environment and in no other's (the engine's own agent, which inherits the
  engine's whole environment, excepted) — where an agent answers with a model
  whose key must not reach the rest, that is the setting; the login file is
  read by every process still.
- **The engine does not restrict egress** and does not encrypt session files
  at rest; both are the deployment's.
- **Connected accounts are a direct-message feature.** The panel opens and the
  tools act only in a direct message with the agent; who a turn acts for in a
  channel or a merged thread is not designed yet. They are not offered at all
  on the ws and http gateways, whose user ids are whatever the calling program
  says, nor in a scheduled turn. A connection outlives its owner's chat
  account until they disconnect or the provider revokes it, and what the
  provider said of the person at the connect (`account.extra`, a groups
  claim) stays as said until they connect again: a group taken away at the
  provider is still in the record, so a rule that reads it decides on the
  connect's groups, not today's.
- **Prompt injection is bounded, not eliminated.** The allowlist, the gate and
  the broker decide what a talked-into agent can do; nothing decides whether
  it was talked into it.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) at the repository root.
