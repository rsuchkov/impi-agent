# Changelog

Notable changes per release, newest first. The **Unreleased** section collects
what will ship next; `scripts/release.sh` stamps it with the version and date
when a release is cut, and `impi update` shows the target version's section.

## Unreleased

_Nothing yet._

## v0.12.0 — 2026-08-24

- **Secrets an agent can use but never read.** An agent runs `secret-exec --env
  GITHUB_TOKEN=vault://github-token -- gh release create …`; you get a card in
  chat showing the agent, the secret, the reason and the exact command, and
  answer **Allow once**, **Allow for…** (1 min to 1 hour) or **Deny**. On an
  approval the value is injected straight into that child process — it never
  enters the model's context, the session history or the logs. One request may
  name several secrets, served together or not at all, under the shortest window
  their policies allow.

  What decides is a broker in **its own container**, beside the store it opens
  and away from the engine: it holds the store's credential, the policies, the
  windows and the ledger, and posts the cards as its own chat account. Agents
  reach it over mutual TLS, and the certificate is what says which agent is
  asking — a header would only be a claim. The certificate authority lives with
  the broker and its key goes nowhere else, so nothing on the engine's side can
  invent an agent; `impi ward cert <agent>` asks the broker for an identity.

  Only a configured approver can answer, an agent has no way to list what
  exists, and every authorization refusal reads identically so the store cannot
  be mapped by guessing names. Off by default; the installer asks. See
  [docs/secrets.md](docs/secrets.md), which is explicit about what this protects
  against and what it does not.
- **`impi ward …`** — `init`, `unlock`, `status`, `set`, `ls`, `rm`, `policy`,
  `grants`, `revoke`, `audit`, `cert`.
- **Turning the secret store on in a deployment that already runs** takes a few
  steps in a fixed order: the broker's own settings before anything restarts, a
  chat account for it to post as, then the one-time `impi ward init`. They are
  in [docs/secrets.md](docs/secrets.md#turning-it-on-in-a-deployment-that-already-runs).
  `impi update` now rebuilds the broker's image along with the engine's, so an
  update cannot leave the two on different releases.
- **Every agent is told its own name.** `AGENT_NAME` is set in the environment of
  the process an agent runs in, so a program it starts can tell which agent it is
  running as. That is how `secret-exec` finds the right identity, and it is there
  for anything else you have an agent run.
- **A tool's confirmation is now enforced by the engine, and can be given for a
  while.** `requires_confirmation` was checked only in the runtime's extension,
  and the token that extension authenticates with lives in the agent's own
  environment — so a shell in that container could reach the tool server
  directly and never see the question. The check now also happens in the server
  that does the work, and fails closed where there is no way to ask. The same
  card offers **Allow once** / **Allow for…** / **Deny**, so a human can stop
  being asked every single time; `TOOL_MAX_GRANT_S` caps the window.
- **A skill's provenance marker is now `.skill-source.json`** (it was
  `.impi-source.json` — the library that writes it must not name the
  application). Skills installed before this update keep working, but list as
  `local` and refuse `impi skill update` until reinstalled from their source.

## v0.11.0 — 2026-08-10

- **An agent can open an engine panel (experimental).** With the new
  `open_screen` tool, "show me the skills" opens the same panel `/skills` does,
  in the conversation the turn is running in. The model only decides to open it:
  the view is rendered by the engine and every click on it is answered by the
  engine, rewriting the same message with no turn behind it — so the panel can
  never describe a task that isn't there. It also reaches Slack, where a custom
  slash command cannot run inside a thread. Note that the panel is clickable by
  anyone who can see it.

_Nothing yet._

## v0.10.0 — 2026-08-09

- **The support agent caught up with the engine.** Its profile had not changed
  since before the library split, so it described an engine that no longer
  exists and could not do several things it was documented to do. It now has the
  shared-skill tools `docs/skills.md` always promised (`list_skills`,
  `install_skill`, `assign_skill`, `remove_skill` — until now the tool server
  answered every one of them with 403), plus `list_agents`, a blocking
  confirmation, and its own scheduling. Four new skills cover what it had no
  idea about: installing and assigning library skills, registering a slash
  command, scheduled work and why a run didn't happen, and diagnosing the engine
  layer by layer. Its map of the source and of where things live in a container
  is correct again.
- **Fixed: the support agent's documentation didn't ship.** Its knowledge base
  is `$IMPI_ROOT/docs`, and the image build excluded `docs/` — so in a real
  deployment every reference it followed was a dead end. The docs are now part
  of the image, and `IMPI_ROOT` is set there explicitly rather than inferred.
- **Fixed: an engine agent lost `IMPI_ROOT` when `TOOL_ENABLED=false`.** The env
  an engine-owned agent is given is supposed to survive the typed tools being
  off; it didn't.

## v0.9.0 — 2026-08-09

