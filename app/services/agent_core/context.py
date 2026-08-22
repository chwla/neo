"""Keeping the transcript inside the model's context window.

The previous design concatenated everything it could find and cut the result at
15,000 characters, which silently dropped whichever sources happened to be last.
This compacts instead: the system prompt, the objective and the most recent
exchanges are always kept intact, and older *tool output* is what gets shortened,
because that is both the bulkiest part of a transcript and the part whose detail
matters least once it has been acted on.
"""

from __future__ import annotations

from app.services.llm import LLMMessage

#: Rough characters-per-token. Deliberately conservative: overestimating tokens
#: compacts a little early, while underestimating overflows the window and the
#: provider silently truncates the *oldest* messages, including the system prompt.
CHARS_PER_TOKEN = 3.5

#: Recent turns are never compacted -- they are what the model is reasoning about.
KEEP_RECENT = 8

COMPACTED_TOOL_LIMIT = 400


def estimate_tokens(messages: list[LLMMessage]) -> int:
    total = 0
    for message in messages:
        total += len(message.content or "")
        for call in message.tool_calls or []:
            total += len(str(call))
    return int(total / CHARS_PER_TOKEN) + 4 * len(messages)


def _compact(message: LLMMessage) -> LLMMessage:
    content = message.content or ""
    if len(content) <= COMPACTED_TOOL_LIMIT:
        return message
    head = content[: COMPACTED_TOOL_LIMIT // 2]
    tail = content[-COMPACTED_TOOL_LIMIT // 4 :]
    return message.model_copy(
        update={
            "content": f"{head}\n... [{len(content)} characters, compacted] ...\n{tail}",
        }
    )


def fit(messages: list[LLMMessage], budget_tokens: int) -> list[LLMMessage]:
    """Return a transcript that fits, preferring to lose detail over losing turns.

    Dropping whole messages would break the assistant/tool pairing that providers
    require, so nothing is removed -- older tool results are shortened until the
    estimate fits, and only then does the oldest history go.
    """

    if budget_tokens <= 0 or estimate_tokens(messages) <= budget_tokens:
        return messages

    head = messages[:1]  # the system prompt is never touched
    body = messages[1:]
    if len(body) <= KEEP_RECENT:
        return messages

    older, recent = body[:-KEEP_RECENT], body[-KEEP_RECENT:]
    compacted = [_compact(message) if message.role == "tool" else message for message in older]
    result = [*head, *compacted, *recent]
    if estimate_tokens(result) <= budget_tokens:
        return result

    # Still too large: drop the oldest exchanges, always in assistant+tool pairs so
    # a tool result is never left without the call that produced it.
    while compacted and estimate_tokens([*head, *compacted, *recent]) > budget_tokens:
        compacted.pop(0)
        while compacted and compacted[0].role == "tool":
            compacted.pop(0)

    dropped = len(older) - len(compacted)
    notice = (
        [LLMMessage(role="user", content=f"[{dropped} earlier steps were dropped to fit context.]")]
        if dropped
        else []
    )
    result = [*head, *notice, *compacted, *recent]
    if estimate_tokens(result) <= budget_tokens:
        return result

    # Last resort: a handful of very large recent tool results can exceed the
    # budget on their own. Compacting them is worse than keeping them whole, but
    # it is far better than overflowing -- an overflow makes the provider drop the
    # system prompt, which loses the objective and the safety rules entirely.
    recent = [_compact(message) if message.role == "tool" else message for message in recent]
    return [*head, *notice, *compacted, *recent]


def context_budget(context_window: int | None, fraction: float) -> int:
    """Leave room for the reply: the window has to hold the answer too."""

    if not context_window or context_window <= 0:
        return 0
    return int(context_window * fraction)
