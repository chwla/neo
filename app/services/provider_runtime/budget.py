from __future__ import annotations


def estimate_tokens(messages: list[dict] | list, completion_tokens: int | None = None) -> dict:
    chars = sum(
        len(str(getattr(item, "content", None) or item.get("content", ""))) for item in messages
    )
    prompt = max(1, (chars + 3) // 4)
    completion = completion_tokens or min(1200, max(64, prompt // 2))
    return {
        "prompt_tokens_estimate": prompt,
        "completion_tokens_estimate": completion,
        "total_tokens_estimate": prompt + completion,
    }


def context_check(
    estimate: dict, context_window: int | None, max_output_tokens: int | None
) -> dict:
    limit = context_window or 8192
    output = max_output_tokens or estimate["completion_tokens_estimate"]
    exceeds = estimate["prompt_tokens_estimate"] + output > limit
    return {
        "context_window": limit,
        "exceeds": exceeds,
        "suggestion": "Compact non-safety context before retrying." if exceeds else None,
    }
