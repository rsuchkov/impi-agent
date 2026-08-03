# Changelog

Notable changes per release, newest first. The **Unreleased** section collects
what will ship next; `scripts/release.sh` stamps it with the version and date
when a release is cut, and `impi update` shows the target version's section.

## Unreleased

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
