# Skills

A **skill** is a folder with a `SKILL.md` that teaches an agent to do something:
the front matter says what it is, the body says how, and any scripts beside it
are the agent's to run. Skills are shared — installed once into a library, then
given to whichever agents should have them.

Two places a skill can live:

- **the library** (`SKILLS_PATH`) — shared, referenced as `registry:<name>`;
- **an agent's own profile** (`<profile>/.pi/skills/<name>`) — private to it,
  referenced by its bare name.

Nothing is ambient: an agent gets exactly the skills its profile lists.

## The library

One directory, one skill per subdirectory — ideally its own git repository, so
the code of everything you installed from elsewhere is reviewable in a diff:

```
$SKILLS_PATH/
  greek-tutor/SKILL.md          # the skill
  greek-tutor/scripts/drill.sh  # what it runs
  greek-tutor/.skill-source.json # where it came from, at which commit
```

**The directory name is the skill's identity** — that is what an agent
references. A `name:` in the front matter that disagrees is ignored (a skill
installed under a different name must not answer to two).

## Installing

```bash
impi skill install ./my-skill                       # a directory
impi skill install anthropics/skills/pdf@v2         # owner/repo[/path][@ref]
impi skill install https://git.example.com/s.git#skills/greek@main
impi skill list                                     # what you have, and who uses it
impi skill show greek-tutor
impi skill update greek-tutor                       # re-fetch from its source
impi skill remove greek-tutor
```

Before anything is copied you see **every file, its size, and which are
executable**, and you confirm. That is the trust model, and it is not
ceremonial: a skill's scripts run inside the engine with the agent's tools, so
installing one from the internet is running someone else's code on your machine.
A git install is pinned to the exact commit it came from, recorded in
`.skill-source.json`; `update` shows the old and new commit before replacing
anything.

Only public repositories are supported — no credentials are passed to git.

## Giving a skill to an agent

```bash
impi skill assign greek-tutor greek-teacher
impi skill assign greek-tutor greek-teacher --remove
```

This edits that agent's `agent.yaml` (comments and layout survive):

```yaml
runtime:
  skills:
    - vocabulary-trainer        # its own, private
    - registry:greek-tutor      # from the library
```

The profile is the single source of truth — **removing the line is how you turn
a skill off**; there is no separate enabled/disabled state to drift out of sync.
An agent picks up the change on its next turn after a reload (`impi reload`; the
chat paths reload for you). A conversation already in flight keeps its current
configuration until its session is reset — its memory is not lost.

> `AGENTS_SKILLS__<AGENT>` in `.env` **replaces** an agent's whole list. It is an
> escape hatch; while it is set, assignments won't show up.

## From chat

`/skills` opens a browser for the library: page through it, open a skill, and
give it to an agent — every click redraws the same message, and no agent turn is
involved (the engine answers it directly, so it is instant and can't invent a
skill that isn't there).

Register it like any other command, pointing at
`<INTEGRATIONS_PUBLIC_URL>/command/<agent>` (see [commands.md](commands.md)),
with one rule: **the trigger word must match `SKILLS_COMMAND`** (default
`skills`). That word is what binds the command to this screen; registered
under any other word it is an ordinary command and goes to the agent instead.
Change `SKILLS_COMMAND` if your workspace already uses `/skills` for
something else.

The **support agent** does the other half: ask it to write a skill and install
it, and it will (`list_skills`, `install_skill`, `assign_skill`,
`remove_skill` — installing asks you to confirm in chat first).

## `requires_tools`

A skill declares what it needs:

```markdown
---
name: greek-tutor
description: Drills Greek vocabulary with spaced repetition
version: 1.2.0
requires_tools: [read, bash]
---
```

Any skill that runs scripts needs `read` + `bash` in the **agent's**
`runtime.tools` allowlist. Assigning tells you when they're missing — without
that check a skill installs cleanly and then quietly does nothing.

## Skills written for other tools

The `SKILL.md` format is shared with Claude Code, Hermes and ClawHub, so a skill
from any of them installs here unchanged; unknown front-matter keys (theirs) are
kept and ignored. What does **not** carry over is everything else a Claude
*plugin* may bundle — commands, hooks, agents, MCP servers — those have no
meaning in impi. Install the skill directory itself:

```bash
impi skill install anthropics/skills/document-skills/pdf
```

Publishing works in the other direction too: a repository of skills with a
`.claude-plugin/marketplace.json` is installable by Claude Code, and by
`impi skill install` regardless.
