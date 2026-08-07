# Files and photos

Someone drops a screenshot into the chat and asks "what's wrong here?"; someone
forwards a PDF and asks for a summary. The engine downloads what was attached,
saves it where the agent can read it, and — for pictures — shows it to the model
directly.

## What the agent gets

Every attached file is downloaded before the turn starts and named in the
prompt, right under the message it came with:

```
[@roman · 2026-08-07 10:12 UTC]: what's wrong here?
[attached] screen.png — image/png, 214 KB — /app/data/attachments/assistant/dm1x…/f7k…-screen.png
```

The path is absolute and readable by the agent's own tools, so anything the
runtime can't look at directly is still fully available:

- **Pictures** additionally travel *into the model's view of the turn*, so the
  agent sees the image itself and can describe it. Give the agent no tools at
  all and this still works.
- **Everything else** — a PDF, a CSV, an archive — is a path. The agent needs
  `read` (text-ish files) and/or `bash` (everything else: `pdftotext`,
  `unzip`, a script of its own) in its `runtime.tools`, otherwise it can only
  see that a file arrived, not what is in it. A skill is the usual place to put
  a conversion recipe — see [skills.md](skills.md).

There is no automatic text extraction: what a PDF means depends on why you sent
it, and the agent is better placed to decide than a fixed pipeline.

## Where the files live

```
$DATA_DIR/attachments/<agent>/<conversation>/<file id>-<name>
```

In a containerized deployment `DATA_DIR` is `/app/data`, i.e. the `impi-data`
volume — attachments survive `impi update` and a rollback like the rest of the
engine's state. Per agent, per conversation: an agent can list what a thread has
accumulated, and no agent can reach another's files.

Files are swept after `ATTACHMENT_RETENTION_DAYS` (default 14; `0` keeps them
forever). A message redelivered by the platform — a reconnect, a retry — writes
the same path again rather than a second copy, because the platform's own file
id names the file.

## Sending a file to the user

Not yet — the outbound half (an agent posting a file back) is the next step.

## Per platform

| | Mattermost | Slack | ws |
|---|---|---|---|
| Incoming files | native | needs the `files:read` scope | inline in the frame |
| Message with no text | delivered (a photo alone is a message) | same | same |

**Slack** attaches files to a `file_share` message; the bot downloads them with
its own token, which requires the **`files:read`** scope on the app (add it in
*OAuth & Permissions* and reinstall the app — without it Slack answers the
download with its sign-in page, and the engine logs exactly that).

**ws** services send bytes inline, base64-encoded, since a service may share no
filesystem with the engine:

```jsonc
{"type": "message", "agent": "helper", "conversation_id": "user-42",
 "text": "",                                    // a caption is optional here
 "files": [{"name": "photo.jpg", "mime": "image/jpeg", "data": "<base64>"}]}
```

See [ws-gateway.md](ws-gateway.md) for the rest of the protocol.

## Limits

| Knob | Default | What it does |
|---|---|---|
| `ATTACHMENTS_ENABLED` | `true` | off = attachments are ignored entirely |
| `ATTACHMENT_MAX_MB` | `20` | per file; a bigger one is skipped (logged), the message still arrives |
| `ATTACHMENT_RETENTION_DAYS` | `14` | `0` = keep forever |
| `INLINE_IMAGE_MAX_MB` | `4` | above this a picture is not shown to the model, only named by path |
| `ATTACHMENTS_DIR` | `{DATA_DIR}/attachments` | where they land |

At most 4 pictures per turn are shown inline; the rest are named by path.
The inline cap is deliberately below the per-file cap: model backends reject
large images, and a 12-megapixel phone photo is well past what any of them will
take.

A file is only shown to the model if its **bytes** really are a PNG, JPEG, GIF or
WebP — a `.png` that is something else (a corrupt download, a renamed file, an
iPhone HEIC) is passed by path instead. That check matters more than it looks:
the runtime session replays its history on every later turn, so a picture the
backend refuses would keep failing the conversation, not just the turn it
arrived in. If a backend ever refuses one anyway, the engine logs the failure
along with the command that resets that conversation — see
[troubleshooting.md](troubleshooting.md).

A failure with one file never costs the user their message — the download error
is logged and the turn runs with whatever else arrived.
