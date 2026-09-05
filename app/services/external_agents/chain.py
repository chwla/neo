"""Handing work from one executor to another inside a single Neo turn.

A chain is **one session with several steps**, never one session per step. That
is the whole design decision, and everything good about it follows: the chain
shares one anchor in the transcript, one lease, one heartbeat, one cancellation
path, one concurrency slot and one budget, and it produces exactly one
``run.completed``. Steps are visible only as ``step.started`` events, which is
enough for the transcript to draw "handed to Codex" between two stretches of
work.

The alternative -- a session per step -- would have meant several rows claiming
the same anchor, a cancel that had to find and stop the right one, and a chain
that could half-finish in a way nothing downstream models. None of that buys the
user anything.

Each step is told what the one before it did (see ``context``), and the
repository itself carries the rest: the second executor reads the files the first
one wrote.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.agent_core import events, store
from app.services.agent_core.workspace import repo_root
from app.services.external_agents import detect, service, snapshot
from app.services.external_agents.types import ExternalAgentError, HandoffStep, RunOutcome

_LOG = logging.getLogger(__name__)

_PLAN = (
    "You are the planning step. Investigate the repository and produce a concrete, "
    "specific implementation plan. Do not modify any files."
)
_BUILD = (
    "You are the implementation step. Carry out the plan above in this repository. "
    "Make the edits, run the tests, and report what you changed."
)
_REVIEW = (
    "You are the review step. Review the changes already made in this repository for "
    "correctness, security and clarity. Read the files and the git diff. Report findings; "
    "do not modify files."
)
_SECOND = (
    "You are giving an independent second opinion. Investigate the repository and answer "
    "on your own terms. Do not modify any files."
)

#: The shipped presets. No arbitrary chain editor in v1 -- these are the three
#: shapes that come up, and each is a claim about which engine does what.
PRESETS: dict[str, dict[str, Any]] = {
    "plan_build": {
        "name": "Plan → Build",
        "steps": [
            HandoffStep(executor="claude_code", mode="plan", role="Plan", instructions=_PLAN),
            HandoffStep(executor="codex", mode="auto", role="Build", instructions=_BUILD),
        ],
    },
    "build_review": {
        "name": "Build → Review",
        "steps": [
            HandoffStep(executor="codex", mode="auto", role="Build", instructions=_BUILD),
            HandoffStep(
                executor="claude_code", mode="plan", role="Review", instructions=_REVIEW
            ),
        ],
    },
    "second_opinion": {
        "name": "Second opinion",
        "steps": [
            HandoffStep(
                executor="claude_code", mode="plan", role="Claude Code", instructions=_SECOND
            ),
            HandoffStep(executor="codex", mode="plan", role="Codex", instructions=_SECOND),
        ],
    },
}


def preset_steps(name: str) -> list[HandoffStep]:
    preset = PRESETS.get(name)
    if not preset:
        raise ExternalAgentError(f"Unknown handoff preset '{name}'.")
    return [step.model_copy() for step in preset["steps"]]


def describe_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "name": value["name"],
            "steps": [
                {"executor": step.executor, "mode": step.mode, "role": step.role}
                for step in value["steps"]
            ],
        }
        for key, value in PRESETS.items()
    ]


def available(name: str) -> tuple[bool, str | None]:
    """Whether every executor a preset needs is usable right now."""

    try:
        steps = preset_steps(name)
    except ExternalAgentError as exc:
        return False, str(exc)
    for step in steps:
        row = detect.status(step.executor)
        if not row.get("available"):
            return False, f"{row.get('name', step.executor)}: {row.get('reason')}"
    return True, None


def run_chain(session_id: str, objective: str, steps: list[HandoffStep]) -> RunOutcome:
    """Run every step in order, threading each one's answer into the next."""

    if not steps:
        raise ExternalAgentError("A handoff needs at least one step.")

    session = store.get_session(session_id) or {}
    root = repo_root(session.get("repo_id"))
    # Captured once, before the first step: "what changed in this turn" means the
    # whole chain, so every step after the first sees the accumulated work.
    before = snapshot.capture(root)

    previous_answer: str | None = None
    previous_name: str | None = None
    last = RunOutcome()

    for index, step in enumerate(steps):
        if (store.get_session(session_id) or {}).get("status") == "cancelled":
            last.cancelled = True
            last.outcome = "failed"
            last.error = "cancelled"
            return last

        spec = detect.spec(step.executor)
        label = spec.name if spec else step.executor
        store.update_session(
            session_id,
            {"handoff": {**(session.get("handoff") or {}), "step": index, "total": len(steps)}},
        )
        store.append_event(
            session_id,
            service.STEP_STARTED,
            {
                "executor": step.executor,
                "name": label,
                "role": step.role,
                "index": index,
                "total": len(steps),
            },
        )

        last = service.run_step(
            session_id,
            executor=step.executor,
            objective=objective,
            mode=step.mode,
            instructions=step.instructions,
            previous_answer=previous_answer,
            previous_executor_name=previous_name,
            pre_state=before,
        )

        if last.outcome == "failed":
            # A failed step ends the chain: the next one would be reasoning about
            # work that did not happen.
            store.append_event(
                session_id,
                events.STATUS,
                {"content": f"{label} failed; the handoff stopped here."},
            )
            return last

        previous_answer = last.final_text or previous_answer
        previous_name = label

    return last


__all__ = ["PRESETS", "available", "describe_presets", "preset_steps", "run_chain"]
