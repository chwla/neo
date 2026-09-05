"""External coding CLIs (Claude Code, Codex) driven as Neo agent executors.

Entry point: :func:`run`, called by ``agent_core.worker`` in place of
``AgentLoop.run`` when a session names an external executor. Everything the
worker provides -- the lease, the heartbeat, cancellation, turning an exception
into ``run.failed`` -- applies unchanged, so an external run has exactly the
lifecycle a Neo run does.
"""

from __future__ import annotations

import logging

from app.services.agent_core import events, store
from app.services.agent_core.types import status_for_stop_reason
from app.services.external_agents.chain import preset_steps, run_chain
from app.services.external_agents.service import STEP_STARTED, run_step
from app.services.external_agents.types import ExternalAgentError, HandoffStep, RunOutcome

_LOG = logging.getLogger(__name__)


def run(session_id: str) -> None:
    """Drive an external session to a terminal state.

    Mirrors what ``AgentLoop.run`` does at the end of a run -- set the stop
    reason, write the summary, backfill the anchor, emit the terminal event --
    because the transcript, the sidebar and the next turn all read those and
    know nothing about who did the work.
    """

    row = store.get_session(session_id)
    if row is None:
        raise LookupError("Agent session not found.")
    if row["status"] in {"completed", "failed", "cancelled"}:
        return

    store.update_session(session_id, {"status": "running", "started_at": store.now_iso()})
    store.append_event(session_id, events.RUN_STARTED, {"objective": row["objective"]})

    objective = row["objective"]
    handoff = row.get("handoff") or {}
    preset = handoff.get("preset")

    try:
        if preset:
            outcome = run_chain(session_id, objective, preset_steps(preset))
        else:
            outcome = run_step(session_id, executor=row["executor"], objective=objective)
    except ExternalAgentError as exc:
        # A refusal we understand: the feature is off, the CLI is missing or
        # signed out, or the workspace is not a git repository. These are the
        # user's to fix, so the message is the message.
        _finish(session_id, "failed", str(exc), error=str(exc))
        return
    except Exception as exc:  # pragma: no cover - defensive; the worker also traps
        _LOG.warning("external_agent_run_failed session=%s", session_id, exc_info=exc)
        _finish(session_id, "failed", f"The run failed: {exc}", error=str(exc))
        return

    _finish_from(session_id, outcome)


def _finish_from(session_id: str, outcome: RunOutcome) -> None:
    if outcome.cancelled or (store.get_session(session_id) or {}).get("status") == "cancelled":
        _finish(session_id, "cancelled", "The run was cancelled.")
        return
    if outcome.outcome == "completed":
        # `unverified_complete`, not `verified_complete`: the CLI reported it
        # finished, and Neo did not adjudicate that against evidence the way its
        # own loop does. Claiming the stronger status would overstate what is
        # known -- the distinction exists precisely so "it stopped talking" is
        # never recorded as "it succeeded".
        summary = outcome.final_text or "The run finished."
        _finish(session_id, "unverified_complete", summary)
        return
    reason = outcome.error or "The run failed."
    _finish(session_id, "failed", reason, error=reason)


def _finish(session_id: str, stop_reason: str, summary: str, *, error: str | None = None) -> None:
    status = status_for_stop_reason(stop_reason)
    store.update_session(
        session_id,
        {
            "status": status,
            "stop_reason": stop_reason,
            "summary": summary,
            "error": error,
            "completed_at": store.now_iso(),
        },
    )
    _backfill_anchor(session_id, summary)
    event = {
        "completed": events.RUN_COMPLETED,
        "failed": events.RUN_FAILED,
        "cancelled": events.RUN_CANCELLED,
    }[status]
    store.append_event(session_id, event, {"stop_reason": stop_reason, "summary": summary})


def _backfill_anchor(session_id: str, summary: str) -> None:
    """Make a finished external run read as an ordinary reply in the chat.

    This is the load-bearing step for the whole feature. The anchor row held the
    turn's place while the CLI worked; filling it in is what lets the *next* turn
    -- by any executor, including plain chat against a local model -- read what
    happened here through normal chat history, because the chat service sees
    message rows and knows nothing about sessions or executors.
    """

    row = store.get_session(session_id) or {}
    anchor_id = row.get("anchor_message_id")
    if not anchor_id:
        return
    executor = row.get("executor") or "external"
    meta = (row.get("external_meta") or {}).get(executor) or {}
    try:
        chunks = [
            event
            for event in store.list_events(session_id, limit=5000)
            if event.get("type") == events.CHUNK and (event.get("content") or "").strip()
        ]
    except Exception:  # pragma: no cover - the run is over; this is cosmetic
        chunks = []
    last = chunks[-1] if chunks else {}
    try:
        store.update_anchor_message(
            anchor_id,
            {
                "content": (last.get("content") or "").strip() or summary,
                "provider_name": executor,
                "model_name": meta.get("model"),
                "route_name": "external",
                "prompt_tokens": meta.get("prompt_tokens") or None,
                "completion_tokens": meta.get("completion_tokens") or None,
                "duration_ms": meta.get("duration_ms") or None,
            },
        )
    except Exception:  # pragma: no cover
        _LOG.warning("external_anchor_backfill_failed session=%s", session_id)


__all__ = [
    "STEP_STARTED",
    "ExternalAgentError",
    "HandoffStep",
    "RunOutcome",
    "preset_steps",
    "run",
    "run_chain",
    "run_step",
]
