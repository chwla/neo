from __future__ import annotations

# Only used when a model row has no recorded context window. Discovery reads the real
# value from the provider, so this is the floor for a model Neo has never inspected.
DEFAULT_CONTEXT_WINDOW = 8192

# Context sizes requested from the provider. A local runtime reloads the model whenever
# the requested context changes, so growing in fixed steps keeps consecutive messages on
# one loaded instance instead of paying a reload for every fluctuation in prompt length.
CONTEXT_STEPS = (4096, 8192, 16384, 32768, 65536, 131072, 262144)


def _message_content(item: object) -> str:
    """Read a message body from either a mapping or an LLMMessage-style object.

    Reading the attribute first and only then falling back to ``.get`` breaks on an
    object whose content is empty: the falsy value sends it down the mapping branch,
    which raises AttributeError. Empty turns are legitimate, so branch on the shape.
    """
    if isinstance(item, dict):
        return str(item.get("content") or "")
    return str(getattr(item, "content", "") or "")


def estimate_tokens(messages: list[dict] | list, completion_tokens: int | None = None) -> dict:
    chars = sum(len(_message_content(item)) for item in messages)
    prompt = max(1, (chars + 3) // 4)
    completion = completion_tokens or min(1200, max(64, prompt // 2))
    return {
        "prompt_tokens_estimate": prompt,
        "completion_tokens_estimate": completion,
        "total_tokens_estimate": prompt + completion,
    }


def fit_context(required_tokens: int, limit: int) -> int:
    """Smallest step that holds the request, never above what the model supports."""
    for step in CONTEXT_STEPS:
        if step >= required_tokens:
            return min(step, limit)
    return limit


def context_check(
    estimate: dict, context_window: int | None, max_output_tokens: int | None
) -> dict:
    limit = context_window or DEFAULT_CONTEXT_WINDOW
    output = max_output_tokens or estimate["completion_tokens_estimate"]
    required = estimate["prompt_tokens_estimate"] + output
    exceeds = required > limit
    return {
        "context_window": limit,
        "required_tokens": required,
        "prompt_tokens": estimate["prompt_tokens_estimate"],
        "output_tokens": output,
        # What the provider is asked to allocate. Sending this instead of relying on the
        # provider default is what stops a long prompt being silently truncated to fit.
        "num_ctx": fit_context(required, limit),
        "exceeds": exceeds,
        "suggestion": "Shorten the message or split it across turns." if exceeds else None,
    }
