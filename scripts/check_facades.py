#!/usr/bin/env python3
"""No module that is only imports.

A module whose body is nothing but ``import`` statements (plus a docstring and
an ``__all__``) is a second name for things that live elsewhere. It looks like a
place where something is defined, so readers go there and find a signpost; it
lets two import paths drift apart in meaning; and once it exists it is kept out
of politeness long after the reason for it is gone. The one legitimate facade is
a package's ``__init__.py``, whose whole job is to name the package's public
surface — everything else should either define something or not exist.

Run by ``make lint`` over every package in the workspace. A finding prints the
file; if a facade is genuinely needed for a while (a rename in flight, say), it
goes in ALLOWED below with the reason, where it stays visible.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# "<path relative to ROOT>" -> the reason a reader deserves. Empty, and worth
# keeping that way.
ALLOWED: dict[str, str] = {}


def is_facade(tree: ast.Module) -> bool:
    """True when every statement is an import, the module docstring, or ``__all__``."""
    body = list(tree.body)
    if not body:
        return False  # an empty module is a different smell, not this one
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    saw_import = False
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            saw_import = True
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if not (isinstance(target, ast.Name) and target.id == "__all__"):
                return False
        else:
            return False
    return saw_import


def main() -> int:
    findings = []
    for path in sorted((ROOT / "packages").glob("*/src/**/*.py")):
        if path.name == "__init__.py":
            continue
        rel = str(path.relative_to(ROOT))
        if rel in ALLOWED:
            continue
        if is_facade(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
            findings.append(rel)
    for rel in findings:
        print(f"{rel}: a module of nothing but imports — define it here or import from where it lives")
    if findings:
        return 1
    print(f"check-facades OK ({len(ALLOWED)} allowlisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
