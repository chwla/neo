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

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.agent_core import events, journal, permissions, store
from app.services.agent_core.approvals import arguments_hash
from app.services.agent_core.completion import CompletionAdjudicator, evidence_verdict
from app.services.agent_core.context import context_budget, fit
from app.services.agent_core.permissions import PermissionResolver
from app.services.agent_core.prompt import build_environment, build_system_prompt
from app.services.agent_core.tool_protocol import (
    ModelTurn,
    ToolProtocolError,
    request_tool_calls,
)
from app.services.agent_core.tools.base import ToolContext
from app.services.agent_core.tools.registry import ToolRegistry, effective_disabled_tools
from app.services.agent_core.types import (
    TERMINAL_STATUSES,
    AgentSession,
    Budgets,
    Grant,
    TodoItem,
    ToolCall,
    ToolResult,
    status_for_stop_reason,
)
from app.services.agent_core.workspace import has_workspace, is_live
from app.services.disclosure import Label, from_json, join, public, to_json
from app.services.disclosure.gate import ApprovedCall
from app.services.isolation import launch
from app.services.llm import LLMMessage

_LOG = logging.getLogger(__name__)


class Suspended(Exception):
    """The loop stopped mid-turn to wait for a human decision."""


#: Marks a run from before provenance was recorded, whose context is unknown.
LEGACY_CONTEXT_TAG = permissions.LEGACY_CONTEXT_TAG


