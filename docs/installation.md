# Installation

The supported way to run impi is the containerized deployment installed by the
interactive installer. For hacking on the engine itself, use the dev checkout
flow in [README.md](../README.md) instead.

## One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/rsuchkov/impi-agent/main/install.sh | bash
```

Prerequisites: **Linux or macOS**, **git**, and a compose runtime — Docker with
compose v2, or podman (rootless is fine). `docker-compose` v1 is not supported.

**Docker vs podman:** with Docker the stack comes back after a machine reboot
on its own — the daemon restores `restart: unless-stopped` containers — with
one exception worth knowing. A Docker Engine older than 29.0 restores them in
no particular order, and the secret broker lives inside the store's network
namespace: a daemon that reaches the broker first fails it for good, so the
store comes back and the broker does not. `impi doctor` names that state, and
`impi start` — idempotent, it applies the order the daemon lacks — brings the
broker back; Docker 29.0 fixed the order. podman is daemonless, so after a
reboot you run `impi start` yourself, or enable its own boot-time restart
(`systemctl --user enable --now podman-restart.service` and
`loginctl enable-linger "$USER"`), which starts the store before the broker.
Data survives either way (volumes + bind mounts). When both are installed the
installer asks which one to use (Docker recommended; pre-seed with
`IMPI_RUNTIME=docker|podman`).

The bootstrap clones the repository at the **latest release tag** into the
install directory (default `~/.impi`) and hands over to the versioned installer
inside the clone, which walks you through:

1. **Chat platform** — Mattermost (full experience) or Slack (bring your own
   Slack app tokens).
2. **Which Mattermost** — deploy Team Edition + Postgres right next to impi
   (zero-touch: the admin account, team, and bots are created for you, no
   browser needed), or connect to an existing server (optionally with a
   system-admin token so impi can create bot accounts itself).
3. **Support bot** — impi's bundled agent-builder; ask it in chat to create and
   maintain your other agents, install skills from the shared library, wire up a
   slash command, or work out why something isn't running.
4. **Agents directory** — where your agent profiles live (git-initialized when
   possible); the first neutral agent is scaffolded for you.
5. **Interactivity** — button/form widgets (recommended on).
6. **Model backend** — an OpenAI-compatible endpoint (base URL + API key), or
   pi's subscription login (an interactive `/login` inside the container;
   the credential lands in `~/.impi/pi/agent/auth.json` on the host).

Everything is summarized before anything is written. The full log lands in
`$IMPI_HOME/install.log`.

## What an installation looks like

```
~/.impi/
  repo/           # git clone at the installed release tag
  conf/.env       # engine config + secrets (chmod 600), mounted into the container
  conf/ward.env   # the secret broker's own settings, if you deployed one
  certs/          # the agents' identities for the broker, if you deployed one
  compose.env     # infra knobs for compose + the wrapper (no secrets)
  pi/             # pi's home: the model credential (pi/agent/auth.json), mode 700
  agents/         # your agent profiles (default location; git repo)
  install.log
```

The engine runs as the `impi` compose service built from `deploy/Dockerfile`
(Python 3.13 + Node 22 + `ripgrep` and `fd` for pi's `grep`/`find` + the `pi` CLI, installed from `deploy/pi/package.json`
and its lockfile with every dependency pinned by hash and install scripts
ignored; the base images are pinned by digest). Engine state lives in the
`impi-data` volume; pi's home — the model credential — is `~/.impi/pi` on the
host, so that an agent's container can be handed the one file out of it. With a co-deployed
Mattermost, the `mattermost` + `db` services join the same compose project and
widget callbacks flow container-to-container (`http://impi:8423`) — nothing is
exposed except Mattermost itself. Answering yes to the secret store adds
**two** services: the store itself and `ward`, the broker that opens it. They
share one network namespace, so the store listens on a loopback that nothing
else can reach and never appears on the project network — what does is the
broker's door, and it requires a client certificate. Their state lives in the
`vault-data` and `ward-data` volumes, the broker's own settings in
`conf/ward.env`, and the agents' certificates in `certs/`. The store starts
sealed and stays that way until you unlock it (see [secrets.md](secrets.md)).
The broker asks its approvers on whichever chat platform the deployment uses —
a bot on Mattermost, an app of its own on Slack.

## The `impi` wrapper

The installer drops a small CLI into `~/.local/bin/impi`:

