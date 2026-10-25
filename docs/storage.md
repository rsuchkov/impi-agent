# Storage

The engine keeps two very different kinds of state, and knowing which is which
is the whole point of this page. One of them can be moved off the process. The
other cannot, today, and no setting here changes that.

## The two kinds

**The inventory** is what the engine knows: which conversations exist and which
runtime session each maps to, scheduled tasks and their run history, the
scheduler's heartbeat, approval windows and the ledger of what was asked,
pending forms and unclicked widgets, the agent registry, and people's
connected accounts. This is the part with a backend setting.

**Conversation memory** is what an agent remembers. That belongs to the runtime,
which keeps it as session files on disk — under `{DATA_DIR}/pi-sessions/` in the
engine (a directory per agent, and in it one per conversation), or inside the agent's own volume when it has a container of its own.
There is no remote backend for it. An agent whose files are gone starts the
conversation again from nothing, whatever the inventory says.

The split is deliberate and predates the choice of backend: the inventory is
recomputable bookkeeping — a session id is *derived* from `(agent,
conversation)`, never invented — while the memory is the actual content of a
relationship with a person.

**On disk, both are the owner's alone.** The SQLite file (with its write-ahead
log) is created 0600, the runtime's session directories 0700, the tool
manifests 0600 (an agent's, and a conversation's own when it is entitled to
less — forgotten with the conversation), attachments 0600 under 0700 directories — whatever the
process's umask: a wider one found on disk is narrowed — the inventory and
the manifests at start, a session directory when its agent runs, attachments
as they are written. The engine itself runs with `umask 077`. Neither kind is encrypted
at rest; that is the volume's or the disk's to do.

**Conversation memory is kept until somebody deletes it**, unless you say
otherwise: `SESSION_RETENTION_DAYS=N` makes the engine forget, at startup,
every conversation idle for N days or more — its record and the runtime's
files for it, exactly what `impi sessions purge-idle --days N` does by hand.
An agent in a container of its own keeps its files in that container's volume;
only its record is forgotten there.

## Choosing a backend

| | SQLite (default) | MongoDB |
|---|---|---|
| where it lives | a file under `DATA_DIR` | a server |
| extra dependency | none | `pymongo` (shipped in the engine image) |
| extra container | none | one |
| what it buys | nothing to run | the engine stops owning state it cannot lose |

SQLite is the right answer for a deployment that lives on one host and keeps a
volume. It needs nothing installed, and `impi update` carries the file across a
release untouched.

MongoDB is the right answer when the engine should be replaceable: several
replicas, or a scheduler that may be rescheduled onto another node. Its value is
not speed — the inventory is small and neither backend is a bottleneck — it is
that no conversation, task or approval is lost when the container is.

### What MongoDB does not do

It does not make the deployment stateless. The agents still keep their memory in
files, so a pod that can be moved needs those files to follow it — a volume that
survives, mounted where the runtime writes. Turning this on and deleting the
agents' volumes gives you an engine that remembers every task and no
conversation — unless the application hands the history back
(`HistorySource`), which restores what was said, not what tools did.

## Turning it on

At install time the installer asks. On a deployment that already runs, set the
axis and bring the stack back up:

```sh
# in ~/.impi/compose.env
IMPI_MONGO=1
```

```sh
impi restart
impi doctor        # says which inventory the engine actually reached
```

