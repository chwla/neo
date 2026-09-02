"""The event vocabulary a session streams.

Events are appended to a durable log with a monotonic sequence number, so the
same records serve live streaming and reconnection: a browser that reloads asks
for everything after the last sequence it saw. The chat path's vocabulary
(`chunk`, `thinking`, `status`) is kept so the frontend renderers stay shared,
and the agentic kinds it never had are added alongside.
"""

from __future__ import annotations

#: Accepted, but waiting for a slot.  Neo runs a bounded number of turns at
#: once so that several background chats cannot collectively stall a single
#: local model server; a turn past that bound sits in this state, visible, until
#: one ahead of it finishes.  It is not terminal -- the turn has not failed and
#: has not been refused.
#:
#: Named for the turn rather than the run because both kinds emit it, and a
#: queued turn has no run yet -- that is the whole point of the state.
QUEUED = "turn.queued"
RUN_STARTED = "run.started"
CHUNK = "chunk"
#: The whole answer so far, replacing what came before rather than extending it.
#: Only the chat path emits this -- it is how a reply that had to be validated
#: and rewritten (web citations, for one) reaches a reader that was already
#: shown the draft.
REPLACE = "replace"
THINKING = "thinking"
STATUS = "run.status"
TODO_UPDATED = "todo.updated"
TOOL_CALL = "tool.call"
TOOL_RESULT = "tool.result"
APPROVAL_REQUIRED = "approval.required"
EVIDENCE_RECORDED = "evidence.recorded"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
RUN_CANCELLED = "run.cancelled"

TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})