| Command | What it does |
|---|---|
| `impi status` | services + installed version |
| `impi logs [-f]` | engine logs |
| `impi start` / `impi stop` | bring the stack up / stop it |
| `impi restart` | restart the engine — picks up `.env` edits and new agents |
| `impi reload` | re-read agent profiles in place (skills, tools, prompts) — no restart |
| `impi skill …` | the shared skill library (see [skills.md](skills.md)) |
| `impi task …` | scheduled and recurring work (see [tasks.md](tasks.md)) |
| `impi ward …` | the secret store and who may reach it (see [secrets.md](secrets.md)) |
| `impi agent add` | interactive agent creation (bot + profile + `.env`) |
| `impi agent list` | profiles with token status |
| `impi escape <agent> [-- "prompt"]` | that agent's pi on this host, in your terminal (see below) |
| `impi login` | pi subscription login inside the container |
| `impi login --copy-auth [file]` | import `~/.pi/agent/auth.json` from a logged-in machine |
| `impi update [--yes]` | update to the newest release tag, rebuild, restart |
| `impi doctor` | quick health checks, and the compose overlays it merged |
| `impi uninstall` | remove containers (volumes only after a second confirmation; `~/.impi` is kept) |

### `impi escape`: an agent's pi in your terminal

`impi escape support` starts `pi` on this host — not in a container — with the
support agent's system prompt, model, tool allowlist and skills, exactly as the
engine would start it, and you at the keyboard. It is for the things an agent
cannot do from inside its container: the `impi` wrapper is on its PATH, so the
support agent runs `impi doctor`, `impi start`, `impi agent sync` or
`impi ward unlock --from …` itself instead of asking you to, and you watch every
call. Any agent works, not only `support`; add `-- "a prompt"` for a one-shot
answer instead of a session.

What carries over: the profile directory (`.pi/SYSTEM.md`, its own skills, any
pi permission policy it ships), the shared skill library, the model and the
`LLM_*` settings from `conf/.env`. What does not: the engine's tools
(`create_agent`, the skill tools, `open_screen`, `ask_user_confirm`,
`schedule_task`, `send_file`) — they live in the engine's tool server and there
is no engine here, so they are dropped from the allowlist and the agent is told
so. Nor any credential of the deployment:
chat tokens, tool tokens and the secret broker's variables stay in the
container, so `secret-exec` on the host answers "not available" whoever asks.
The agent on your host is *you*, with whatever `bash` can do as your user, and
nothing gates its commands but your eyes — a per-command policy is pi's own
`.pi/agent/pi-permissions.jsonc` in the profile, which works in a terminal.

The session is ephemeral: pi writes it under `~/.impi/escape/`, and the
directory is removed when pi exits, however it exits. `--keep` leaves it and
prints how to continue it (`--session-dir <path>`); a directory you name with
`--session-dir` is yours and is never removed. The deployment's own
conversations are not touched. If the session changed a profile or a drop-in,
the wrapper says which command applies it (`impi reload` / `impi start`) — it
does not undo the agent's work.

`pi` has to be installed on the host: the wrapper offers pi's own installer
(`curl -fsSL https://pi.dev/install.sh | sh`) when it is missing, and notes when
its version differs from the one the engine pins (`deploy/pi/package.json`). `--dry-run` prints the working
directory, environment and command line without starting anything; it needs
the engine's image, because the engine is what builds them.

## The boxes the services run in

The engine, every agent container and the broker run with a **read-only
root**, `/tmp` in memory, **no Linux capabilities**, no privilege gain
through setuid binaries (`no-new-privileges`) and a ceiling on processes
(`pids_limit`). Everything they write goes to a volume or a bind mount: the
engine to `/app/data`, `/app/conf`, `/app/agents`, `/app/skills`, `/app/files`
and `/home/impi/.pi` (the `pi/` directory on the host); an agent to its
sessions and files volumes; the broker
to `/var/lib/ward`. The browser container has had this since it existed, with
a seccomp profile on top.

Inside those boxes each agent's runtime process is confined further, with
Landlock: it writes its own conversation's memory and files and its own
`TMPDIR` and `HOME`, and nothing else. A tool that writes elsewhere fails with
`Permission denied` rather than `Read-only file system` — a tmpfs does not
help there; declare the path in the agent's profile or change its level
(`RUNTIME_CONFINEMENT` in [configuration.md](configuration.md)). It needs a
host kernel with Landlock (Linux 5.13+, with Landlock in the boot's LSM list),
and a container runtime whose seccomp profile lets the `landlock_*` calls
through; the confinement line in `impi doctor` says what this host gives.
Without it the engine warns and the agents run unconfined.

An image of your own — a `Dockerfile.include` that installs something which
writes under its own directory at run time, a JDK's cache — hits the read-only
root and fails with `Read-only file system`. (`$HOME` is not the case: it is
memory already, and a confined process has a scratch `HOME` of its own.) The
fix is a tmpfs for that path, in a drop-in:

```yaml
# ~/.impi/compose.d/agent-cache.yaml
services:
  agent-assistant:
    tmpfs:
      - /tmp:mode=1777,size=256m
      - /home/impi:mode=1777,size=64m
      - /home/impi/.pi:mode=1777,size=16m
      - /home/impi/.pi/agent:mode=1777,size=16m
      - /opt/mytool/cache:mode=1777,size=64m
```

Repeat the generated entries as above: depending on the compose implementation
a drop-in's `tmpfs` list is merged with the generated one or replaces it, and a
replaced list without `/home/impi/.pi/agent` leaves `pi` nowhere to take its
lock — every turn then fails with "No API key found". At the default
confinement level the runtime process cannot write the new path either; declare
it in the agent's profile (`runtime.confinement_write` does not apply in a
container of its own) — or give that agent `runtime.confinement: off`.
Rootless podman needs no adjustment: `keep-id` and a read-only root compose,
and `mode=` rather than `uid=` on the tmpfs is deliberate — podman supports no
owner on one.

