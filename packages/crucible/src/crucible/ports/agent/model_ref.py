"""A model as configuration names it: ``provider/model``.

How an agent's fallback models are written — in ``agent.yaml``, in
``DEFAULT_FALLBACK_MODELS``, in ``AGENTS_FALLBACK_MODELS__<AGENT>`` — and
checked by one rule, because all three end in the same place: the chain the
runtime is handed. The model part may hold slashes of its own
(``openrouter/anthropic/claude``): the provider is everything before the first.
"""


def check_model_ref(value: str) -> str:
    """``value`` as a ``provider/model`` reference, stripped. ValueError for
    anything else."""
    ref = value.strip()
    provider, _, model = ref.partition("/")
    if not provider.strip() or not model.strip() or ref != ref.replace(" ", ""):
        raise ValueError(f"{value!r} is not a provider/model reference")
    return ref
