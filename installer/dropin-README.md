# Your compose overlays

Any `*.yaml` here is merged after impi's own compose files, in alphabetical
order, so it can add services (a tunnel, a proxy) or override the engine's.

This directory is yours: updates never read, rewrite or remove it. Use it
instead of editing files under `repo/` — those are replaced on every update.