## Updates and versioning

Releases are SemVer 0.x git tags (`vX.Y.Z`, created with `scripts/release.sh`);
the `VERSION` file mirrors the tag, and [`CHANGELOG.md`](../CHANGELOG.md)
carries the release notes (the release script stamps the `Unreleased` section
with the version and refuses to cut a release with empty notes). `impi update`
fetches tags, shows the target version's release notes, checks out the newest
tag, rebuilds the images (the engine's, the broker's and every agent
container's, after re-rendering the agents' containers with the new engine),
recreates the services that were rebuilt — explicitly, because podman-compose
would otherwise keep a container whose definition did not change on the old
image — and restarts, with a health gate and an offered rollback if the new
engine does not come up.

## Your own compose overlays

Need something else in the stack — a Cloudflare tunnel, a reverse proxy, an extra
volume on the engine? Drop a compose file into **`$IMPI_HOME/compose.d/`**:

```bash
$EDITOR ~/.impi/compose.d/cloudflared.yaml
impi restart
```

Every `*.yaml` there is merged after impi's own files, alphabetically, so it can
add services or override the engine's own. The directory is yours — updates
never read, rewrite or delete it, and `impi doctor` lists what it found there.

Do **not** edit files under `~/.impi/repo/`: that is a git checkout the updater
replaces (and `impi update` refuses to run while it has local changes). Likewise,
the engine's own compose file list is *derived* on every call from
`IMPI_MM_MODE` + `IMPI_COMPOSE_ROOTLESS` in `compose.env` — that is what lets a
new release add an overlay without a stored list to regenerate. Older
installations recorded the derived list in `IMPI_COMPOSE_FILES`; the wrapper
reads the mode back out of it once, and if you had added files of your own there,
it prints exactly where to move them. Everything else you put in `compose.env`
stays: only the keys impi writes are ever rewritten.

Your data survives an update untouched: the inventory lives in a volume — its
own SQLite file, or the database container when the deployment keeps it on
MongoDB ([storage.md](storage.md)) — and the engine brings it up to date itself
at startup, adding what a release needs (a column on SQLite, an index on Mongo)
and rewriting nothing. That also makes the rollback safe: both backends read by
an explicit field list, so an older engine ignores what it doesn't know and
keeps running on a database a newer one has already touched.

## Non-interactive installs (CI / e2e)

Every prompt maps 1:1 to an `IMPI_*` variable; pass them as a file:

```bash
curl -fsSL .../install.sh | bash -s -- --answers my.answers
```

See `installer/tests/e2e.answers` for a complete example. In answers mode a
missing required key is a hard error, never a hang. `make e2e-install` runs a
full zero-touch install of the current working tree into a throwaway
`IMPI_HOME` (dedicated compose project `impi-e2e`, Mattermost on port 8066) and
asserts services, bootstrap, bots, config, and engine readiness.

## macOS notes

- The installer is bash-3.2 compatible (the stock macOS bash); arrow-key menus
  fall back to numbered prompts on plain terminals. In a menu you can always
  type the option's number instead of using the arrows.
- Docker Desktop or podman machine must be running before you start.
- **Apple Silicon:** Mattermost publishes amd64 images only, so a co-deployed
  server runs **emulated** — turn on amd64 emulation (Docker Desktop: Settings →
  General → *Use Rosetta*; podman: the machine's qemu) and expect the first
  start to take minutes. Without it the pull fails with `no matching manifest
  for linux/arm64`. The impi image itself builds natively. An external
  Mattermost (or Slack) has none of this.
- The image keeps its default uid/gid (1000) here instead of the Mac's: the
  engine runs in a VM that maps bind-mount ownership on its own, so matching
  the host ids buys nothing (and macOS gid 20 is a system group in the image's
  Debian base). On Linux the image is built with your own ids.
- The LAN-IP suggestion for external-Mattermost widget callbacks uses
  `route get default` + `ipconfig getifaddr`; confirm or override the URL when
  prompted.

## Troubleshooting an install

- Re-run later steps without reinstalling: the bots and config are idempotent
  to reasonable degrees — `impi agent add`, `impi provision support` (inside
  `impi agent`/`compose run`), and `impi login` can each be run again.
- `impi doctor` checks the containers, config permissions, and engine readiness.
- Zero-touch Mattermost bootstrap uses `mmctl --local` inside the container;
  if it is unavailable the installer falls back to first-user signup via the
  API, then to manual web-UI instructions.
- Logs: `$IMPI_HOME/install.log` (installer), `impi logs -f` (engine).
