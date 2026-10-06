"""The rule for what a runtime process may inherit, and the list of the
engine's own key families it protects, held in step with the settings."""

import re
from pathlib import Path

import pytest

from crucible.config import Settings
from crucible.ports.agent.inherited_env import RESERVED_PREFIXES, check_inherited_env

CONFIG = Path(__file__).resolve().parent.parent / "packages/crucible/src/crucible/config.py"


@pytest.mark.parametrize("pattern", ["BROWSER_CDP_URL", "MYSERVICE_*", "SLACK_BOT_TOKEN"])
def test_a_name_or_a_family_with_a_real_prefix_passes(pattern: str) -> None:
    check_inherited_env(pattern)


@pytest.mark.parametrize("pattern", ["*", "A*", "MY*", "a b", "X-Y", "AGENTS_MM*", "SLACK*", "TOOL_*"])
def test_too_broad_malformed_or_over_the_engines_own_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError, match=re.escape(repr(pattern))):
        check_inherited_env(pattern)


def _covered(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in RESERVED_PREFIXES)


def test_every_secret_the_settings_hold_is_in_a_reserved_family() -> None:
    """A settings field that carries a credential, read from the environment
    under its upper-cased name, must not be reachable by a pattern."""
    secretish = re.compile(r"token|secret|key|password")
    fields = [name.upper() for name in Settings.model_fields if secretish.search(name)]
    assert fields, "the settings name no credential at all?"
    assert [key for key in fields if not _covered(key)] == []


def test_every_per_key_family_the_settings_read_is_reserved() -> None:
    """The dynamic keys config.py builds (AGENTS_MM_TOKEN__<AGENT>,
    HTTP_CALLER_TOKEN__<NAME>, …) are families of their own."""
    families = set(re.findall(r'"([A-Z][A-Z0-9_]*__)', CONFIG.read_text(encoding="utf-8")))
    families -= {"RUNTIME_ENV__"}  # names, not secrets: what is handed over
    assert families
    assert sorted(f for f in families if not _covered(f)) == []
