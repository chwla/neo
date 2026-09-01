"""The agent loop.

This is the piece the previous design did not have. ``AgentRunner.run_sync``
walked a fixed list of step types chosen by two regexes and produced text; there
was no point at which a result could change what happened next. Here the model
decides each step, sees the outcome, and decides again.

Three properties are worth stating because they are what make it an agent rather
than a pipeline:

* **The model chooses.** Nothing in this module knows what a "draft" or a
  "patch proposal" step is. It knows how to run whatever tool was asked for.
* **Results feed back.** Every tool result is appended to the transcript the
  next iteration reads, so a failure changes the next decision instead of being
  recorded and ignored.
* **Stopping is adjudicated.** A turn without tool calls means the model stopped
  talking. Whether the objective was met is decided separately, against
  evidence, in ``completion.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from app.services.agent_core import events, store
from app.services.agent_core.approvals import arguments_hash
from app.services.agent_core.completion import CompletionAdjudicator
from app.services.agent_core.context import context_budget, fit
from app.services.agent_core.permissions import PermissionResolver
from app.services.agent_core.prompt import build_environment, build_system_prompt
from app.services.agent_core.tool_protocol import (
    ModelTurn,
    ToolProtocolError,
    request_tool_calls,
)
from app.services.agent_core.tools.base import ToolContext
from app.services.agent_core.tools.registry import ToolRegistry
from app.services.agent_core.types import (
    AgentSession,
    Budgets,
    Grant,
    TodoItem,
    ToolCall,
    ToolResult,
    status_for_stop_reason,
)
from app.services.agent_core.workspace import is_live
from app.services.llm import LLMMessage

_LOG = logging.getLogger(__name__)


class Suspended(Exception):
    """The loop stopped mid-turn to wait for a human decision."""


def _to_llm_messages(rows: list[dict]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=row["role"],
            content=row.get("content") or "",
            tool_calls=row.get("tool_calls") or None,
            tool_call_id=row.get("tool_call_id"),
            name=row.get("name"),
        )
        for row in rows
    ]


class AgentLoop:
    def __init__(
        self,
        *,
        llm_factory: Callable[[], Any],
        registry: ToolRegistry | None = None,
        adjudicator: CompletionAdjudicator | None = None,
        context_window: int | None = None,
    ) -> None:
        self.llm_factory = llm_factory
        self.registry = registry or ToolRegistry()
        self.adjudicator = adjudicator or CompletionAdjudicator(llm_factory)
        self.resolver = PermissionResolver()
        self.context_window = context_window

    # --- persistence helpers -------------------------------------------------

    def _session(self, session_id: str) -> AgentSession:
        row = store.get_session(session_id)
        if row is None:
            raise LookupError("Agent session not found.")
        return AgentSession(**{**row, "budgets": Budgets(**(row.get("budgets") or {}))})

    def _emit(self, session_id: str, event_type: str, payload: dict | None = None) -> None:
        store.append_event(session_id, event_type, payload or {})

    def _cancelled(self, session_id: str) -> bool:
        row = store.get_session(session_id)
        return row is None or row["status"] == "cancelled"

    def _finish(self, session: AgentSession, reason: str, summary: str) -> AgentSession:
        status = status_for_stop_reason(reason)
        store.update_session(
            session.id,
            {
                "status": status,
                "stop_reason": reason,
                "summary": summary,
                "completed_at": store.now_iso(),
            },
        )
        event = {
            "completed": events.RUN_COMPLETED,
            "failed": events.RUN_FAILED,
            "cancelled": events.RUN_CANCELLED,
        }[status]
        self._backfill_anchor(session, summary)
        self._emit(session.id, event, {"stop_reason": reason, "summary": summary})
        return self._session(session.id)

    def _backfill_anchor(self, session: AgentSession, summary: str) -> None:
        """Leave a finished run reading like any other reply in the chat.

        The anchor row held the turn's place while the run worked; filling it in
        is what makes the transcript one conversation rather than a chat with
        gaps where the agent went.  It also puts the answer where the next plain
        reply will read it, since the chat service sees message rows and knows
        nothing about sessions.

        Answer before summary, matching the run view: the summary adjudicates
        whether the objective was met, which is not the same as what to say.
        """

        if not session.anchor_message_id:
            return
        try:
            chunks = [
                event
                for event in store.list_events(session.id, limit=2000)
                if event.get("type") == events.CHUNK and (event.get("content") or "").strip()
            ]
        except Exception:  # pragma: no cover - the run is over; this is cosmetic
            chunks = []
        last = chunks[-1] if chunks else {}
        totals = {
            "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in chunks),
            "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in chunks),
            "total_tokens": sum(int(item.get("total_tokens") or 0) for item in chunks),
            "duration_ms": sum(int(item.get("duration_ms") or 0) for item in chunks),
        }
        try:
            store.update_anchor_message(
                session.anchor_message_id,
                {
                    "content": (last.get("content") or "").strip() or summary,
                    "provider_name": last.get("provider_name"),
                    "model_name": last.get("model_name"),
                    "route_name": last.get("route_name") or "agent",
                    "finish_reason": last.get("finish_reason"),
                    **{key: value for key, value in totals.items() if value},
                },
            )
        except Exception:  # pragma: no cover
            _LOG.warning("agent_anchor_backfill_failed session=%s", session.id)

    # --- the loop ------------------------------------------------------------

    def run(self, session_id: str) -> AgentSession:
        """Drive a session until it finishes, blocks, or needs a decision."""

        session = self._session(session_id)
        if session.status in {"completed", "failed", "cancelled"}:
            return session

        if session.status == "queued":
            store.update_session(session_id, {"status": "running", "started_at": store.now_iso()})
            self._emit(session_id, events.RUN_STARTED, {"objective": session.objective})
            self._seed_transcript(session)
        else:
            store.update_session(session_id, {"status": "running"})

        deadline = time.monotonic() + session.budgets.max_seconds

        while True:
            session = self._session(session_id)
            if self._cancelled(session_id):
                return self._session(session_id)

            stop = self._budget_breach(session, deadline)
            if stop:
                return self._finish(session, "budget_exhausted", stop)

            try:
                turn = self._think(session)
            except ToolProtocolError as exc:
                return self._finish(
                    session, "failed", f"The model did not produce a usable tool call: {exc}"
                )
            except Exception as exc:
                _LOG.warning("agent_loop_model_failed session=%s", session_id, exc_info=exc)
                store.update_session(session_id, {"error": str(exc)})
                return self._finish(session, "failed", f"The model call failed: {exc}")

            store.update_session(session_id, {"iterations": session.iterations + 1})

            if turn.content:
                # The transcript renders the same footer chat does, so the turn's
                # provider, model, tokens and duration ride with its narration.
                self._emit(session_id, events.CHUNK, {"content": turn.content, **turn.metadata})
            store.append_message(
                session_id,
                {
                    "role": "assistant",
                    "content": turn.content,
                    "tool_calls": [call.model_dump() for call in turn.tool_calls],
                },
            )

            if not turn.tool_calls:
                if turn.truncated:
                    # The reply hit the token ceiling mid-thought. That is not the
                    # model deciding it is finished, so adjudicating here would end
                    # the run in the middle of the work. Prompt it to carry on; the
                    # iteration budget still bounds this.
                    store.append_message(
                        session_id,
                        {
                            "role": "user",
                            "content": (
                                "Your previous reply was cut off before it finished. "
                                "Continue from where you stopped, and call the tool you "
                                "were about to use rather than describing it."
                            ),
                        },
                    )
                    self._emit(session_id, events.STATUS, {"content": "Continuing a cut-off reply"})
                    continue
                verdict = self._adjudicate(session_id)
                if verdict is None:
                    continue
                return verdict

            try:
                self._run_calls(session_id, turn.tool_calls)
            except Suspended:
                return self._session(session_id)

    def _budget_breach(self, session: AgentSession, deadline: float) -> str | None:
        budgets = session.budgets
        if session.iterations >= budgets.max_iterations:
            return f"Stopped after {budgets.max_iterations} steps without finishing."
        if time.monotonic() > deadline:
            return f"Stopped after {budgets.max_seconds // 60} minutes without finishing."
        if session.tool_call_count >= budgets.max_tool_calls:
            return f"Stopped after {budgets.max_tool_calls} tool calls without finishing."
        if session.consecutive_errors >= budgets.max_consecutive_errors:
            return (
                f"Stopped after {budgets.max_consecutive_errors} consecutive tool failures. "
                "The agent was not making progress."
            )
        return None

    def _seed_transcript(self, session: AgentSession) -> None:
        schemas = self.registry.schemas(
            mode=session.mode,
            has_repo=bool(session.repo_id),
            live=is_live(session.repo_id),
            disabled=frozenset(session.disabled_tools),
        )
        native = bool(getattr(self.llm_factory(), "supports_tools", lambda: False)())
        repo, project = self._context_records(session)
        store.append_message(
            session.id,
            {
                "role": "system",
                "content": build_system_prompt(
                    session,
                    tool_schemas=schemas,
                    native_tools=native,
                    environment=build_environment(session, repo, project),
                ),
            },
        )
        for row in self._chat_history(session):
            store.append_message(session.id, row)
        store.append_message(session.id, {"role": "user", "content": session.objective})

    @staticmethod
    def _chat_history(session: AgentSession) -> list[dict]:
        """What was already said in the chat this run is a turn of.

        Seeded as ordinary user/assistant messages so `fit` compacts them by the
        same rules as the run's own transcript rather than by a second policy.

        The message immediately before the anchor is the prompt that started
        this run: it is dropped here because the objective is appended right
        after, and the same instruction twice reads to a model as emphasis.
        """

        if not session.chat_id or not session.anchor_message_id:
            return []
        try:
            history = store.chat_history_before(session.chat_id, session.anchor_message_id)
        except Exception:  # pragma: no cover - history is context, never the run
            return []
        objective = session.objective.strip()
        while history and history[-1]["role"] == "user":
            if history[-1]["content"].strip() != objective:
                break
            history.pop()
        return history

    @staticmethod
    def _context_records(session: AgentSession) -> tuple[dict | None, dict | None]:
        repo = project = None
        if session.repo_id:
            try:
                from app.services.repos import store as repos_store

                repo = repos_store.get_repo(session.repo_id)
            except Exception:
                repo = None
        if session.project_id:
            try:
                from app.services.agent_core.store import get_sidebar_project

                project = get_sidebar_project(session.project_id)
            except Exception:
                project = None
        return repo, project

    def _think(self, session: AgentSession) -> ModelTurn:
        messages = _to_llm_messages(store.list_messages(session.id))
        budget = context_budget(self.context_window, session.budgets.context_fraction)
        schemas = self.registry.schemas(
            mode=session.mode,
            has_repo=bool(session.repo_id),
            live=is_live(session.repo_id),
            disabled=frozenset(session.disabled_tools),
        )
        return request_tool_calls(self.llm_factory(), fit(messages, budget), schemas)

    # --- tool execution ------------------------------------------------------

    def _run_calls(self, session_id: str, calls: list[ToolCall]) -> None:
        for call in calls:
            session = self._session(session_id)
            if self._cancelled(session_id):
                raise Suspended()

            tool = self.registry.get(call.name)
            self._emit(
                session_id,
                events.TOOL_CALL,
                {
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "summary": tool.describe(call.arguments) if tool else call.name,
                },
            )

            if tool is None:
                self._record(
                    session,
                    call,
                    self.registry.execute(
                        call,
                        self._tool_context(session),
                        disabled=frozenset(session.disabled_tools),
                    ),
                )
                continue

            if call.name in session.disabled_tools:
                self._record(
                    session,
                    call,
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        status="denied",
                        content="This tool has been turned off for this chat.",
                        error="tool disabled for this chat",
                    ),
                )
                continue

            grants = [Grant(**row) for row in store.list_grants(session_id)]
            decision = self.resolver.resolve(
                tool=tool, arguments=call.arguments, mode=session.mode, grants=grants
            )

            if decision.outcome == "deny":
                self._record(
                    session,
                    call,
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        status="denied",
                        content=f"Not permitted: {decision.reason}",
                        error=decision.reason,
                    ),
                )
                continue

            if decision.outcome == "propose":
                self._record(
                    session,
                    call,
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        status="proposed",
                        content=(
                            "PLAN MODE, not executed. This would: "
                            f"{tool.describe(call.arguments)}. "
                            "Continue planning; describe the remaining steps."
                        ),
                    ),
                )
                continue

            if decision.outcome == "ask":
                self._request_approval(session, call, tool, decision)
                raise Suspended()

            self._record(session, call, self._execute(session, call))

    def _tool_context(self, session: AgentSession) -> ToolContext:
        def sink(items: list[TodoItem]) -> None:
            store.update_session(session.id, {"todo": [item.model_dump() for item in items]})
            self._emit(
                session.id, events.TODO_UPDATED, {"items": [item.model_dump() for item in items]}
            )

        return ToolContext(
            session_id=session.id,
            project_id=session.project_id,
            repo_id=session.repo_id,
            task_id=session.task_id,
            mode=session.mode,
            extras={"todo_sink": sink},
        )

    def _execute(self, session: AgentSession, call: ToolCall) -> ToolResult:
        return self.registry.execute(
            call, self._tool_context(session), disabled=frozenset(session.disabled_tools)
        )

    def _request_approval(self, session: AgentSession, call, tool, decision) -> None:
        approval = store.insert_approval(
            {
                "id": store.new_id(),
                "session_id": session.id,
                "call_id": call.id,
                "tool_name": call.name,
                "arguments": call.arguments,
                "arguments_hash": arguments_hash(call.arguments),
                "reason": decision.reason,
                "grantable": decision.grantable,
                "created_at": store.now_iso(),
            }
        )
        store.update_session(session.id, {"status": "waiting_approval"})
        self._emit(
            session.id,
            events.APPROVAL_REQUIRED,
            {
                "approval_id": approval["id"],
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "summary": tool.describe(call.arguments),
                "reason": decision.reason,
                "grantable": decision.grantable,
            },
        )

    def _record(self, session: AgentSession, call: ToolCall, result: ToolResult) -> None:
        """Persist a tool result, feed it back to the model, and update counters."""

        store.append_message(
            session.id,
            {
                "role": "tool",
                "content": result.content or ("error: " + (result.error or "")),
                "tool_call_id": call.id,
                "name": call.name,
            },
        )
        store.record_tool_call(
            session.id,
            {
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "status": result.status,
                "content": result.content,
                "error": result.error,
                "duration_ms": result.duration_ms,
            },
        )
        self._emit(
            session.id,
            events.TOOL_RESULT,
            {
                "call_id": call.id,
                "name": call.name,
                "status": result.status,
                "content": (result.content or "")[:4000],
                "error": result.error,
                "duration_ms": result.duration_ms,
            },
        )

        current = self._session(session.id)
        updates: dict[str, Any] = {
            "tool_call_count": current.tool_call_count + 1,
            # Consecutive, not total: an agent that fails, adapts and succeeds is
            # working correctly. Only an unbroken run of failures means stuck.
            "consecutive_errors": current.consecutive_errors + 1 if result.failed else 0,
        }
        if result.evidence is not None:
            evidence = [*current.evidence, result.evidence]
            updates["evidence"] = [item.model_dump() for item in evidence]
            self._emit(session.id, events.EVIDENCE_RECORDED, result.evidence.model_dump())
        store.update_session(session.id, updates)

    # --- stopping ------------------------------------------------------------

    def _adjudicate(self, session_id: str) -> AgentSession | None:
        """Decide what an empty tool-call turn means. ``None`` means keep working."""

        session = self._session(session_id)
        transcript = _to_llm_messages(store.list_messages(session_id))
        verdict = self.adjudicator.adjudicate(session, transcript)

        if verdict.continue_working:
            store.update_session(session_id, {"adjudications": session.adjudications + 1})
            store.append_message(
                session_id,
                {
                    "role": "user",
                    "content": (
                        "You are not finished yet. Outstanding:\n"
                        + "\n".join(f"- {item}" for item in verdict.unmet_criteria)
                        + "\n\nContinue working. Use tools; do not just reply."
                    ),
                },
            )
            self._emit(session_id, events.STATUS, {"content": "Checking remaining work"})
            return None

        return self._finish(session, verdict.stop_reason, verdict.summary)


def resume_after_approval(loop: AgentLoop, session_id: str, approval_id: str) -> AgentSession:
    """Run an approved call, then hand control back to the loop.

    The call is claimed atomically against its frozen argument hash, so an
    approval authorises exactly one execution of exactly the arguments the user
    saw -- and resumption continues the existing transcript rather than
    restarting the run, which is what the previous design did.
    """

    approval = store.get_approval(approval_id)
    if approval is None or approval["session_id"] != session_id:
        raise LookupError("Approval not found.")

    session = loop._session(session_id)
    call = ToolCall(
        id=approval["call_id"], name=approval["tool_name"], arguments=approval["arguments"]
    )

    if approval["status"] == "rejected":
        loop._record(
            session,
            call,
            ToolResult(
                call_id=call.id,
                name=call.name,
                status="denied",
                content="The user rejected this action. Do not retry it; find another approach.",
                error="rejected by user",
            ),
        )
        store.update_session(session_id, {"status": "running"})
        return loop.run(session_id)

    claimed = store.claim_approval(approval_id, arguments_hash(approval["arguments"]))
    if claimed is None:
        raise ValueError("This approval is no longer valid.")

    loop._record(session, call, loop._execute(session, call))
    store.update_session(session_id, {"status": "running"})
    return loop.run(session_id)
