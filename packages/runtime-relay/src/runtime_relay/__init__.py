"""runtime-relay: the front door of an agent's own container.

The engine no longer starts an agent's runtime as a child of itself. It asks
this program, over a socket only the engine can reach, and this program starts
it here — with this container's dependencies, this agent's profile, this agent's
session files and this agent's credentials, and nobody else's.
"""