def _to_llm_messages(rows: list[dict]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=row["role"],
            content=row.get("content") or "",
            tool_calls=row.get("tool_calls") or None,
            tool_call_id=row.get("tool_call_id"),
            name=row.get("name"),
            source_label=row.get("source_label")
            if isinstance(row.get("source_label"), Label)
            else Label.unknown("unlabelled_agent_message"),
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
        body = dict(payload or {})
        if event_type in {events.CHUNK, events.TOOL_CALL, events.TOOL_RESULT} or (
            event_type in events.TERMINAL_EVENTS
        ):
            body["source_label"] = to_json(self._egress(session_id).label)
        store.append_event(session_id, event_type, body)

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
        # The caller's session object may predate the last private tool read.
        # Source tags are recovered from the committed session row at write time.
        label = self._egress(session.id).label
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
                    # Neo's own loop wrote this, and it may have read the user's
                    # private data on the way. Marked as such so no later turn
                    # forwards it to an external engine without approval.
                    "metadata_json": json.dumps({
                        "provenance": "internal_model",
                        "label_tags": sorted(label.tags),
                        "source_label": to_json(label),
                    }),
                    **{key: value for key, value in totals.items() if value},
                },
            )
        except Exception:  # pragma: no cover
            _LOG.warning("agent_anchor_backfill_failed session=%s", session.id)

    # --- the loop ------------------------------------------------------------

    def run(self, session_id: str) -> AgentSession:
        """Drive a session until it finishes, blocks, or needs a decision."""

        session = self._session(session_id)
        if session.status in TERMINAL_STATUSES:
            return session

        if session.status == "queued":
            history = self._chat_history(session)
            self._mark_initial_context(session, history)
            store.update_session(session_id, {"status": "running", "started_at": store.now_iso()})
            self._emit(session_id, events.RUN_STARTED, {"objective": session.objective})
            self._seed_transcript(session, history)
        else:
            if not (store.get_session(session_id) or {}).get("egress_marked"):
                # A run started before provenance was recorded may have seeded
                # private transcript rows without saying so. Treat it as restricted
                # on resume. Only such a row has no marker at all: a run whose
                # starting context was classified and found public carries an
                # explicit empty list, and resuming it changes nothing -- reading
                # "empty" as "unknown" made every run restricted after its first
                # approval pause, which stopped grants and AUTO mode from applying.
                store.update_session(session_id, {"egress_tags": [LEGACY_CONTEXT_TAG]})
            store.update_session(session_id, {"status": "running"})

        deadline = time.monotonic() + session.budgets.max_seconds

        while True:
            session = self._session(session_id)
            if self._cancelled(session_id):
                return self._session(session_id)

            # Calls a previous pass left unanswered -- the rest of a batch whose
            # first call paused for approval, or one cut short by a restart --
            # run before the model is asked anything else.
            try:
                self._run_calls(session_id, self._unanswered_calls(session_id))
            except Suspended:
                return self._session(session_id)
            self._answer_orphaned_calls(session_id)

            # Also runs after approval/restart: the persisted batch and tool results
            # are the authority, rather than a transient flag in the worker.
            session = self._session(session_id)
            if self._cancelled(session_id):
                return session
            finished = self._finish_file_task(session)
            if finished is not None:
                return finished

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

            if self._cancelled(session_id):
                return self._session(session_id)
            if turn.truncated:
                # A partial model response cannot assert that the whole task is
                # finished, even if a complete file call preceded the cutoff.
                for call in turn.tool_calls:
                    call.arguments.pop("complete_task", None)
            store.update_session(session_id, {"iterations": session.iterations + 1})
            self._raise_model_label(session_id, turn.source_label)

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
                    "source_label": turn.source_label,
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
                            "source_label": public("builtin:continue_prompt"),
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

    @staticmethod
    def _mark_initial_context(session: AgentSession, history: list[dict]) -> None:
        """Persist private prompt sources before the first model call."""

        tags = set((store.get_session(session.id) or {}).get("egress_tags") or [])
        if history:
            tags.add("chat_private")
        if session.project_id:
            tags.add("project")
        if (session.agent_definition_snapshot or {}).get("system_prompt"):
            tags.add("agent_def")
        if session.skills:
            tags.add("skill")
        if session.repo_id:
            from app.core.config import active_profile_storage_dir
            from app.services.disclosure.artifacts import ArtifactLabels
            from app.services.repos import store as repos_store

            storage = active_profile_storage_dir.get()
            if not storage:
                tags.add("unknown_context")
            else:
                labels = ArtifactLabels(Path(storage) / "artifact-labels.json")
                repo_label = labels.get(f"repo:{session.repo_id}")
                if repo_label.is_private():
                    tags.update(repo_label.tags)
                elif repo_label.is_unknown():
                    tags.add("unknown_context")
                repo = repos_store.get_repo(session.repo_id)
                if not repo:
                    tags.add("unknown_context")
                else:
                    # A live folder may have been renamed and registered under a
                    # different repo id. Its inode record carries the old scope.
                    for path in {repo.get("workspace_path"), repo.get("original_path")}:
                        if not path:
                            continue
                        try:
                            identity = Path(path).stat()
                        except OSError:
                            tags.add("unknown_context")
                            continue
                        identity_label = labels.get(
                            f"folder:{identity.st_dev}:{identity.st_ino}"
                        )
                        if identity_label.is_private():
                            tags.update(identity_label.tags)
        # Written even when empty. The stored empty list is the record that this
        # run's starting context was looked at and held nothing private; a row
        # with no value at all is one that was never classified.
        store.update_session(session.id, {"egress_tags": sorted(tags)})

    @staticmethod
    def _raise_model_label(session_id: str, label: Label) -> None:
        """Persist the actual model-call provenance before accepting its output."""

        tags = set(label.tags) if label.is_private() else set()
        if label.is_unknown():
            tags.add("unknown_context")
        if not tags:
            return
        current = set((store.get_session(session_id) or {}).get("egress_tags") or [])
        if not tags <= current:
            store.update_session(session_id, {"egress_tags": sorted(current | tags)})

    def _seed_transcript(self, session: AgentSession, history: list[dict] | None = None) -> None:
        has_repo = has_workspace(session.repo_id)
        schemas = self.registry.schemas(
            mode=session.mode,
            has_repo=has_repo,
            live=is_live(session.repo_id),
            disabled=frozenset(session.disabled_tools),
            skills=session.skills,
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
                    environment=build_environment(session, repo if has_repo else None, project),
                ),
                "source_label": self._egress(session.id).label,
            },
        )
        for row in history if history is not None else self._chat_history(session):
            stored_label = row.get("source_label")
            if isinstance(stored_label, Label):
                source_label = stored_label
            elif isinstance(stored_label, str):
                source_label = from_json(stored_label)
            elif row.get("role") == "user":
                source_label = Label.private("chat_private")
            else:
                source_label = Label.unknown("legacy_chat_assistant")
            # A history row's `id` is its chat_messages id. Transcript ids are one
            # global key across every run, so reusing it made the second agent run
            # in a chat fail on rows the first run had already copied.
            seeded = {key: value for key, value in row.items() if key != "id"}
            store.append_message(
                session.id,
                {**seeded, "source_label": join(Label.private("chat_private"), source_label)},
            )
        store.append_message(session.id, {
            "role": "user", "content": session.objective,
            "source_label": self._objective_label(session),
        })

    @staticmethod
    def _objective_label(session: AgentSession) -> Label:
        """The objective's provenance, as the server recorded it.

        Typed this turn, it is the user's own words. Supplied by the server -- a
        task's text -- it is whatever that text was, and a run must not reword it
        into something the user typed.
        """

        stored = getattr(session, "objective_label_json", None)
        if stored:
            return from_json(stored)
        if getattr(session, "task_id", None):
            # Created before objective provenance was recorded, and linked to a
            # task: the objective may well have been the task's own text.
            return Label.unknown("legacy_task_session_objective")
        return public(f"user_input:{session.id}")

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
            has_repo=has_workspace(session.repo_id),
            live=is_live(session.repo_id),
            disabled=frozenset(session.disabled_tools),
            skills=session.skills,
        )
        return request_tool_calls(self.llm_factory(), fit(messages, budget), schemas)

    # --- tool execution ------------------------------------------------------

    @staticmethod
    def _open_batch(session_id: str) -> tuple[list[ToolCall], set[str], bool]:
        """The last assistant turn's calls, which are answered, and whether it is last.

        ``True`` when nothing but tool results follows that turn, which is when
        the calls still missing a result can simply be run.
        """

        rows = store.list_messages(session_id)
        index = next(
            (
                position
                for position in range(len(rows) - 1, -1, -1)
                if rows[position]["role"] == "assistant" and rows[position].get("tool_calls")
            ),
            None,
        )
        if index is None:
            return [], set(), True
        calls = []
        for raw in rows[index].get("tool_calls") or []:
            try:
                calls.append(ToolCall(**raw))
            except Exception:  # a malformed stored call cannot be run or answered
                continue
        after = rows[index + 1:]
        answered = {str(row.get("tool_call_id")) for row in after if row["role"] == "tool"}
        return calls, answered, all(row["role"] == "tool" for row in after)

    def _unanswered_calls(self, session_id: str) -> list[ToolCall]:
        """Calls from the last model turn that have no result yet, in order.

        A model may ask for several tools in one turn. When one of them needs the
        user's approval the run suspends, and only that one call used to be run on
        resume: the rest were never executed and never answered. The model was
        left guessing, and a provider that checks the protocol rejected the next
        request outright, because every call id must have a result.

        The call waiting on a decision is left out: it is answered by the
        approval path, not here.
        """

        calls, answered, trailing = self._open_batch(session_id)
        if not trailing:
            return []
        pending = store.pending_approval(session_id)
        waiting = {pending["call_id"]} if pending else set()
        return [call for call in calls if call.id not in answered and call.id not in waiting]

    def _answer_orphaned_calls(self, session_id: str) -> None:
        """Give every call of the last model turn a result before the next request.

        The invariant a provider enforces: an assistant turn's tool calls are each
        followed by a tool message. A call can still be missing one when other
        messages were appended after the batch (the user steering a paused run),
        at which point it can no longer be run in place. It is answered as not
        run, so the transcript stays valid and the model can ask again.
        """

        calls, answered, trailing = self._open_batch(session_id)
        if trailing:
            return
        for call in calls:
            if call.id in answered:
                continue
            store.append_message(
                session_id,
                {
                    "role": "tool",
                    "content": "This call was not run. Call the tool again if it is still needed.",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "source_label": public("builtin:unanswered_tool_call"),
                },
            )

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

            if call.name in effective_disabled_tools(set(session.disabled_tools)):
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
                tool=tool,
                arguments=call.arguments,
                mode=session.mode,
                grants=grants,
                egress=self._egress(session_id),
                isolated=launch.sandbox_available(),
                undo_available=journal.undo_available()
                or not is_live(session.repo_id),
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

            result = self._execute(session, call)
            self._note_private_read(session_id, call.name, result)
            self._record(session, call, result)

    @staticmethod
    def _egress(session_id: str) -> permissions.EgressContext:
        """What private data this run has read so far.

        Read from the session row rather than a local, because a run suspends for an
        approval and resumes in a later call; a restriction held in memory would be
        forgotten exactly when the user had just been asked about it.
        """

        row = store.get_session(session_id) or {}
        tags = tuple(str(tag) for tag in (row.get("egress_tags") or []))
        if not tags:
            return permissions.EgressContext(label=public(f"user_input:{session_id}"))
        if "unknown_context" in tags:
            return permissions.EgressContext(label=Label.unknown("unlabelled_model_context"))
        return permissions.EgressContext(label=Label.private(*tags))

    @staticmethod
    def _note_private_read(session_id: str, tool_name: str, result: ToolResult) -> None:
        """Record that this run has taken in private data.

        Only ever adds. Once the model has seen a memory there is no way to show
        that a URL it composes later does not encode it, so the restriction holds
        for the rest of the run instead of being re-derived per call.
        """

        tags = permissions.tags_for_read(tool_name)
        if not tags or result.status in {"denied", "error"}:
            return
        row = store.get_session(session_id) or {}
        current = {str(tag) for tag in (row.get("egress_tags") or [])}
        merged = current | set(tags)
        if merged != current:
            store.update_session(session_id, {"egress_tags": sorted(merged)})

    def _tool_context(
        self, session: AgentSession, *, approved: ApprovedCall | None = None
    ) -> ToolContext:
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
            # The session's own snapshot, so ``load_skill`` refuses anything the
            # chat did not have turned on when this run started.
            extras={
                "todo_sink": sink,
                "skills": session.skills,
                # Present only for the one execution resume_after_approval claimed;
                # an ordinary execution never carries it.
                **({"approved_call": approved} if approved is not None else {}),
            },
            source_label=self._egress(session.id).label,
        )

    def _execute(
        self, session: AgentSession, call: ToolCall, *, approved: ApprovedCall | None = None
    ) -> ToolResult:
        return self.registry.execute(
            call,
            self._tool_context(session, approved=approved),
            disabled=frozenset(session.disabled_tools),
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

        result_label = join(self._egress(session.id).label, from_json(result.source_label_json))
        self._raise_model_label(session.id, result_label)
        store.append_message(
            session.id,
            {
                "role": "tool",
                "content": result.content or ("error: " + (result.error or "")),
                "tool_call_id": call.id,
                "name": call.name,
                "source_label": result_label,
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

    def _finish_file_task(self, session: AgentSession) -> AgentSession | None:
        """Accept an explicit final file action only after its saved result exists.

        The model decides whether all requested work fits in this batch; the
        runtime checks execution. Merely creating a file never ends a task.
        Failed/proposed/denied actions and unfinished batches cannot finish it.
        """

        calls, answered, clean = self._open_batch(session.id)
        file_tools = {"write_file", "edit_file", "create_directory"}
        if (
            session.mode == "plan" or not calls or not clean
            or calls[-1].arguments.get("complete_task") is not True
            or any(call.name not in file_tools or call.id not in answered for call in calls)
            or session.consecutive_errors
            or any(item.status != "completed" for item in session.todo)
            or not evidence_verdict(session.evidence)
        ):
            return None
        results = {row["call_id"]: row for row in store.list_tool_calls(session.id)}
        verified = {
            item.tool_call_id for item in session.evidence
            if item.kind == "file_verify" and item.passed
        }
        if any(
            results.get(call.id, {}).get("status") != "ok" or call.id not in verified
            for call in calls
        ):
            return None
        summary = "\n".join(results[call.id]["content"] for call in calls)
        store.append_message(session.id, {
            "role": "assistant", "content": summary, "source_label": self._egress(session.id).label,
        })
        self._emit(session.id, events.CHUNK, {"content": summary})
        return self._finish(session, "verified_complete", summary)

    def _adjudicate(self, session_id: str) -> AgentSession | None:
        """Decide what an empty tool-call turn means. ``None`` means keep working."""

        session = self._session(session_id)
        transcript = _to_llm_messages(store.list_messages(session_id))
        self._emit(session_id, events.STATUS, {"content": "Checking whether the task is complete"})
        verdict = self.adjudicator.adjudicate(session, transcript)
        if self._cancelled(session_id):
            return self._session(session_id)

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
                    # The outstanding items are the adjudicator's own words, and it
                    # read this run's transcript to write them. A builtin label
                    # would launder whatever that held into "text Neo ships", so
                    # the message carries the run's restriction instead.
                    "source_label": self._egress(session_id).label,
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

    # Built from the record just claimed, never from anything the model wrote. It
    # names what the run had read when the user was asked -- the sources the
    # approval prompt described -- so the call can send what was approved and
    # nothing that draws on a source the user was not told about.
    label = loop._egress(session_id).label
    approved = ApprovedCall(
        approval_id=claimed["id"],
        session_id=session_id,
        call_id=claimed["call_id"],
        tool_name=claimed["tool_name"],
        arguments_hash=claimed["arguments_hash"],
        tags=frozenset(label.tags) if label.is_private() else frozenset(),
        covers_unknown=label.is_unknown(),
    )
    loop._record(session, call, loop._execute(session, call, approved=approved))
    store.update_session(session_id, {"status": "running"})
    return loop.run(session_id)