- **A slash command no longer has to name an agent.** Register it with
  `…/command/default` and the engine picks: the only agent running, or the one
  `AGENT_NAME` names when there are several — and the startup log says which. Its
  token goes in the unsuffixed `COMMAND_TOKENS`, the same fallback
  `MATTERMOST_TOKEN` already gets, so a single-agent deployment spells its agent's
  name nowhere at all. Existing `…/command/<agent>` URLs and per-agent token keys
  are untouched, an agent really called `default` keeps its own endpoint, and the
  token check is unchanged: resolving an agent is not authorising a command. With
  several agents and no `AGENT_NAME` among them the command is refused rather than
  handed to a guess.

## v0.8.1 — 2026-08-09

- **Fixed: `/tasks` reached an agent when scheduling was off.** The screen was
  only bound to its word while `SCHEDULER_ENABLED` was true, so with the
  scheduler off the command became an ordinary turn — and a model asked about a
  task list it cannot read describes one anyway. It now answers for itself and
  says how to turn scheduling on.
- **Fixed: session cleanup pointed at the wrong database.** `python -m
  crucible.sessions_cli` resolves the inventory with the library's own default
  filename, so against an impi deployment it opened a file nobody writes and
  reported an empty stand — including in the documented recovery from a picture
  the model backend refuses. There is now `impi sessions list|delete|purge-idle`
  on the engine's own database (and `--db` on the library's entry point), and the
  log line that names the recovery prints a command that runs as written.
- **Fixed: a paged task list could show a task twice or not at all.** Tasks were
  ordered by creation time alone, which has second resolution; tasks made in the
  same second came back in no fixed order.
- **Deleting a task now deletes its run history.** It was kept, but every reader
  reaches a run through its task, so the rows were unreachable as well as
  unbounded.
- **Fixed: a slash command, click or scheduled run made the gateway log a
  rejected reaction.** The engine marked the triggering "message" as being worked
  on, but a synthetic turn has no post behind it to react to.
- Smaller things: no redundant **Details** button inside a task's own detail
  view; `impi task status` prints times the way every other surface does;
  `impi task rm` with no terminal to ask on refuses instead of raising; `make
  run-bg` appends to the engine log instead of truncating the evidence for
  whatever prompted the restart.

## v0.8.0 — 2026-08-08

- **Agents can work on a schedule.** "Remind me in two hours", "every weekday at
  nine, go through my inbox": a task is a prompt plus a schedule, kept in the
  engine's database and run in the conversation it was created in. Write it as a
  delay (`in 2h`), a moment, an interval (`every 15m`) or a cron expression, in
  the zone you mean (`SCHEDULER_TIMEZONE`, or per task) — a cron keeps its
  wall-clock time across a daylight-saving change, an interval stays an absolute
  duration. A run is either an ordinary turn in that conversation, with its
  memory, or a fresh memoryless one whose answer the engine posts. Manage them in
  chat (the agent has `schedule_task` and friends, and answers with the next few
  fire times so a misunderstanding surfaces immediately), from `/tasks`, or with
  `impi task list|show|runs|add|rm|pause|resume|run-now|status`. See
  [docs/tasks.md](docs/tasks.md).
- **A run that doesn't happen says why.** Every occurrence leaves a row —
  `ok`, `missed`, `overlap`, `timeout`, `deadline`, `interrupted`, `no_agent` and
  the rest — with the reason in plain words, readable with `impi task runs`. If
  the engine was down at the due time the task catches up **once**, and only
  inside its grace window; later than that it is reported as missed and moves on.
  A failure is never silent and never announced twice: in the conversation the
  turn already posted about, the scheduler stays quiet. Five failures in a row
  pause a task and say so.
- **The scheduler can be asked whether it is alive.** It records a heartbeat at
  the end of every pass, so `impi task status`, `impi doctor` and the `/tasks`
  header can tell an idle ticker from a stopped one, and name what it wakes for
  next — the failure mode where a timer quietly dies and every task simply stops.
- **Fixed: a turn could wait forever for a runtime slot.** A semaphore permit is
  held for as long as its session lives, idle time included, and the wait had no
  bound — while the per-turn timeout only starts once the session exists. A turn
  with no free slot hung silently, never raising a timeout and never reaching the
  user's fallback message. It now gives up after two minutes and reports it.

## v0.7.1 — 2026-08-08

- **Fixed: `impi update` ended with a syntax error and could offer to roll back
  a successful update.** Two faults in the wrapper, both reported against 0.7.0:
  it replaced itself with a `cp` over the file bash was still reading, so the
  shell resumed at its old byte offset inside new content (`syntax error near
  unexpected token`, printed after the update had already succeeded, hiding the
  real exit code) — the new wrapper is now installed by rename, which leaves the
  running shell on its own inode. And every readiness check
  (`compose logs | grep -q "app built:"`) was unable to return success: `grep -q`
  stops at the first match, the compose process writing into the pipe dies of
  SIGPIPE, and `set -o pipefail` reports *that* as the pipeline's status. So
  `impi doctor` claimed the engine never started while it was running fine, and
  the update's wait loop ran its full 30 iterations before offering a rollback of
  a healthy engine.

