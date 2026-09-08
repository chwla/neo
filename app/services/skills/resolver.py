"""Which skills a turn may use, and the text that says so.

This module is the single answer to "is this skill on?". Nothing else combines
a skill's default with a chat's override -- if a second place ever computes it,
the two will disagree and the panel will stop describing what actually runs.

The shape mirrors ``disabled_tools``, deliberately: resolve once at the top of a
turn, snapshot onto the session, and enforce again at the point of use. A toggle
flipped while a run is in flight does not change that run; it changes the next.
"""

from __future__ import annotations

from pathlib import Path

from app.services.skills import library, store

#: How much skill text may be prepended for an engine that cannot fetch it on
#: demand. Deliberately a fraction of ``external_agents.context.MAX_PREAMBLE_CHARS``
#: (8000), which this text shares with the agent role and the conversation
#: history: the preamble is there to inform the run, not to become it, and a
#: skill that crowded out the history would be paying for itself twice.
MAX_PREPENDED_SKILL_CHARS = 5000


def is_enabled(skill: dict, overrides: dict | None) -> bool:
    """The one rule: the chat's override if it has one, else the skill's default."""

    value = (overrides or {}).get(skill["slug"])
    if value is None:
        return bool(skill["enabled_by_default"])
    return bool(value)


def catalog(overrides: dict | None) -> list[dict]:
    """Every installed skill, each told whether it is on for this chat.

    What the panel renders. Disabled skills are present here -- the user has to
    see a switch to flip it -- and absent from everything below.
    """

    return [{**skill, "enabled": is_enabled(skill, overrides)} for skill in store.list_skills()]


def effective_skills(overrides: dict | None) -> list[dict]:
    """Only the skills that are on, in the form a session snapshot keeps."""

    return [
        {
            "slug": skill["slug"],
            "name": skill["name"],
            "description": skill["description"],
            "directory": skill["directory"],
        }
        for skill in store.list_skills()
        if is_enabled(skill, overrides)
    ]


def roster_text(skills: list[dict]) -> str:
    """The always-present half: what exists, and when each one applies.

    Names and descriptions only. The body is what costs context, and a run that
    needs none of the skills should not pay for all of them.
    """

    if not skills:
        return ""
    lines = [f"- {skill['name']}: {skill['description']}" for skill in skills]
    return "\n".join(lines)


def body_for(slug: str, allowed: list[dict]) -> str:
    """One skill's instructions, refusing anything outside ``allowed``.

    ``allowed`` is the session's own snapshot, so this cannot be widened by a
    skill being enabled after the run started, and cannot be dodged by naming a
    skill the roster never mentioned.
    """

    for skill in allowed:
        if skill["slug"] == slug or skill["name"] == slug:
            return library.body_text(skill)
    raise KeyError(slug)


def prepended_text(skills: list[dict], *, budget: int = MAX_PREPENDED_SKILL_CHARS) -> str:
    """Roster and bodies together, for an engine that cannot ask for a body.

    Claude Code and Codex receive one prompt string and hand nothing back until
    they are done, so there is no ``load_skill`` round trip to make. The whole
    of every enabled skill goes in up front instead. That is a real difference
    from Neo's own loop and it is written down rather than hidden -- but the
    part that matters is the same either way: a skill that is off contributes
    nothing here.
    """

    if not skills:
        return ""
    parts = ["Skills available for this task:", roster_text(skills), ""]
    spent = 0
    for skill in skills:
        path = Path(skill["directory"]) / library.SKILL_FILE
        try:
            body = library.body_text(skill)
        except Exception:
            # A skill whose file has gone missing must not take the run with it.
            # It stays in the roster above, which is the honest thing to show.
            continue
        if spent + len(body) > budget:
            # Never a truncated body. Half a set of instructions is worse than
            # none -- it reads as complete and stops mid-rule. The file is on
            # the same machine the CLI is running on, so point at it and let the
            # engine read it with its own tools if it decides the skill applies.
            parts.append(
                f"--- {skill['name']} ---\n"
                f"Full instructions are in {path}. Read that file before using this skill."
            )
            continue
        spent += len(body)
        parts.append(f"--- {skill['name']} ---\n{body}")
    return "\n\n".join(part for part in parts if part)


__all__ = [
    "MAX_PREPENDED_SKILL_CHARS",
    "body_for",
    "catalog",
    "effective_skills",
    "is_enabled",
    "prepended_text",
    "roster_text",
]
