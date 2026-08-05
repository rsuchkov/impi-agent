# Changelog

Notable changes per release, newest first. The **Unreleased** section collects
what will ship next; `scripts/release.sh` stamps it with the version and date
when a release is cut, and `impi update` shows the target version's section.

## Unreleased

_Nothing yet._

## v0.5.0 — 2026-08-05

- **Widgets and forms gained the full control vocabulary of both platforms.** A
  form field can now be `text`/`textarea`, `number`/`email`/`url`/`tel`,
  `select`/`multiselect`/`radio`, `bool`, a workspace picker
  (`user`/`users`/`channel`/`channels`), `date`/`datetime`/`time`, or a static
  `label` — one neutral vocabulary that Mattermost renders as dialog elements
  and Slack as Block Kit, up to 15 fields with `help_text` hints. In a message,
  `ask_user_select` takes `source: users` / `channels` to post a people or
  channel picker. Picked people and channels come back as `@name (id)` /
  `~name (id)`, so the agent reads a name and can still pass the id to a tool.
  Type table and Mattermost version floors: docs/creating-agents.md.
- **The form button's wording is the agent's call** — `open_form` takes
  `open_label` ("Report a bug", "Book a slot"); omitted, it stays "📝 Fill in…".
- **A form's "Fill in…" button now behaves the same on both platforms:** it
  survives a modal closed without submitting (so it can be reopened) and is
  struck off its message when the form is answered. Previously Slack retired it
  the moment the modal opened, while Mattermost left it clickable forever — a
  click after submitting hit a form that no longer existed.

## v0.4.3 — 2026-08-03

- **Fixed: co-deploying Mattermost failed on Apple Silicon** with `no matching
  manifest for linux/arm64` — Mattermost ships amd64 images only (no tag has an
  arm64 manifest). The service now pins `platform: linux/amd64` so an ARM host
  emulates it, its health start-period allows for the slower emulated boot, and
  the installer says so up front on an ARM machine.
- **The provider/model questions explain what "default" means.** Leaving them
  empty passes no provider/model flag at all, so `pi` follows its own settings —
  i.e. whatever you pick during the login step; filling them in pins a backend
  that an agent's `agent.yaml` can still override. The summary now spells out
  which of the two you ended up with.

## v0.4.2 — 2026-08-03

- **Fixed: installer prompts were not in raw mode under `curl … | bash` on
  macOS.** bash 3.2 applies `read`'s `-s`/`-n` terminal settings to fd 0
  whatever `-u` says, and there fd 0 is the pipe — so the tty stayed in cooked
  mode: arrow keys echoed as `^[[B` instead of moving the menu cursor, and a
  pasted token was printed in clear text by the "no echo" prompt. Every prompt
  now reads with `<&3` instead of `-u3`.

## v0.4.1 — 2026-08-03

- **Fixed: the image failed to build on macOS.** The engine image is built with
  the operator's uid/gid, and macOS gid 20 (`staff`) is already Debian's
  `dialout`, so `groupadd` aborted the build (exit 4). The image now reuses
  whatever group/user holds those ids and only creates what is missing (the
  same collision hit Linux hosts with a gid like 100), and the installer keeps
  the default ids on macOS — where the engine runs in a VM that maps bind-mount
  ownership itself.
- **Fixed: arrow keys did not move the installer menu on macOS.** Escape
  sequences are now read one byte at a time and both forms are accepted — CSI
  (`ESC [ A`) and the SS3 (`ESC O A`) that terminals send in application cursor
  mode. The menu also shows its keys (`↑/↓ or j/k · Enter · number`).

## v0.4.0 — 2026-08-03

