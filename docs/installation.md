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

**Docker vs podman:** with Docker the stack comes back automatically after a
machine reboot (the daemon restores `restart: unless-stopped` containers);
podman is daemonless, so after a reboot you run `impi start` yourself — data
survives either way (volumes + bind mounts). If you want auto-start, use
Docker. When both are installed the installer asks which one to use
(Docker recommended; pre-seed with `IMPI_RUNTIME=docker|podman`).

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
   maintain your other agents.
4. **Agents directory** — where your agent profiles live (git-initialized when
   possible); the first neutral agent is scaffolded for you.
5. **Interactivity** — button/form widgets (recommended on).
6. **Model backend** — an OpenAI-compatible endpoint (base URL + API key), or
   pi's subscription login (an interactive `/login` inside the container;
   credentials persist in a volume).

Everything is summarized before anything is written. The full log lands in
`$IMPI_HOME/install.log`.

## What an installation looks like

```
~/.impi/
  repo/         # git clone at the installed release tag
  conf/.env     # engine config + secrets (chmod 600), mounted into the container
  compose.env   # infra knobs for compose + the wrapper (no secrets)
  agents/       # your agent profiles (default location; git repo)
  install.log
```

The engine runs as the `impi` compose service built from `deploy/Dockerfile`
(Python 3.13 + Node 22 + the pinned `pi` CLI). Engine state lives in the
`impi-data` volume, pi's model credentials in `pi-auth`. With a co-deployed
Mattermost, the `mattermost` + `db` services join the same compose project and
widget callbacks flow container-to-container (`http://impi:8423`) — nothing is
exposed except Mattermost itself.

## The `impi` wrapper

The installer drops a small CLI into `~/.local/bin/impi`:

| Command | What it does |
|---|---|
| `impi status` | services + installed version |
| `impi logs [-f]` | engine logs |
| `impi start` / `impi stop` | bring the stack up / stop it |
| `impi restart` | restart the engine — picks up `.env` edits and new agents |
| `impi agent add` | interactive agent creation (bot + profile + `.env`) |
| `impi agent list` | profiles with token status |
| `impi login` | pi subscription login inside the container |
| `impi login --copy-auth [file]` | import `~/.pi/agent/auth.json` from a logged-in machine |
| `impi update [--yes]` | update to the newest release tag, rebuild, restart |
| `impi doctor` | quick health checks |
| `impi uninstall` | remove containers (volumes only after a second confirmation; `~/.impi` is kept) |

## Updates and versioning

Releases are SemVer 0.x git tags (`vX.Y.Z`, created with `scripts/release.sh`);
the `VERSION` file mirrors the tag, and [`CHANGELOG.md`](../CHANGELOG.md)
carries the release notes (the release script stamps the `Unreleased` section
with the version and refuses to cut a release with empty notes). `impi update`
fetches tags, shows the target version's release notes, checks out the newest
tag, rebuilds the image, and restarts — with a health gate and an offered
rollback if the new engine does not come up.

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
