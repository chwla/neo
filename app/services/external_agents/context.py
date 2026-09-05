"""What an external CLI is told before it starts working.

The requirement is that a user never has to re-explain what the previous
executor did. The temptation is to solve that by pasting the whole Neo
transcript into every invocation, which is wrong twice over: it is expensive,
and it is redundant with the thing the CLI is best at -- reading the repository
itself. The live filesystem and git state are the source of truth for code, so
what gets sent is *orientation*, not content:

* the objective,
* a bounded tail of the conversation,
* what the previous executor said it did,
* which files have changed so far in this turn,
* and, on a resume, only what happened outside that CLI's own view.

That last point is the subtle one. When Claude Code resumes its own session it
already remembers everything it did -- repeating it is noise. What it cannot know
is that Codex edited eleven files in between. So a resumed run is told about the
gap and nothing else.
"""

from __future__ import annotations

from typing import Any

#: How many earlier chat messages to carry. Enough for the thread of a
#: conversation, short of pasting a day's work into an argv.
MAX_HISTORY_MESSAGES = 8

#: Per-message ceiling. A long pasted stack trace earlier in the chat should not
#: crowd out the actual instruction.
MAX_MESSAGE_CHARS = 1200

#: Total ceiling on the preamble.
MAX_PREAMBLE_CHARS = 8000


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[... {len(text) - limit} more characters]"


def history_lines(history: list[dict[str, Any]]) -> list[str]:
    """The tail of the conversation, as plain speaker-labelled lines."""

    lines: list[str] = []
    for row in history[-MAX_HISTORY_MESSAGES:]:
        role = str(row.get("role") or "")
        content = _clip(str(row.get("content") or ""), MAX_MESSAGE_CHARS)
        if not content:
            continue
        speaker = {"user": "User", "assistant": "Assistant"}.get(role)
        if speaker:
            lines.append(f"{speaker}: {content}")
    return lines


def build_prompt(
    objective: str,
    *,
    history: list[dict[str, Any]] | None = None,
    previous_answer: str | None = None,
    previous_executor_name: str | None = None,
    change_summary: str | None = None,
    instructions: str | None = None,
    resuming: bool = False,
) -> str:
    """The prompt text for one external step.

    ``resuming`` means this CLI is continuing its *own* conversation, so its
    history is already in its context and only outside events are worth stating.
    """

    sections: list[str] = []

    if instructions:
        sections.append(instructions.strip())

    if not resuming and history:
        lines = history_lines(history)
        if lines:
            sections.append(
                "Earlier in this conversation:\n" + "\n".join(lines)
            )

    if previous_answer:
        who = previous_executor_name or "The previous agent"
        sections.append(
            f"{who} worked on this before you and reported:\n"
            f"{_clip(previous_answer, 4000)}"
        )

    if change_summary:
        header = (
            "Files changed since you last worked in this repository"
            if resuming
            else "Files already changed in this turn"
        )
        sections.append(
            f"{header} (read them from disk -- they are the source of truth):\n{change_summary}"
        )

    sections.append(f"Your task:\n{objective.strip()}")

    return _clip("\n\n---\n\n".join(section for section in sections if section.strip()),
                 MAX_PREAMBLE_CHARS)


__all__ = ["MAX_HISTORY_MESSAGES", "build_prompt", "history_lines"]