## v0.7.0 — 2026-08-07

- **Agents receive files and photos.** Attach a screenshot in Mattermost or
  Slack (or send bytes inline over the ws gateway) and the engine downloads it,
  keeps it under `DATA_DIR/attachments/<agent>/<conversation>/`, and names it in
  the agent's prompt with its type, size and absolute path. Pictures also go to
  the model directly, so an agent with no tools at all can describe one;
  anything else the agent opens itself with `read`/`bash`. A message that is
  nothing but a photo is now a message — until now it reached the agent as empty
  text, and a file-only post vanished from replayed history entirely. Slack needs
  the `files:read` scope. Only real PNG/JPEG/GIF/WebP bytes are shown to the
  model — a file that merely claims to be an image travels as a path, because a
  picture the backend refuses would fail every later turn of that conversation,
  not just the one it arrived in. Limits and retention are configurable
  (`ATTACHMENT_MAX_MB`, `ATTACHMENT_RETENTION_DAYS`, `INLINE_IMAGE_MAX_MB`,
  `ATTACHMENTS_ENABLED`); see [docs/files.md](docs/files.md).
- **Agents can send files back.** The new `send_file` tool posts a file into the
  conversation the turn is running in — the chart it just drew, the document it
  assembled, a file it was sent earlier — with an optional caption. It reads
  only from the agent's profile directory, its own attachment directory and
  `/tmp`, so the engine's configuration stays out of reach. Slack needs the
  `files:write` scope; ws services receive a `file` frame.
- **Fixed: a tool added to a profile now works after `impi reload`.** The reload
  rewrote the agent's manifest, so the runtime offered the new tool, but the tool
  server kept gating on the allowlist computed at startup and answered every call
  with `403 forbidden` until a restart.

## v0.6.2 — 2026-08-06

- **Your own compose files now survive `impi update`.** Drop any `*.yaml` into
  `$IMPI_HOME/compose.d/` — a tunnel, a proxy, an extra volume — and it is
  merged after impi's own files on every call. Until now the only place to add
  one was `IMPI_COMPOSE_FILES` in `compose.env`, which the updater regenerates
  (it has to: a release may add an overlay), so the next update would have
  dropped it. That key is gone: `compose.env` records the *intent*
  (`IMPI_MM_MODE`, `IMPI_COMPOSE_ROOTLESS`) and the file list is derived from it,
  leaving nothing to rewrite. An existing installation is migrated on the next
  wrapper call, and any file it had added by hand is named with the command to
  move it. `impi doctor` lists the overlays it merged.

## v0.6.1 — 2026-08-05

- **Fixed: every dropdown crashed on Slack.** `block_id` is a property of a
  Block Kit *block*, and the renderer put it on the menu *element* — Slack
  rejected the whole message (`invalid additional property: block_id`), so
  `ask_user_select`, the people/channel pickers and the `/skills` card menus all
  failed on that gateway; buttons and modal forms were unaffected. The token now
  rides on the containing actions block, which Slack echoes back on the action,
  so nothing about decoding a click changed. Reported against 0.5.0.

## v0.6.0 — 2026-08-05

- **A shared skill library.** A skill no longer has to live inside one agent's
  profile: install it once into `SKILLS_PATH` — its own directory, ideally its
  own git repository — and give it to any agent with `registry:<name>` in that
  agent's `runtime.skills`. `impi skill list|show|install|update|remove|assign`
  manages it from the CLI; the support agent has the same operations as tools
  (`list_skills`, `install_skill`, `assign_skill`, `remove_skill`), so "write a
  skill and give it to the tutor" works from chat.
  Installing from a repository (`owner/repo[/path][@ref]`, or any git URL)
  **pins the exact commit** and lists every file, executables marked, before
  copying anything — a skill's scripts run inside the engine with the agent's
  tools. A skill that declares `requires_tools` is checked against the agent's
  allowlist, so it can't be assigned and then quietly do nothing. Assignments
  edit `agent.yaml` in place, keeping its comments. The `SKILL.md` format is the
  one Claude Code, Hermes and ClawHub share, so skills written for them install
  unchanged. New guide: docs/skills.md.
- **`/skills` browses the library in chat.** Each skill is a card of its own —
  name, version, what it does, who has it — with its controls beside it, and
  clicking one redraws that same message instead of starting an agent turn or
  posting a new one. It is the first of a kind of command the engine answers
  itself (`ScreenRegistry`), which also gives the chat clients two general
  verbs: post a message as cards, and rewrite one in place. Rename the command
  with `SKILLS_COMMAND` if your workspace already uses `/skills`.
- **A dropdown option now has a label of its own** (`Choice`), so a menu shows
  what a person should read while the value carries the machinery. Before this,
  any option whose value wasn't meant for human eyes was displayed raw.
- **`impi reload`** re-reads agent profiles in place (SIGHUP): no restart, and
  conversations in flight keep their memory. Assignments made from chat trigger
  it themselves.

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
