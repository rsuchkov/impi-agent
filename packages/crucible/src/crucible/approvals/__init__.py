"""Asking a human to authorize something, and remembering that they did.

A general primitive, not a secrets one. Two things in the engine already want
the same shape — a credential an agent asks for, and a tool call the runtime
gates on — and they differ only in who the question is addressed to and what a
"yes" leaves behind.

The rendering half lives here too, and it is the security-critical half: what a
card says is the only thing a human has to go on, and everything interesting in
it comes from the caller.
"""

from crucible.approvals.card import (
    code_block,
    code_span,
    command_line,
    one_line,
    render_card,
)

__all__ = ["code_block", "code_span", "command_line", "one_line", "render_card"]
