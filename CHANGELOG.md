# Changelog

Notable changes per release, newest first. The **Unreleased** section collects
what will ship next; `scripts/release.sh` stamps it with the version and date
when a release is cut, and `impi update` shows the target version's section.

## Unreleased

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