- **Commands: agents can be invoked by a slash command or a message shortcut.**
  Mattermost posts a slash command to `/command/<agent>` on the interactions
  receiver (verified by the command's token, `AGENTS_COMMAND_TOKENS__<AGENT>`);
  Slack uses `crux_*` message shortcuts over its socket (`SLACK_COMMAND_PREFIX`),
  since it forbids custom slash commands in threads. Either becomes an ordinary
  turn in the invoking thread, answered there like any message; what a command
  means is up to the agent's SYSTEM.md. New guide: docs/commands.md — including
  the pattern for commands whose result must stay private or be produced
  deterministically: handle them beside the agent with `run_stateless` instead
  of leaving delivery to the model.
- **Agents no longer miss thread messages posted between their replies.** In a
  channel an agent only runs when mentioned, so anything said in the thread in
  between never reached it. Every turn now replays the messages posted since the
  agent's last reply (its own posts excluded — those are already in its session).
- **Agents can send ephemeral messages** (visible to one user only) via the new
  `send_ephemeral` tool, where the platform supports it — Mattermost and Slack;
  the ws gateway doesn't advertise it. Targets the user who triggered the turn
  by default, or a given `@username`. Gated by a new `CAP_EPHEMERAL` capability;
  the session now records the last triggering user so a mid-turn tool can
  address them. Note: Mattermost requires the `create_post_ephemeral` permission
  (a bot lacks it by default — grant it to the bot's role); see
  docs/creating-agents.md.
- **`ws` gateway: plug your own services into the engine.** A duplex WebSocket
  hub (`WS_PORT`, default 8424): a client service dials in with its service
  token (`impi ws add-service`) and exchanges JSON frames — `message` in,
  addressed to any allowed agent per frame, `reply`/`notice` out on the same
  socket, `{type: agents}` for discovery. Conversations are isolated per
  `(agent, service, conversation_id)`; replies to an offline service buffer
  and flush on reconnect. Agents opt in with `AGENTS_GATEWAY__<AGENT>=ws`.
  See docs/ws-gateway.md.
- Documented that gateway kinds mix freely in one engine process (Slack +
  Mattermost + ws side by side) — this already worked, now it's official.

## v0.3.0 — 2026-07-28

- **Slack output formatting is now the gateway's job.** Outgoing agent prose is
  converted from Markdown to Slack mrkdwn at the adapter boundary (headings,
  bold/italic, links, images, bullet/task lists, strikethrough; tables are
  flattened into lists; fenced and inline code pass through untouched;
  malformed asterisk runs from weak models are repaired). The prompt hint no
  longer teaches the model mrkdwn — models just write plain Markdown.
- **`impi login` publishes the OAuth callback port (1455)**, so the
  openai-codex `/login` flow completes from inside the container.
- **pi crashes are diagnosable from the turn error.** "pi process exited
  unexpectedly" now carries the exit code and the last stderr lines (the
  actual cause: bad models.json, unreachable endpoint, ...). Widget-post
  failures log the platform error code in the message itself.
- **Docs:** built-in tools vs the working directory (and how to reach data
  outside the profile), pi permission-denial troubleshooting, `models.json`
  env-interpolation limits (`apiKey`/`headers` only), composition notes for
  standalone apps (interactivity opt-out, shared tool settings).

## v0.2.0 — 2026-07-27

- **One-line install:** `curl -fsSL .../install.sh | bash` — interactive TUI
  questionnaire, compose-only deployment, optional co-deployed Mattermost Team
  Edition with zero-touch bootstrap (admin, team, tokens — no browser),
  `--answers` mode for CI and a full e2e test target.
- **`deploy/`:** the engine Dockerfile (Python 3.13 + Node 22 + pinned pi) and
  compose base + overlays (mattermost, external-mm, rootless podman).
- **Agent provisioning:** `impi agent add` / `impi provision support` /
  `impi mm bootstrap-token` in the container CLI, and the `create_agent`
  engine tool — the support agent creates new agents from chat (bot account +
  profile + `.env`) behind a button confirmation.
- **Host wrapper `impi`:** status / logs / start / stop / restart / agent /
  login / update (tag-based, with health gate and rollback) / doctor /
  uninstall.
- **Versioning:** `VERSION` + SemVer `v*` tags, `scripts/release.sh`.

(Earlier history — the engine itself, multi-agent wiring, widgets and forms,
the crucible/impi split — predates tagged releases; see `git log`.)
