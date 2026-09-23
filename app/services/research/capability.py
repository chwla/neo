"""What the model behind the research route can do, read from Neo's model registry.

Research never picks a provider or a model: it asks the canonical route the user has
configured what its limits are, then sizes its own requests to fit. Nothing here knows
any provider or model by name; a route that the registry cannot describe yields
conservative limits marked ``known=False`` so callers can say so in the trace.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Used only when the registry has no recorded window for the routed model.
FALLBACK_CONTEXT_WINDOW = 8192
#: Used only when neither route nor model records an output limit.
FALLBACK_MAX_OUTPUT = 1024
#: Roughly four characters per token; good enough to size a request, never to bill one.
CHARS_PER_TOKEN = 4
#: Room left for the prompt's own framing and for tokenizer estimate error.
CONTEXT_SAFETY_TOKENS = 512


@dataclass(frozen=True)
class ModelCapability:
    """Limits of the model serving a route, as the registry describes it."""

    context_window: int = FALLBACK_CONTEXT_WINDOW
    max_output_tokens: int = FALLBACK_MAX_OUTPUT
    supports_json: bool = False
    route_name: str = "research"
    provider: str = ""
    model: str = ""
    #: False when the registry could not be read, so the limits above are assumptions.
    known: bool = False

    def describe(self) -> str:
        where = f"{self.provider}/{self.model}" if self.model else self.route_name
        return (
            f"{where}: context {self.context_window} tokens, "
            f"output {self.max_output_tokens} tokens"
            + ("" if self.known else " (assumed; registry did not describe this route)")
        )


def resolve_capability(route_name: str = "research") -> ModelCapability:
    """Describe the model Neo's configured route points at.

    Reads the same route record the provider runtime uses to serve the request, so a user
    who repoints the route gets the new limits without any change here.
    """
    try:
        from app.services.provider_runtime.router import select

        route = select("research", route_name)
    except Exception:
        return ModelCapability(route_name=route_name)
    model = route.get("model_record") or {}
    record = route.get("route_record") or {}
    # A route may cap output below what the model could produce; the smaller wins.
    limits = [
        value
        for value in (record.get("max_output_tokens"), model.get("max_output_tokens"))
        if isinstance(value, int) and value > 0
    ]
    window = model.get("context_window")
    return ModelCapability(
        context_window=window
        if isinstance(window, int) and window > 0
        else FALLBACK_CONTEXT_WINDOW,
        max_output_tokens=min(limits) if limits else _assumed_output(window),
        supports_json=bool(model.get("supports_json")),
        route_name=str(route.get("route_name") or route_name),
        provider=str(route.get("provider") or ""),
        model=str(route.get("model") or ""),
        known=True,
    )


def _assumed_output(context_window: object) -> int:
    """An output ceiling for a model whose registry row records none.

    A model with a large window can almost certainly write more than the floor, so scale
    the assumption with the window rather than crippling a capable model on a missing field.
    """
    if not isinstance(context_window, int) or context_window <= 0:
        return FALLBACK_MAX_OUTPUT
    return max(FALLBACK_MAX_OUTPUT, min(4096, context_window // 8))


def output_budget(capability: ModelCapability, want: int, prompt_chars: int = 0) -> int:
    """The largest output this model can be asked for, given what the prompt already costs.

    Asking for more than the model allows is how research work gets silently cut off, and
    asking for more than the context window leaves is how a request gets refused outright.
    """
    room = capability.context_window - prompt_chars // CHARS_PER_TOKEN - CONTEXT_SAFETY_TOKENS
    return max(64, min(want, capability.max_output_tokens, max(64, room)))


def work_scale(capability: ModelCapability, *, reference_items: int, output_per_item: int) -> int:
    """How many times more calls this model needs to do the same work, 1 to 4.

    A model that can only describe two chunks per answer is not worse at research than one
    that can describe ten; it needs more turns to say the same thing. Scaling the call
    allowance keeps the evidence the depth asked for instead of quietly dropping it.
    """
    fits = items_per_call(
        capability, output_tokens_per_item=output_per_item, ceiling=max(1, reference_items)
    )
    return max(1, min(4, -(-reference_items // max(1, fits))))


def items_per_call(
    capability: ModelCapability,
    *,
    output_tokens_per_item: int,
    overhead_tokens: int = 200,
    input_chars_per_item: int = 0,
    ceiling: int = 64,
) -> int:
    """How many items of work fit in one call to this model, by output and by context.

    Splitting work to fit is what keeps a small model honest: more calls, same evidence,
    rather than one call whose tail is thrown away.
    """
    usable_output = max(64, capability.max_output_tokens - overhead_tokens)
    by_output = usable_output // max(1, output_tokens_per_item)
    by_context = ceiling
    if input_chars_per_item > 0:
        room = capability.context_window - capability.max_output_tokens - CONTEXT_SAFETY_TOKENS
        by_context = max(1, (room * CHARS_PER_TOKEN) // max(1, input_chars_per_item))
    return max(1, min(ceiling, by_output, by_context))
