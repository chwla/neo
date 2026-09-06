"""Asking a model to rate the answers, when there is no rule that could.

Off by default, and worth understanding why. A judging pass is another model call, so it
is another wait -- and unlike the answers it cannot be parallelised across contenders,
because the whole point is to see them side by side. On a custom prompt it is the only
thing that can produce a score at all; on a built-in pack the rules already have an
answer and the judge is a second opinion, not a replacement.

Three properties keep the opinion worth having.

The answers are anonymised and rotated before the judge sees them. Models show a position
bias -- the first option gets picked more often than it earns -- so the letter each answer
wears is derived from the task id rather than fixed, and the mapping is undone before
anything is shown.

The rubric is the user's when they wrote one. It replaces the default outright rather
than being appended to it: two sets of grading instructions in one prompt is not a
stricter rubric, it is a confused one.

And the judge's rating is never blended into the rule-based score. They are reported as
two numbers because they are two different kinds of claim, and averaging them would hide
which one is a checkable rule and which is a model's opinion.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from app.services.llm import LLMConfig, LLMMessage
from app.services.model_compare.grading import strip_fences
from app.services.model_compare.types import Comparison, JudgeSettings, Task, TaskOutcome

_LOG = logging.getLogger(__name__)

#: A rating pass reads several answers and writes a few numbers, so this is ample.
#: The judge is always built with thinking switched off -- rating is mechanical, and a
#: reasoning model left to think will spend the entire budget on it and return an empty
#: object. That is not hypothetical: it is what gemma4 did on the first run of this.
JUDGE_MAX_TOKENS = 400

#: One pass over one task's answers. Shorter than a task deadline because the judge is
#: reading rather than solving.
JUDGE_TIMEOUT_SECONDS = 60

#: The letters answers are shown under. Four is the most contenders a comparison takes.
LABELS = "ABCD"

JUDGE_SYSTEM = (
    "You are grading answers to the same question. Judge only how well each answer "
    "does what the question asked. Ignore length, style and formatting unless the "
    "question asked for them. Never reward an answer for agreeing with the others."
)

#: What the judge weighs when the user has not said. Deliberately plain and short: a
#: long default rubric would quietly become the thing being measured.
DEFAULT_RUBRIC = (
    "Judge these answers on:\n"
    "1. Factual correctness\n"
    "2. Completeness\n"
    "3. Clarity\n"
    "4. Conciseness"
)

#: The shape the reply has to come back in. Appended to whichever rubric is in force,
#: because it is a protocol rather than a criterion and a user rubric must not have to
#: restate it.
JUDGE_FORMAT = (
    'Rate each answer from 0 to 10. Reply with only a JSON object of the form '
    '{"ratings": {"A": 7, "B": 4}, "why": "one short sentence"}.'
)


def _order(task_id: str, count: int) -> list[int]:
    """Which answer wears which letter, rotated per task.

    Derived from the task id rather than randomised so that judging the same comparison
    twice gives the same arrangement, which is what makes a surprising rating something
    the user can go back and check.
    """

    shift = sum(ord(char) for char in task_id) % max(1, count)
    return [(index + shift) % count for index in range(count)]


def build_prompt(task: Task, answers: list[str], rubric: str = "") -> str:
    lines = [f"Question:\n{task.prompt}\n"]
    for label, answer in zip(LABELS, answers, strict=False):
        body = answer.strip() or "(no answer)"
        lines.append(f"Answer {label}:\n{body}\n")
    lines.append((rubric or DEFAULT_RUBRIC).strip())
    lines.append(JUDGE_FORMAT)
    return "\n".join(lines)


def parse_ratings(text: str, count: int) -> tuple[dict[str, float], str]:
    """Pull the ratings out of whatever came back. Returns ({letter: 0..1}, why)."""

    body = strip_fences(text or "")
    # Some models introduce the JSON despite being asked not to; take the object.
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return {}, ""
    try:
        payload = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}, ""
    raw = payload.get("ratings")
    if not isinstance(raw, dict):
        return {}, ""
    ratings: dict[str, float] = {}
    for label in LABELS[:count]:
        value = raw.get(label, raw.get(label.lower()))
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        ratings[label] = max(0.0, min(1.0, float(value) / 10.0))
    why = payload.get("why")
    return ratings, str(why).strip() if isinstance(why, str) else ""


def _rate_task(
    client: Any, task: Task, outcomes: list[TaskOutcome], rubric: str
) -> dict[str, tuple[float, str]]:
    """Rate one task's answers. Returns {contender_id: (0..1, note)}."""

    answerable = [item for item in outcomes if item.status == "ok"]
    if len(answerable) < 2:
        # Nothing to compare. A single answer rated against itself is not a comparison,
        # and spending a model call to say so would be the one thing this must not do.
        return {}

    arrangement = _order(task.id, len(answerable))
    shown = [answerable[index] for index in arrangement]
    prompt = build_prompt(task, [item.answer for item in shown], rubric)
    try:
        result = client.chat_with_metadata(
            [
                LLMMessage(role="system", content=JUDGE_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0,
        )
    except Exception as exc:
        _LOG.debug("Judging failed for %s: %s", task.id, exc)
        return {}

    ratings, why = parse_ratings(result.content or "", len(shown))
    return {
        outcome.contender_id: (ratings[label], why)
        for label, outcome in zip(LABELS, shown, strict=False)
        if label in ratings
    }


def pass_over(config: LLMConfig, settings: JudgeSettings):
    """A finalize hook for ``runner.run`` that rates every task before the run is summed.

    Returned as a closure rather than called directly so that the runner stays unaware of
    judging: it knows only that something may want the outcomes before they are totalled.
    """

    from app.services.model_compare.runner import build_client

    def finalize(comparison: Comparison, stop) -> Iterator[dict[str, Any]]:
        gradeable = [
            task
            for task in comparison.config.tasks
            if len([o for o in comparison.outcomes if o.task_id == task.id and o.status == "ok"])
            >= 2
        ]
        if not gradeable:
            comparison.errors.append(
                "There were not two answers to compare on any task, so nothing was rated."
            )
            yield {
                "type": "judge_failed",
                "judge": settings.display_name,
                "message": (
                    "Nothing could be rated: a task needs at least two answers to compare."
                ),
            }
            return

        yield {
            "type": "judging",
            "judge": settings.display_name,
            "tasks": len(gradeable),
        }
        # allow_thinking is left at its default of False. See JUDGE_MAX_TOKENS.
        client = build_client(config, max_tokens=JUDGE_MAX_TOKENS, timeout=JUDGE_TIMEOUT_SECONDS)
        rated = 0
        for task in gradeable:
            if stop.is_set():
                break
            outcomes = [item for item in comparison.outcomes if item.task_id == task.id]
            for outcome in outcomes:
                if outcome.status == "ok":
                    yield {
                        "type": "cell_state",
                        "contender_id": outcome.contender_id,
                        "task_id": task.id,
                        "state": "evaluating",
                    }
            verdicts = _rate_task(client, task, outcomes, settings.rubric)
            for index, outcome in enumerate(comparison.outcomes):
                if outcome.task_id != task.id or outcome.status != "ok":
                    continue
                found = verdicts.get(outcome.contender_id)
                if found is None:
                    # Put the cell back where it was; it simply has no rating.
                    yield {
                        "type": "cell_state",
                        "contender_id": outcome.contender_id,
                        "task_id": task.id,
                        "state": "complete",
                    }
                    continue
                score, note = found
                comparison.outcomes[index] = replace(
                    outcome, judge_score=score, judge_note=note, state="complete"
                )
                yield {
                    "type": "judged",
                    "contender_id": outcome.contender_id,
                    "task_id": task.id,
                    "judge_score": round(score, 3),
                    "judge_note": note,
                }
            if verdicts:
                rated += 1

        if rated:
            comparison.judged_by = settings.display_name
        else:
            # Said out loud rather than left blank. A screen that quietly shows no
            # ratings after the user asked for them looks like it forgot, and the user
            # has no way to tell that from a judge that answered in a shape Neo could
            # not read.
            message = (
                f"{settings.display_name} could not rate these. Its reply did not come "
                "back as ratings. The rule-based checks are unaffected."
            )
            comparison.errors.append(message)
            yield {
                "type": "judge_failed",
                "judge": settings.display_name,
                "message": message,
            }

    return finalize
