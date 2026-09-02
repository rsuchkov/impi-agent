# Storage

The engine keeps two very different kinds of state, and knowing which is which
is the whole point of this page. One of them can be moved off the process. The
other cannot, today, and no setting here changes that.

## The two kinds

**The inventory** is what the engine knows: which conversations exist and which
runtime session each maps to, scheduled tasks and their run history, the
scheduler's heartbeat, approval windows and the ledger of what was asked,
pending forms and unclicked widgets, and the agent registry. This is the part
with a backend setting.

**Conversation memory** is what an agent remembers. That belongs to the runtime,
which keeps it as session files on disk — under `{DATA_DIR}/pi-sessions/` in the
engine, or inside the agent's own volume when it has a container of its own.
There is no remote backend for it. An agent whose files are gone starts the
conversation again from nothing, whatever the inventory says.

The split is deliberate and predates the choice of backend: the inventory is
recomputable bookkeeping — a session id is *derived* from `(agent,
conversation)`, never invented — while the memory is the actual content of a
relationship with a person.

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
conversation.

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

Three keys, and the first decides what the second means. Anything else a
backend needs goes in `DB_URL`, which is where a connection string already
carries a replica set, TLS or credentials:

```sh
DB_URL=mongodb://user:pass@a:27017,b:27017/?replicaSet=rs0&tls=true
```

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
memory, but scheduled tasks, approval windows and the ledger do not come across.
Recreate the tasks (`impi task add`) and re-approve what needs it.

Switching back is the same in reverse — the SQLite file is left where it was, so
setting `IMPI_MONGO=0` returns to whatever it last held.

## Collections

One per subject, named the way the SQLite tables are:

| Collection | Holds |
|---|---|
| `sessions` | conversation → runtime session |
| `pending_interactions` | widgets awaiting a click (one-shot) |
| `pending_forms` | modal forms awaiting a submit |
| `processed_posts` | dedup, so a redelivered message is not answered twice |
| `agents` | the registry, synced from profiles at boot |
| `tasks` | scheduled work |
| `task_runs` | run history |
| `scheduler_heartbeat` | one document; the scheduler's own liveness |
| `approval_grants` | windows a human left open |
| `approval_audit` | the ledger of what was asked and how it came out |

The indexes are created on first use and are load-bearing rather than an
optimisation: they are what makes a claimed occurrence unable to fire twice and
a replayed message a no-op. A deployment that dropped them would not slow down,
it would double-fire.

## What stays on SQLite regardless

The secret broker keeps its own database — policies, windows and its ledger —
and it is not affected by this setting. It is a separate process with a
different threat model, and its state is small, local and deliberately not
shared. See [secrets.md](secrets.md).
