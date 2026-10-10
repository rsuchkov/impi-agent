"""A model as configuration names it: ``provider/model``.

How an agent's fallback models are written — in ``agent.yaml``, in
``DEFAULT_FALLBACK_MODELS``, in ``AGENTS_FALLBACK_MODELS__<AGENT>`` — and
checked by one rule, because all three end in the same place: the chain the
runtime is handed. The model part may hold slashes of its own
(``openrouter/anthropic/claude``): the provider is everything before the first.

And the one rule for a provider and a model given apart: a provider needs a
model (a model alone is fine — the runtime finds its provider).
"""


def check_model_ref(value: str) -> str:
    """``value`` as a ``provider/model`` reference, stripped. ValueError for
    anything else."""
    ref = value.strip()
    provider, _, model = ref.partition("/")
    if not provider.strip() or not model.strip() or ref != ref.replace(" ", ""):
        raise ValueError(f"{value!r} is not a provider/model reference")
    return ref


def check_provider_has_model(provider: str, model: str, provider_key: str, model_key: str) -> None:
    """A provider with no model is refused by the runtime on every turn, so it
    is refused where it is configured, naming the keys to set. Blank is unset.
    ValueError."""
    if provider.strip() and not model.strip():
        raise ValueError(
            f"{provider_key} is set ({provider.strip()!r}) but {model_key} is not: the runtime "
            f"refuses a provider without a model — set {model_key}, or clear {provider_key} to "
            "let the runtime choose both"
        )