The overlay (`deploy/compose.mongo.yaml`) adds a `mongo` service on the
deployment's own network, publishing nothing to the host, and points the engine
at it. To use a database you already run, skip the overlay and set the settings
directly in `conf/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `STORE_BACKEND` | `sqlite` | what kind: `sqlite` or `mongo` |
| `DB_NAME` | `""` | which one: a file path on `sqlite` (default `{DATA_DIR}/impi.db`), a database name on `mongo` (default `impi`) |
| `DB_URL` | `""` | where the server is; required on `mongo`, unused on `sqlite` |
| `DB_COLLECTION_PREFIX` | `""` | on `mongo`, a prefix for every collection name (`sessions` → `engine_sessions`); letters, digits, `_` and `-`. Unused on `sqlite` |

Three keys, and the first decides what the second means. Anything else a
backend needs goes in `DB_URL`, which is where a connection string already
carries a replica set, TLS or credentials:

```sh
DB_URL=mongodb://user:pass@a:27017,b:27017/?replicaSet=rs0&tls=true
```

The fourth is for a database that is not this engine's alone — shared with
another engine, or with an application's own collections — and is empty by
default, so an existing deployment keeps its collection names. Set it before
the first start: changing it later is the same as switching backends (below),
the old collections stay where they were and nothing reads them. A library
application passes it as `open_store(..., prefix=…)`.

`DB_PATH` is the name `DB_NAME` had when SQLite was the only option. It still
works — a deployment that set it keeps opening the same file — but rename it:
on a server backend a key called `DB_PATH` names a database, which reads as a
mistake even when it is not.

A backend name that is not one of the two, or `mongo` without a URL, stops the
engine at startup with a sentence saying so. That is on purpose: the failure
this replaces is an engine that quietly opens a SQLite file nobody writes and
reports an empty stand.

## Switching an existing deployment

There is no migration. Switching backends starts from an empty inventory: the
conversations are still in the chat platform and the agents still have their
memory, but scheduled tasks, approval windows, the ledger and connected
accounts do not come across. Recreate the tasks (`impi task add`), re-approve
what needs it, and expect everyone to connect their accounts again.

Switching back is the same in reverse — the SQLite file is left where it was, so
setting `IMPI_MONGO=0` returns to whatever it last held.

## Collections

One per subject, named the way the SQLite tables are:

| Collection | Holds |
|---|---|
| `sessions` | conversation → runtime session |
| `pending_interactions` | widgets awaiting a click (one-shot) |
| `pending_forms` | modal forms awaiting a submit |
| `held_cards` | confirmation cards a conversation holds on a gateway outside any turn of that gateway's (raised on another gateway), until their verdict |
| `processed_posts` | dedup, so a redelivered message is not answered twice; forgotten after `PROCESSED_POSTS_RETENTION_DAYS` |
| `agents` | the registry, synced from profiles at boot |
| `tasks` | scheduled work |
| `task_runs` | run history |
| `scheduler_heartbeat` | one document; the scheduler's own liveness |
| `approval_grants` | windows a human left open |
| `approval_audit` | the ledger of what was asked and how it came out |
| `tool_traces` | one turn's tool calls, for the widget under its reply — arguments and outcomes, never results |
| `connections` | people's accounts connected at an OAuth provider: the tokens sealed (unreadable without the sealing key), whose and which account readable; one per person and provider |
| `connection_links` | connects started and not yet finished — single use, gone after ten minutes (a TTL index) |
| `leases` | what runs in one process at a time: a gateway's connection (`gateway:<kind>:<agent>`), a replica's liveness (`replica:<id>`, the holder its address) |
| `conversation_owners` | which replica serves which conversation; an owner whose `replica:` lease lapsed is replaced |

The indexes are created on first use and are load-bearing rather than an
optimisation: they are what makes a claimed occurrence unable to fire twice,
a replayed message a no-op, and a second connect of one person to one
provider a reconnect rather than a duplicate. A deployment that dropped them would not slow down,
it would double-fire.

One of them changed shape once: `sessions.runtime_session_id` became **unique**
(a tool call names that key alone, so two conversations must never share one).
Mongo will not change an index's options in place, so a deployment from before
the change has to drop the plain index by hand before updating — the engine
refuses to start until it is gone:

```
db.sessions.dropIndex("runtime_session_id_1")
```

## What stays on SQLite regardless

The secret broker keeps its own database — policies, windows and its ledger —
and it is not affected by this setting. It is a separate process with a
different threat model, and its state is small, local and deliberately not
shared. See [secrets.md](secrets.md).
