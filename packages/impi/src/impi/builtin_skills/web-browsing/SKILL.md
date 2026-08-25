---
name: web-browsing
description: "Read live web pages with a real Chrome this deployment runs — open a URL and read it, follow links, fill a form, take a screenshot. Use whenever the answer depends on something on the web right now: a page's contents, documentation, a search result, a site that needs JavaScript or a sign-in. Not for a plain JSON API — curl is smaller and better at that."
version: 0.1.0
requires_tools: [bash]
category: web
tags: [browser, web, chrome, page, form, screenshot]
---

# Reading the web with a real browser

There is a Chrome in this deployment, in a container of its own, and you drive
it with `playwright-cli` over `bash`. It renders JavaScript and holds a session,
so it reaches pages `curl` cannot.

Reference: `$IMPI_ROOT/docs/browsing.md` — the boundaries, and what to tell an
operator when something is wrong. `playwright-cli --help` is the full command
list; this skill is the operating procedure, not the manual.

## What you are actually holding

**It is not your browser. It is everybody's.** One Chrome serves every agent in
this deployment, so tabs, cookies and local storage are shared. Two consequences
you have to work with:

- Do not assume privacy. Anything you sign into, the next agent inherits.
- Do not leave a session signed in. Sign out when you are done, or say plainly
  in chat that you left one open and why.

The profile starts empty and belongs to nobody — it holds no operator session,
no saved password. If a page needs an account, that account has to come from
somewhere in this conversation, and the rules below apply.

## Attaching

```bash
playwright-cli attach --cdp="$BROWSER_CDP_URL"
```

`BROWSER_CDP_URL` is in your environment when this deployment has a browser. If
it is unset, there is no browser here — say so and stop; it is not something you
can fix or work around.

Detach when you are finished:

```bash
playwright-cli detach
```

`detach` leaves the shared browser running for everyone else. Do not use
`close`: it shuts the browser down under the other agents.

## The loop

```bash
playwright-cli goto https://example.com
playwright-cli snapshot
playwright-cli click f3e6
playwright-cli fill f3e4 "text"
playwright-cli press Enter
```

`snapshot` is the thing to lean on. It returns the page as an accessibility tree
where every element carries a ref (`f3e6`), and those refs are what `click`,
`fill`, `hover` and `select` take. **Use the ref from the snapshot. Never invent
a CSS selector** — a ref names something that is demonstrably on the page, a
guessed selector names something you hope is.

A snapshot lands in a file and the command prints its path; read the file — the
refs are in there, not in what the command printed. After anything that
navigates, take a new snapshot: the old refs are stale.

## Long pages

Read what you need and stop. Never paste a whole page into chat — summarise it
and quote the few lines that carry the answer. If the page is long, `snapshot`
with a narrower scope beats dumping everything.

## Forms and signing in

`fill` types into a field. Two rules, and they do not bend:

- **Never type a password that was handed to you in chat.** A chat message is
  not a credential store, and typing it into a shared browser leaves the session
  behind for the next agent.
- If this deployment runs the secret broker, that is the supported route —
  see the `ward` skill. If it does not, tell the operator that signing in is
  something they should do rather than something you should.

## Screenshots

```bash
playwright-cli screenshot
```

It writes a file and prints the path. To put it in chat, pass that absolute path
to `send_file` — but only if `send_file` is in your allowlist. It is not part of
this skill; if you do not have it, describe what you saw instead.

## What this cannot do

Say so plainly rather than trying and failing:

- **No downloads.** The browser's filesystem is read-only and its `/tmp` is not
  yours — a downloaded file exists only inside that container and you cannot
  read it.
- **No `file://`**, and no local paths. The browser sees the web, not this
  machine.
- **It cannot reach this deployment's own services.** The browser sits on a
  network of its own precisely so a web page cannot be used as a hop into the
  chat server or the secret store. `http://mattermost:8065` will not resolve
  from there, and that is deliberate, not a fault.

## Etiquette

One page at a time; do not crawl a site because a question was broad. If a page
asks who you are, say honestly that you are an automated agent — do not claim to
be a person.
