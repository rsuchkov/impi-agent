"""wardline — everything that talks to the secret broker, and nothing that is it.

Two programs and a vocabulary:

* ``secret-exec`` — what an agent runs. Names the secrets it wants and the
  command it wants to run; on an approval the values are put into that command's
  environment and never anywhere else.
* ``ward-admin`` — what an operator runs. Stores values, writes policies, reads
  the ledger. A different certificate, and routes an agent cannot reach.
* ``wire`` — how a secret is named, the two words a caller may be told, and the
  words a policy uses. The broker imports this and nothing else here.

Why its own package: the engine is not part of this. Secrets are a tool agents
use, so the tool ships beside them — in the same image, on their PATH — while
the engine that spawns them stays unaware of it. This package therefore imports
nothing from the rest of the workspace, and an import-linter contract keeps it
that way.
"""
