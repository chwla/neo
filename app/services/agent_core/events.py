"""The event vocabulary a session streams.

Events are appended to a durable log with a monotonic sequence number, so the
same records serve live streaming and reconnection: a browser that reloads asks
for everything after the last sequence it saw. The chat path's vocabulary
(`chunk`, `thinking`, `status`) is kept so the frontend renderers stay shared,
and the agentic kinds it never had are added alongside.
"""

from __future__ import annotations

RUN_STARTED = "run.started"
CHUNK = "chunk"
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
