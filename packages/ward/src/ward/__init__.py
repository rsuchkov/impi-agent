"""ward — the thing standing between an agent and a credential.

A second application on `crucible`, deployed beside the secret store and
nowhere near an agent. It holds the store's credential, decides who may reach
what, asks a human when the policy says to, and hands the value to the process
that asked — never to the model.

Why it is not part of the engine: the engine and the agents' shells run as the
same user in the same container, so a credential the engine holds is a
credential an agent can eventually reach. Moving the read out is not enough on
its own — an engine that could mint its own approvals would still be able to
help itself silently — so the deciding moves with it. What is left for a
compromised engine is asking, which a human sees and the ledger records.

Everything that decides is here: the broker, the policy rules, the Vault
adapter, the door that identifies callers by their certificate, and the tiny
certificate authority that issues them. What the library keeps is the other end
of the wire — the ``secret-exec`` client an agent runs, and the vocabulary the
two agree on — because that half ships in the engine's image and this half must
not.

The name is the folklore one: a ward is the charm that keeps an imp from
crossing the threshold.
"""
