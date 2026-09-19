"""Where an impi deployment's agents come from, and what pi is told about the
model — shared by the engine's composition root and the CLI, so a command that
reasons about an agent sees the same profiles, defaults and env the engine
would give it.
"""

from __future__ import annotations

from pathlib import Path

from crucible.profiles import FsProfileStore
from crucible.skills import SkillLibrary
from impi.config import ImpiSettings

# Engine-owned agent profiles (e.g. `support`) ship WITH impi, under the package.
BUILTIN_AGENTS_PATH = Path(__file__).parent / "builtin_agents"
# The engine's own checkout root (packages/impi/src/impi/profiles.py -> repo root):
# forwarded to engine agents so support can read the engine source/docs to diagnose.
IMPI_ROOT = Path(__file__).resolve().parents[4]


def build_pi_env(settings: ImpiSettings) -> dict[str, str]:
    """Env forwarded into every pi subprocess.

    Empty when the ChatGPT subscription is used — pi then authenticates via its
    own OAuth store; LLM_* only feed the optional custom provider extension.
    """
    env: dict[str, str] = {}
    if settings.llm_base_url:
        env["LLM_BASE_URL"] = settings.llm_base_url
    if settings.llm_api_key:
        env["LLM_API_KEY"] = settings.llm_api_key
    if settings.llm_model:
        env["LLM_MODEL"] = settings.llm_model
    if not settings.llm_verify_ssl:
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


def open_profile_stores(
    settings: ImpiSettings, library: SkillLibrary, *, agents_path: str | None = None
) -> tuple[FsProfileStore, FsProfileStore]:
    """The user's agents and the engine's own, as two stores.

    Engine-owned agents (support) are always enumerated (not subject to the user
    AGENTS_ENABLED list); the token gate still skips them without a token. Their
    provider/model override the global default so a public checkout still runs.
    """
    user_store = FsProfileStore(
        agents_path or settings.agents_path,
        default_timeout=settings.pi_timeout,
        default_provider=settings.default_provider,
        default_model=settings.default_model,
        skills_override=settings.skills_for,
        library=library.path_if_present,
    )
    engine_store = FsProfileStore(
        str(BUILTIN_AGENTS_PATH),
        default_timeout=settings.pi_timeout,
        default_provider=settings.support_provider or settings.default_provider,
        default_model=settings.support_model or settings.default_model,
        skills_override=settings.skills_for,
        library=library.path_if_present,
    )
    return user_store, engine_store
