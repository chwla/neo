from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from threading import Thread
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import build_engine
from app.models import ChatGeneration, ChatMessage
from app.repositories.app_store import AppStore
from app.services.agents.guidance import agent_run_guidance
from app.services.chat_intent import resolve_internal_chat_intent
from app.services.code_index.service import CodeIndexService
from app.services.coding_agent.service import CodingAgentService
from app.services.context import ContextPackage
from app.services.files.service import WorkspaceFilesService
from app.services.git.service import GitContextService
from app.services.llm import ChatTurn, LLMChatResult, LLMClient, LLMMessage
from app.services.memory.contracts import SourceKind
from app.services.memory.direct_answer import DirectMemoryAnswerService
from app.services.memory.extraction_contracts import (
    EXTRACTION_WINDOW_MAX_CHARS,
    ConversationRole,
    CurrentTurnOverride,
    ExtractionMode,
    ExtractionRequest,
    TrustedConversationMessage,
)
from app.services.memory.factory import MemoryRuntime
from app.services.memory.policy import turn_may_contain_memory
from app.services.memory.runtime import drain_memory_outbox
from app.services.memory_chat import (
    STABLE_MEMORY_POLICY,
    MemoryQueryContext,
    RecallPromptOrchestrator,
    RecallPromptSelection,
    RecallQuery,
)
from app.services.projects import ProjectContextService
from app.services.recovery.service import RecoveryService
from app.services.rules.resolver import RuleResolver
from app.services.search.citations import validate_citation_markers
from app.services.search.content import FactResult, extract_release_date, run_extractors
from app.services.search.intent import SearchIntentResolver, resolve_search_intent
from app.services.search.live_data import (
    FrankfurterClient,
    LiveDataError,
    OpenMeteoClient,
    local_datetime_answer,
)
from app.services.search.types import ResolvedSearchIntent, SearchIntentKind
from app.services.source_citations import CitationFormatter
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.tasks import TaskContextService
from app.services.test_runner.service import TestRunnerContextService
from app.services.tools.executor import ToolsService, ToolValidationError
from app.services.web_search import (
    EXTRACTION_FAILURE_MESSAGE,
    GROUNDING_FAILURE_MESSAGE,
    WebContext,
    WebSearchDecisionService,
    WebSearchService,
)

_ROUTING_LOG = logging.getLogger("neo.chat.routing")
_MEMORY_SAVE_OUTCOMES = frozenset(
    {"created", "reconfirmed", "refined", "replaced", "merged", "restored"}
)
_MEMORY_FORGET_OUTCOMES = frozenset({"archived", "forgotten", "erased_permanently"})
# Replayed in place of a turn that removed a memory.  Deleting a memory has to
# survive the rest of the conversation: the forget request and its confirmation
# both spell out the value that was just removed, so replaying them verbatim let
# the model read the deleted fact straight off the transcript and repeat it back
# while insisting it had forgotten it.
_FORGOTTEN_TURN_PLACEHOLDER = "[A memory was removed in this turn. Its content is unavailable.]"
_CONNECTOR_INFORMATIONAL_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:explain|describe|document|write\s+(?:documentation|docs)|"
    r"compare|define|summari[sz]e|teach|tell\s+me\s+about|"
    r"what\b|why\b|how\b|when\b|where\b|who\b|which\b)",
    re.IGNORECASE,
)


class NeoChatService:
    """Connects memory context, Ollama generation, archiving, and extraction."""

    def __init__(
        self,
        db: Session,
        ollama: LLMClient | None = None,
        rule_result: dict[str, Any] | None = None,
        memory_orchestrator: RecallPromptOrchestrator | None = None,
        memory_context_factory: Callable[[str], MemoryQueryContext | None] | None = None,
        memory_enabled: bool | None = None,
        memory_runtime: MemoryRuntime | None = None,
        active_project_id: str | None = None,
        active_project_name: str | None = None,
    ) -> None:
        self.db = db
        self.store = AppStore(db)
        if ollama is None:
            from app.services.llm import get_llm_client

            ollama = get_llm_client(route_name="chat")
        self.ollama = ollama
        self.memory_runtime = memory_runtime
        self.active_project_id = active_project_id
        self.active_project_name = active_project_name
        self.rule_result = rule_result or {
            "resolved_rules": {},
            "applied_profiles": [],
            "warnings": [],
        }
        self.settings = get_settings()
        self.memory_enabled = (
            bool(
                self.settings.memory_enabled
                and memory_orchestrator is not None
                and memory_context_factory is not None
            )
            if memory_enabled is None
            else memory_enabled
        )
        self.memory_prompt_enabled = self.memory_enabled
        # Answering straight from memory bypasses the model entirely, so the
        # model never gets to weigh saved memory against search or its own
        # knowledge.  Recall still runs and is injected as context either way.
        self.memory_direct_answers_enabled = bool(
            self.memory_enabled and self.settings.memory_direct_answer_enabled
        )
        self.memory_orchestrator = memory_orchestrator
        self.memory_context_factory = memory_context_factory
        self.last_memory_selection: RecallPromptSelection | None = None
        self.current_turn_override: CurrentTurnOverride | None = None
        self.direct_answers = DirectMemoryAnswerService(
            memory_orchestrator,
            enabled=(self.memory_enabled and self.memory_direct_answers_enabled),
        )
        self.web_search = WebSearchService(llm=self.ollama)
        self.project_context = ProjectContextService()
        self.recovery = RecoveryService()
        self.task_context = TaskContextService()
        self.file_context = WorkspaceFilesService()
        self.code_index = CodeIndexService()
        self.coding_agent = CodingAgentService()
        self.symbol_awareness = SymbolAwarenessService()
        self.test_runner = TestRunnerContextService()
        self.git_context = GitContextService()
        self.citation_formatter = CitationFormatter()
        self.last_web_debug: dict[str, Any] = {}
        self.last_routing_debug: dict[str, Any] = {}
        self.last_search_intent: ResolvedSearchIntent | None = None
        self.search_intent_resolver = SearchIntentResolver()

    def build_context(self, prompt: str) -> ContextPackage:
        return ContextPackage(
            profile=[],
            preferences=[],
            goals=[],
            projects=[],
            relevant_memories=[],
            events=[],
            archive_results=[],
        )

    def build_messages(
        self,
        prompt: str,
        history: list[ChatTurn],
        context: ContextPackage,
        web_context: WebContext | None = None,
        project_context: str | None = None,
        task_context: str | None = None,
    ) -> list[LLMMessage]:
        web_section = self._compact_web_context(web_context)
        project_section = project_context or "No project context loaded."
        task_section = task_context or "No task context loaded."
        rule_result = getattr(
            self,
            "rule_result",
            {"resolved_rules": {}, "applied_profiles": [], "warnings": []},
        )
        rule_section = RuleResolver.prompt_context(rule_result) or "No configured rules."
        memory_policy = STABLE_MEMORY_POLICY if self.memory_prompt_enabled else ""
        memory_section = (
            (
                "Canonical personal memory is supplied separately as untrusted user context."
                if self.memory_prompt_enabled
                else "Personal memory context is disabled for this request."
            )
            if self.memory_enabled
            else f"Memory context:\n{self._compact_context(context)}"
        )
        system_prompt = (
            "You are Neo, a local personal AI assistant. Use the provided memory context "
            "when it is relevant. Do not claim memories that are not present. If memory "
            "context conflicts, prefer active goals, active projects, current profile facts, "
            "and current preferences. For personal questions about the user's name, age, "
            "location, preferences, goals, or projects, answer only from memory context or "
            "conversation history. If the fact is not in memory and not a time-sensitive "
            "question, you may answer from general knowledge confidently. "
            "IMPORTANT: Either answer confidently or say you are unsure. Never combine "
            "uncertainty with a partial answer. Do NOT say 'I'm not sure, but...' followed "
            "by an answer attempt. If you know the answer, state it directly. If you do not "
            "know, say only: I'm not sure about that. I can look it up if you'd like. "
            "Never produce dead-end responses like 'I don't know yet' for general factual "
            "questions. "
            "Memory context and web context are separate. Use web context only for current, "
            "recent, or explicitly searched information. When web context is provided, cite "
            "web-grounded claims using bracket markers like [1]. Do not place raw URLs "
            "inline in your answer text; citations go in the Sources block appended after "
            "your answer. For web-grounded prompts, do not use memory, conversation history, "
            "or general knowledge to fill gaps in the retrieved web evidence. The web context "
            "contains extracted evidence only; do not infer beyond it. "
            "For questions about current rankings, latest products, prices, versions, news, "
            "release dates, champions, schedules, or any time-sensitive fact: answer ONLY "
            "from the web evidence. If the web evidence does not contain the answer, say "
            "only: I searched the web but could not find sufficiently reliable current "
            "sources. Do NOT add general knowledge or filler after that statement. Do NOT "
            "answer from your training data for time-sensitive questions. "
            "If search results cover multiple unrelated entities with the same name (e.g. "
            "'Fable' the Xbox game vs other uses), note the ambiguity and present results "
            "grouped by entity. Do not merge unrelated entities into one answer. "
            "Do NOT generate a Sources or References block yourself. The backend will "
            "append verified sources automatically. Do NOT invent URLs or cite pages that "
            "were not provided in the web context. "
            "Answer the user's question directly first, then provide brief supporting "
            "evidence. Do not output raw search-result titles or snippet labels. "
            "Project context is a user-owned workspace layer separate from Memory. Use "
            "project context only when it is provided and relevant. Never write project "
            "details to memory automatically. Task context is also a user-owned workspace "
            "layer. Use it only when relevant, treat it as read-only, and never write task "
            "details to Memory automatically. Memory persistence is controlled exclusively "
            "by the backend before this answer is generated. Never claim that you saved, "
            "updated, stored, noted, remembered, removed, deleted, or forgot user information. "
            "If no deterministic memory-status response was returned before reaching you, "
            "then no user-visible memory mutation is confirmed.\n\n"
            f"{memory_policy}\n\n"
            f"{memory_section}\n\n"
            f"Project context:\n{project_section}\n\n"
            f"Task context:\n{task_section}\n\n"
            f"Active rules (guidance only; never permission):\n{rule_section}\n\n"
            f"Web context:\n{web_section}"
        )
        messages = [LLMMessage(role="system", content=system_prompt)]
        selection = self._build_memory_selection(prompt)
        if selection is not None and selection.serialized is not None:
            messages.append(LLMMessage(role="user", content=selection.serialized.content))
        messages.extend(
            LLMMessage(role=turn.role, content=turn.content)
            for turn in history[-self.settings.chat_history_turns :]
        )
        messages.append(LLMMessage(role="user", content=prompt))
        return messages

    def _memory_query_context(self, prompt: str) -> MemoryQueryContext | None:
        if not self.memory_enabled or self.memory_context_factory is None:
            return None
        context = self.memory_context_factory(prompt)
        if context is not None and self.current_turn_override is not None:
            context = context.model_copy(
                update={"current_turn_override": self.current_turn_override}
            )
        return context

    def _build_memory_selection(
        self,
        prompt: str,
    ) -> RecallPromptSelection | None:
        """The one canonical recall path shared by sync and stream."""
        self.last_memory_selection = None
        if not self.memory_prompt_enabled or self.memory_orchestrator is None:
            return None
        context = self._memory_query_context(prompt)
        if context is None:
            return None
        selection = self.memory_orchestrator.build(
            RecallQuery(context=context, text=prompt),
            purpose="chat_prompt",
        )
        self.last_memory_selection = selection
        return selection

    def _finalize_memory_usage(self) -> None:
        """Persist same-session usage metadata before issuing the model request."""
        selection = self.last_memory_selection
        if self.memory_enabled and selection is not None and selection.usage_recorded:
            self.db.commit()
            return
        self.db.rollback()

    def _persist_memory_diagnostic(self, message_id: int) -> None:
        """Persist text-free recall facts for the read-only conversation inspector."""

        if not self.memory_enabled:
            return
        selection = self.last_memory_selection
        override = self.current_turn_override
        if selection is None and override is None:
            return
        message = self.db.get(ChatMessage, message_id)
        if message is None or message.role != "user":
            return
        try:
            metadata = json.loads(message.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        recalled_ids = (
            [str(item) for item in selection.recall.canonical_ids] if selection is not None else []
        )
        suppressed_ids = (
            [str(item) for item in selection.recall.diagnostic.suppressed_ids]
            if selection is not None
            else [str(item) for item in override.suppressed_memory_ids]
            if override is not None
            else []
        )
        final_ids = (
            [str(item) for item in selection.serialized.canonical_ids]
            if selection is not None and selection.serialized is not None
            else []
        )
        metadata["memory_diagnostic"] = {
            "recalled_ids": recalled_ids,
            "current_turn_suppressed_ids": suppressed_ids,
            "final_serialized_ids": final_ids,
        }
        message.metadata_json = json.dumps(metadata, sort_keys=True)
        self.db.flush()

    def send_message(
        self,
        chat_id: int,
        prompt: str,
        *,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> str:
        persisted_messages = self.store.list_chat_messages(chat_id)
        history = self._history_turns(persisted_messages)
        search_intent = self._resolve_search_intent(
            prompt,
            persisted_messages,
            timezone=timezone,
            locale=locale,
        )
        user_message = self.store.add_chat_message(
            chat_id,
            "user",
            prompt,
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
            },
        )
        self.store.rename_chat_from_prompt(chat_id, prompt)
        self.db.commit()
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=user_message.id,
            selected_route="pending",
            component="chat_submission",
            final_status="received",
        )

        active_rules_reply = self._active_rules_reply(prompt)
        if active_rules_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", active_rules_reply)
            self.db.commit()
            self.last_web_debug = {
                "rules_loaded": True,
                "rule_warnings": self.rule_result.get("warnings", []),
                "web_search_needed": False,
            }
            return active_rules_reply

        agent_guidance = agent_run_guidance(prompt)
        if agent_guidance is not None:
            self.store.add_chat_message(chat_id, "assistant", agent_guidance)
            self.db.commit()
            self.last_web_debug = {
                "agent_guidance": True,
                "web_search_needed": False,
            }
            return agent_guidance
        self.current_turn_override = self._analyze_current_turn(
            prompt,
            chat_id=chat_id,
            message_id=user_message.id,
            history=persisted_messages,
        )
        context = self.build_context(prompt)
        project_context = self.project_context.context_for_prompt(prompt)
        task_context = self.task_context.context_for_prompt(prompt)
        task_context = f"{task_context}\n\n{self.file_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.code_index.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.symbol_awareness.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.test_runner.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.git_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.coding_agent.context_for_prompt(prompt)}"
        internal_intent = resolve_internal_chat_intent(prompt)
        coding_direct_reply = (
            self.coding_agent.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "coding"
            else None
        )
        if coding_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", coding_direct_reply)
            self.db.commit()
            self.last_web_debug = {"coding_context_loaded": True, "web_search_needed": False}
            return coding_direct_reply
        recovery_direct_reply = (
            self.recovery.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "recovery"
            else None
        )
        if recovery_direct_reply is not None:
            self.store.add_chat_message(
                chat_id,
                "assistant",
                recovery_direct_reply,
                response_kind="internal_action",
                provider_name="Neo Recovery",
                route_name="recovery",
                finish_reason="stop",
                duration_ms=0,
            )
            self.db.commit()
            self.last_web_debug = {"recovery_context_loaded": True, "web_search_needed": False}
            return recovery_direct_reply
        git_direct_reply = (
            self.git_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "git"
            else None
        )
        if git_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", git_direct_reply)
            self.db.commit()
            self.last_web_debug = {"git_context_loaded": True, "web_search_needed": False}
            return git_direct_reply
        test_direct_reply = (
            self.test_runner.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tests"
            else None
        )
        if test_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", test_direct_reply)
            self.db.commit()
            self.last_web_debug = {"test_context_loaded": True, "web_search_needed": False}
            return test_direct_reply
        task_direct_reply = (
            self.task_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tasks"
            else None
        )
        if task_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", task_direct_reply)
            self.db.commit()
            self.last_web_debug = {
                "task_context_loaded": True,
                "web_search_needed": False,
            }
            return task_direct_reply
        connector_answer = self._connector_answer(prompt, search_intent)
        if connector_answer is not None:
            reply, metadata = connector_answer
            self.store.add_chat_message(
                chat_id,
                "assistant",
                reply,
                **metadata,
            )
            self.db.commit()
            self.last_web_debug = {
                "web_search_needed": False,
                "connector_trace": (metadata.get("metadata") or {}).get("connector_trace"),
            }
            return reply
        structured_live = self._structured_live_answer(
            prompt,
            search_intent,
            timezone=timezone,
            locale=locale,
        )
        if structured_live is not None:
            reply, metadata = structured_live
            self.store.add_chat_message(chat_id, "assistant", reply, **metadata)
            self.db.commit()
            self.last_web_debug = {
                "web_search_needed": False,
                "structured_intent": search_intent.model_dump(mode="json"),
            }
            return reply
        web_started = time.perf_counter()
        if search_intent.kind in {
            SearchIntentKind.GENERAL_WEB,
            SearchIntentKind.RELEASE_DATE,
        }:
            web_query = self._web_query_with_memory_region(prompt, context)
            web_context = self.web_search.build_context_forced(
                web_query, hint=search_intent.resolved_query
            )
        else:
            web_query = prompt
            web_context = WebContext(query=prompt, needed=False)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            self._finalize_memory_usage()
            self._persist_memory_diagnostic(user_message.id)
            self.store.add_chat_message(
                chat_id,
                "assistant",
                direct_reply,
                response_kind="direct_memory",
                provider_name="Neo memory",
                route_name="memory",
                finish_reason="stop",
                duration_ms=0,
                metadata={"search_intent": search_intent.model_dump(mode="json")},
            )
            self.db.commit()
            self.last_web_debug = self._web_debug(
                web_context, context=context, final_answer=direct_reply
            )
            return direct_reply
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
            self.store.add_chat_message(
                chat_id,
                "assistant",
                web_failure,
                response_kind="web_search",
                provider_name=(
                    web_context.search.provider if web_context.search is not None else None
                ),
                route_name="web_search",
                finish_reason="evidence_unavailable",
                duration_ms=int((time.perf_counter() - web_started) * 1000),
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            )
            self.db.commit()
            return web_failure
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            self.store.add_chat_message(
                chat_id,
                "assistant",
                direct_web_reply,
                response_kind="web_search",
                provider_name=(
                    web_context.search.provider if web_context.search is not None else None
                ),
                route_name="web_search",
                finish_reason="stop",
                duration_ms=int((time.perf_counter() - web_started) * 1000),
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            )
            self.db.commit()
            return direct_web_reply
        messages = self.build_messages(
            prompt, history, context, web_context, project_context, task_context
        )
        self._finalize_memory_usage()
        self._persist_memory_diagnostic(user_message.id)
        # Release this session's write reservation before calling the provider.
        # The provider runtime records its request audit through a separate
        # SQLite connection to the same database, so holding the transaction
        # open here makes that second writer wait out its busy timeout and fail
        # the whole turn with "database is locked".  The streaming path performs
        # the same commit for the same reason.
        self.db.commit()

        result = None
        finish_reason = None
        provider_name = None
        model_name = None
        route_name = "web_search" if web_context.needed else "chat"
        trace_id = None
        try:
            result = self._generate_complete(
                messages,
                num_predict=self._num_predict(prompt, context),
            )
            if web_context.citations and not self._has_web_citation_marker(
                result.content, web_context
            ):
                reply = self._web_generation_fallback(
                    prompt,
                    web_context,
                    RuntimeError("generated web answer lacked citation markers"),
                )
            else:
                reply = self._with_web_citations(result.content, web_context)
            prompt_tokens = result.prompt_tokens
            completion_tokens = result.completion_tokens
            total_tokens = result.total_tokens
            duration_ms = result.duration_ms
            thinking = result.thinking
            finish_reason = result.finish_reason
            provider_name = result.provider_name or result.provider_id
            model_name = result.model_name or result.model_id
            route_name = result.route_name or route_name
            trace_id = result.provider_request_id
        except Exception as exc:
            if web_context.citations:
                reply = self._web_generation_fallback(prompt, web_context, exc)
                prompt_tokens = None
                completion_tokens = None
                total_tokens = None
                duration_ms = int((time.perf_counter() - web_started) * 1000)
                thinking = None
                finish_reason = "provider_error"
            else:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
        memory_extraction = {"status": "scheduled", "source_message_id": str(user_message.id)}
        assistant = self.store.add_chat_message(
            chat_id,
            "assistant",
            reply,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            thinking=thinking,
            response_kind="web_search" if web_context.needed else "normal_chat",
            provider_name=provider_name,
            model_name=model_name,
            route_name=route_name,
            finish_reason=finish_reason,
            trace_id=trace_id,
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
                "memory_extraction": memory_extraction,
                "web_debug": self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                    final_answer=reply,
                ),
            },
        )
        self.db.commit()
        self._extract_after_response(
            prompt,
            chat_id=chat_id,
            message_id=user_message.id,
            assistant_message_id=assistant.id,
            history=persisted_messages,
            transport="sync",
        )
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
        )
        return reply

    def _persist_stream_assistant(
        self,
        chat_id: int,
        content: str,
        *,
        generation_id: str | None,
        generation_lease_token: str | None,
        **metadata,
    ) -> ChatMessage:
        """Persist one streamed result and fence workers that lost their lease."""

        if generation_id is None:
            return self.store.add_chat_message(chat_id, "assistant", content, **metadata)
        if generation_lease_token is None:
            raise RuntimeError("A durable generation requires an active lease token.")
        lease = self.db.execute(
            update(ChatGeneration)
            .where(
                ChatGeneration.id == generation_id,
                ChatGeneration.chat_id == chat_id,
                ChatGeneration.status == "running",
                ChatGeneration.lease_token == generation_lease_token,
            )
            .values(heartbeat_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        if lease.rowcount != 1:
            self.db.rollback()
            raise RuntimeError("The generation lease is no longer active.")
        return self.store.upsert_generation_assistant(
            chat_id,
            generation_id,
            content,
            **metadata,
        )

    def stream_message(
        self,
        chat_id: int,
        prompt: str,
        after_reply: Callable[[str, str], None] | None = None,
        existing_user_message_id: int | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        generation_id: str | None = None,
        generation_lease_token: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        def persist_assistant(content: str, **metadata) -> ChatMessage:
            return self._persist_stream_assistant(
                chat_id,
                content,
                generation_id=generation_id,
                generation_lease_token=generation_lease_token,
                **metadata,
            )

        persisted_messages = self.store.list_chat_messages(chat_id)
        history = self._history_turns(persisted_messages)
        if existing_user_message_id is not None:
            try:
                message_index = next(
                    index
                    for index, message in enumerate(persisted_messages)
                    if message.id == existing_user_message_id and message.role == "user"
                )
            except StopIteration as exc:
                raise ValueError("The edited user message no longer exists.") from exc
            history = history[:message_index]
            search_history = persisted_messages[:message_index]
            routing_message_id = existing_user_message_id
        else:
            search_history = persisted_messages
            search_intent = self._resolve_search_intent(
                prompt,
                search_history,
                timezone=timezone,
                locale=locale,
            )
            user_message = self.store.add_chat_message(
                chat_id,
                "user",
                prompt,
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                },
            )
            self.store.rename_chat_from_prompt(chat_id, prompt)
            self.db.commit()
            routing_message_id = user_message.id
        if existing_user_message_id is not None:
            search_intent = self._resolve_search_intent(
                prompt,
                search_history,
                timezone=timezone,
                locale=locale,
            )
            source_message = self.db.get(ChatMessage, existing_user_message_id)
            if source_message is not None:
                try:
                    source_metadata = json.loads(source_message.metadata_json or "{}")
                except (TypeError, ValueError):
                    source_metadata = {}
                if not isinstance(source_metadata, dict):
                    source_metadata = {}
                source_metadata["search_intent"] = search_intent.model_dump(mode="json")
                if generation_id is not None:
                    source_metadata["generation_id"] = generation_id
                source_message.metadata_json = json.dumps(source_metadata, sort_keys=True)
                self.db.commit()
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=routing_message_id,
            selected_route="pending",
            component="chat_submission",
            final_status="received",
        )

        active_rules_reply = self._active_rules_reply(prompt)
        if active_rules_reply is not None:
            assistant = persist_assistant(active_rules_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "rules_loaded": True,
                "rule_warnings": self.rule_result.get("warnings", []),
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": active_rules_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": active_rules_reply,
                "web_debug": self.last_web_debug,
            }
            return

        agent_guidance = agent_run_guidance(prompt)
        if agent_guidance is not None:
            assistant = persist_assistant(agent_guidance)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "agent_guidance": True,
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": agent_guidance}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": agent_guidance,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        self.current_turn_override = self._analyze_current_turn(
            prompt,
            chat_id=chat_id,
            message_id=routing_message_id,
            history=search_history,
        )
        context = self.build_context(prompt)
        project_context = self.project_context.context_for_prompt(prompt)
        task_context = self.task_context.context_for_prompt(prompt)
        task_context = f"{task_context}\n\n{self.file_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.code_index.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.symbol_awareness.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.test_runner.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.git_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.coding_agent.context_for_prompt(prompt)}"
        internal_intent = resolve_internal_chat_intent(prompt)
        coding_direct_reply = (
            self.coding_agent.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "coding"
            else None
        )
        if coding_direct_reply is not None:
            assistant = persist_assistant(coding_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"coding_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": coding_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": coding_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        recovery_direct_reply = (
            self.recovery.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "recovery"
            else None
        )
        if recovery_direct_reply is not None:
            assistant = persist_assistant(
                recovery_direct_reply,
                response_kind="internal_action",
                provider_name="Neo Recovery",
                route_name="recovery",
                finish_reason="stop",
                duration_ms=0,
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "recovery_context_loaded": True,
                "web_search_needed": False,
                "routing": self._routing_diagnostic(
                    chat_id,
                    prompt,
                    message_id=assistant.id,
                    selected_route="internal",
                    component="recovery_service",
                    matched_intent=f"{internal_intent.feature}:{internal_intent.action}",
                    confidence=1.0,
                    direct_feature_service="RecoveryService.answer_for_prompt",
                    provider_invoked=False,
                    response_source="recovery_service",
                    final_status="completed",
                ),
            }
            yield {"type": "chunk", "content": recovery_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": recovery_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": 0,
                "response_kind": "internal_action",
                "provider_name": "Neo Recovery",
                "route_name": "recovery",
                "finish_reason": "stop",
                "web_debug": self.last_web_debug,
            }
            return
        git_direct_reply = (
            self.git_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "git"
            else None
        )
        if git_direct_reply is not None:
            assistant = persist_assistant(git_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"git_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": git_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": git_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        test_direct_reply = (
            self.test_runner.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tests"
            else None
        )
        if test_direct_reply is not None:
            assistant = persist_assistant(test_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"test_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": test_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": test_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        task_direct_reply = (
            self.task_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tasks"
            else None
        )
        if task_direct_reply is not None:
            assistant = persist_assistant(task_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "task_context_loaded": True,
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": task_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": task_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        connector_answer = self._connector_answer(prompt, search_intent)
        if connector_answer is not None:
            reply, metadata = connector_answer
            assistant = persist_assistant(
                reply,
                **metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            connector_trace = (metadata.get("metadata") or {}).get("connector_trace")
            self.last_web_debug = {
                "web_search_needed": False,
                "connector_trace": connector_trace,
            }
            yield {"type": "chunk", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "connector_trace": connector_trace,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in metadata.items() if key != "metadata"},
            }
            return
        structured_live = self._structured_live_answer(
            prompt,
            search_intent,
            timezone=timezone,
            locale=locale,
        )
        if structured_live is not None:
            reply, metadata = structured_live
            assistant = persist_assistant(
                reply,
                **metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "web_search_needed": False,
                "structured_intent": search_intent.model_dump(mode="json"),
            }
            yield {"type": "chunk", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in metadata.items() if key != "metadata"},
            }
            return
        web_started = time.perf_counter()
        if search_intent.kind in {
            SearchIntentKind.GENERAL_WEB,
            SearchIntentKind.RELEASE_DATE,
        }:
            web_query = self._web_query_with_memory_region(prompt, context)
            yield {"type": "status", "content": "Searching trusted sources"}
            web_context = self.web_search.build_context_forced(
                web_query, hint=search_intent.resolved_query
            )
        else:
            web_query = prompt
            web_context = WebContext(query=prompt, needed=False)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            self._finalize_memory_usage()
            self._persist_memory_diagnostic(routing_message_id)
            direct_metadata = {
                "response_kind": "direct_memory",
                "provider_name": "Neo memory",
                "route_name": "memory",
                "finish_reason": "stop",
                "duration_ms": 0,
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                },
            }
            assistant = persist_assistant(
                direct_reply,
                **direct_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = self._web_debug(
                web_context, context=context, final_answer=direct_reply
            )
            yield {"type": "chunk", "content": direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in direct_metadata.items() if key != "metadata"},
            }
            return
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
            web_metadata = {
                "response_kind": "web_search",
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "evidence_unavailable",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            }
            assistant = persist_assistant(
                web_failure,
                **web_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            yield {"type": "chunk", "content": web_failure}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": web_failure,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in web_metadata.items() if key != "metadata"},
            }
            return
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            web_metadata = {
                "response_kind": "web_search",
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "stop",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            }
            assistant = persist_assistant(
                direct_web_reply,
                **web_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, direct_web_reply)
            yield {"type": "chunk", "content": direct_web_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": direct_web_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": web_metadata["duration_ms"],
                "web_debug": self.last_web_debug,
                "response_kind": web_metadata["response_kind"],
                "provider_name": web_metadata["provider_name"],
                "route_name": web_metadata["route_name"],
                "finish_reason": web_metadata["finish_reason"],
            }
            return
        messages = self.build_messages(
            prompt, history, context, web_context, project_context, task_context
        )
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=routing_message_id,
            selected_route="llm",
            component="default_chat_route",
            matched_intent=(
                f"{internal_intent.feature}:{internal_intent.action}"
                if internal_intent is not None
                else None
            ),
            confidence=1.0 if internal_intent is not None else 0.0,
            provider_invoked=True,
            response_source="provider_pending",
            final_status="streaming",
        )
        self._finalize_memory_usage()
        self._persist_memory_diagnostic(routing_message_id)
        # The provider runtime records request/stream audit state through its
        # own short-lived SQLite connection.  Persisting the diagnostic above
        # performs a flush and therefore opens a write transaction on this
        # session; leaving it open while starting the provider makes the second
        # writer wait until SQLite's timeout and fail with "database is locked".
        self.db.commit()

        raw_reply = ""
        streamed_thinking = ""
        final_metadata: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "duration_ms": None,
            "finish_reason": None,
        }
        buffer_for_validation = bool(web_context.needed)
        if buffer_for_validation:
            yield {"type": "status", "content": "Reading and validating evidence"}
        output_budget = self._num_predict(prompt, context)
        # What the client has actually received. Continuation text is accumulated but not
        # streamed, so this is the only reliable way to know whether the browser is showing
        # the whole answer or just the first call's output.
        streamed_reply = ""
        try:
            for event in self.ollama.chat_stream(
                messages,
                temperature=0.2,
                num_predict=output_budget,
            ):
                if event["type"] == "chunk":
                    raw_reply += event["content"]
                    if not buffer_for_validation:
                        streamed_reply += event["content"]
                        yield event
                    continue
                if event["type"] == "thinking":
                    streamed_thinking += event["content"]
                    yield event
                    continue
                final_metadata = event
            continuation_count = 0
            while final_metadata.get("finish_reason") == "length" and continuation_count < 2:
                continuation_count += 1
                yield {
                    "type": "status",
                    "content": "Continuing a response that reached the model limit",
                }
                # An assistant turn with no text carries nothing to continue from, and some
                # providers reject empty turns outright.
                continuation_messages = [
                    *messages,
                    *(
                        [LLMMessage(role="assistant", content=raw_reply)]
                        if raw_reply.strip()
                        else []
                    ),
                    LLMMessage(
                        role="user",
                        content=(
                            "Continue the same answer exactly where it stopped. "
                            "Do not repeat earlier text. Finish the requested answer."
                        ),
                    ),
                ]
                continuation = ""
                continuation_metadata: dict[str, Any] = {}
                # Grow the budget each round. Repeating the same cap makes a model that
                # overspends on reasoning fail identically every time.
                retry_budget = min(output_budget * (continuation_count + 1), 8192)
                for event in self.ollama.chat_stream(
                    continuation_messages,
                    temperature=0.2,
                    num_predict=retry_budget,
                ):
                    if event["type"] == "chunk":
                        continuation += str(event.get("content") or "")
                    elif event["type"] == "thinking":
                        streamed_thinking += str(event.get("content") or "")
                        yield event
                    elif event["type"] == "done":
                        continuation_metadata = event
                raw_reply = _append_without_overlap(raw_reply, continuation)
                final_metadata = _merge_generation_metadata(
                    final_metadata,
                    continuation_metadata,
                )
        except Exception as exc:
            if not web_context.citations:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
            reply = self._web_generation_fallback(prompt, web_context, exc)
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                final_answer=reply,
            )
            fallback_metadata = {
                "response_kind": "web_search",
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "provider_error",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "fallback": True,
                    "web_debug": self.last_web_debug,
                },
            }
            assistant = persist_assistant(
                reply,
                **fallback_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, reply)
            yield {"type": "replace", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in fallback_metadata.items() if key != "metadata"},
            }
            return

        incomplete = final_metadata.get("finish_reason") == "length"
        cleaned_reply = self.ollama.clean_response(raw_reply)
        if incomplete:
            reply = (
                "The selected model repeatedly reached its output limit before it "
                "could complete a reliable answer. I did not save the truncated text. "
                "Please narrow the request or increase the model output limit."
            )
            final_metadata["finish_reason"] = "incomplete_length"
        elif web_context.citations and not self._has_web_citation_marker(
            cleaned_reply,
            web_context,
        ):
            reply = self._web_generation_fallback(
                prompt,
                web_context,
                RuntimeError("generated web answer lacked citation markers"),
            )
        else:
            reply = self._with_web_citations(cleaned_reply, web_context)
        memory_extraction = {"status": "scheduled", "source_message_id": str(routing_message_id)}
        if buffer_for_validation or reply != streamed_reply:
            yield {"type": "replace", "content": reply}
        thinking = (
            final_metadata.get("thinking")
            or streamed_thinking.strip()
            or self.ollama.extract_thinking(raw_reply)
        )
        assistant = persist_assistant(
            reply,
            prompt_tokens=final_metadata.get("prompt_tokens"),
            completion_tokens=final_metadata.get("completion_tokens"),
            total_tokens=final_metadata.get("total_tokens"),
            duration_ms=final_metadata.get("duration_ms"),
            thinking=thinking,
            response_kind="web_search" if web_context.needed else "normal_chat",
            provider_name=final_metadata.get("provider_name") or final_metadata.get("provider"),
            model_name=final_metadata.get("model_name") or final_metadata.get("model"),
            route_name=final_metadata.get("route_name") or "chat",
            finish_reason=final_metadata.get("finish_reason"),
            trace_id=final_metadata.get("provider_request_id"),
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
                "memory_extraction": memory_extraction,
                "web_debug": self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                    final_answer=reply,
                ),
            },
        )
        self.db.commit()
        self.db.refresh(assistant)
        self._extract_after_response(
            prompt,
            chat_id=chat_id,
            message_id=routing_message_id,
            assistant_message_id=assistant.id,
            history=search_history,
            transport="stream",
        )
        if after_reply is not None:
            after_reply(prompt, reply)
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
        )
        self.last_web_debug["routing"] = self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=assistant.id,
            selected_route="llm",
            component="default_chat_route",
            matched_intent=(
                f"{internal_intent.feature}:{internal_intent.action}"
                if internal_intent is not None
                else None
            ),
            confidence=1.0 if internal_intent is not None else 0.0,
            provider_invoked=True,
            provider=final_metadata.get("provider")
            or getattr(self.ollama, "last_metadata", {}).get("provider"),
            model=final_metadata.get("model")
            or getattr(self.ollama, "last_metadata", {}).get("model"),
            fallback_reason=("provider_fallback" if final_metadata.get("fallback_used") else None),
            response_source="provider",
            final_status="completed",
        )
        yield {
            "type": "done",
            "message_id": assistant.id,
            "reply": reply,
            "thinking": thinking,
            "prompt_tokens": final_metadata.get("prompt_tokens"),
            "completion_tokens": final_metadata.get("completion_tokens"),
            "total_tokens": final_metadata.get("total_tokens"),
            "duration_ms": final_metadata.get("duration_ms"),
            "response_kind": "web_search" if web_context.needed else "normal_chat",
            "provider_name": final_metadata.get("provider_name") or final_metadata.get("provider"),
            "model_name": final_metadata.get("model_name") or final_metadata.get("model"),
            "route_name": final_metadata.get("route_name") or "chat",
            "finish_reason": final_metadata.get("finish_reason"),
            "trace_id": final_metadata.get("provider_request_id"),
            "web_debug": self.last_web_debug,
        }

    def _active_rules_reply(self, prompt: str) -> str | None:
        if not re.search(
            r"\b(which|what|show|list).{0,30}\b(active |applied )?rules\b", prompt, re.I
        ):
            return None
        profiles = self.rule_result.get("applied_profiles", [])
        rules = self.rule_result.get("resolved_rules", {})
        lines = ["Active rules for this chat:"]
        if profiles:
            lines.extend(f"- {item['name']} ({item['scope_type']})" for item in profiles)
        else:
            lines.append("- Built-in safety rules only")
        guidance = [*rules.get("instructions", []), *rules.get("coding_style", [])]
        if guidance:
            lines.append("Guidance:")
            lines.extend(f"- {item}" for item in guidance)
        forbidden = rules.get("forbidden_paths", [])
        if forbidden:
            lines.append("Forbidden paths: " + ", ".join(forbidden))
        warnings = self.rule_result.get("warnings", [])
        if warnings:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in warnings)
        lines.append("Rules are guidance only and cannot grant permissions or disable safety.")
        return "\n".join(lines)

    def _extraction_request(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
        transport: str,
        mode: ExtractionMode,
    ) -> ExtractionRequest:
        # Extraction reads a bounded window, so a very long message is trimmed to fit
        # rather than rejected. The hash below covers the trimmed text, keeping the
        # recorded provenance aligned with what extraction actually saw.
        extraction_message = prompt[:EXTRACTION_WINDOW_MAX_CHARS]
        supporting = tuple(
            TrustedConversationMessage(
                message_id=str(item.id),
                role=ConversationRole(item.role),
                content=item.content,
            )
            for item in history[-12:]
            if item.role in {"user", "assistant"} and item.id != message_id
        )
        total = len(extraction_message)
        bounded: list[TrustedConversationMessage] = []
        for item in reversed(supporting):
            if total + len(item.content) > EXTRACTION_WINDOW_MAX_CHARS:
                continue
            bounded.append(item)
            total += len(item.content)
        bounded.reverse()
        return ExtractionRequest(
            request_id=f"chat:{chat_id}:{message_id}:{mode.value}",
            owner_id=self.memory_runtime.execution.owner_id,
            conversation_id=str(chat_id),
            active_project_id=self.active_project_id,
            active_project_name=self.active_project_name,
            session_id=f"profile:{self.memory_runtime.execution.profile_id}",
            message_id=str(message_id),
            user_message=extraction_message,
            supporting_window=tuple(bounded),
            explicit_memory_intent=bool(
                re.search(r"\b(?:remember|save|forget|correct|changed my mind)\b", prompt, re.I)
            ),
            incognito=self.memory_runtime.execution.is_incognito,
            memory_enabled=self.memory_enabled,
            mode=mode,
            source_content_hash=ExtractionRequest.content_hash(extraction_message),
        )

    def _run_extraction(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
        mode: ExtractionMode,
        transport: str,
    ):
        if not self.memory_enabled or self.memory_runtime is None:
            return None
        request = self._extraction_request(
            prompt,
            chat_id=chat_id,
            message_id=message_id,
            history=history,
            transport=transport,
            mode=mode,
        )
        context = self.memory_runtime.context(
            source_kind=SourceKind.CHAT_MESSAGE,
            source_id="chat",
            request_id=request.request_id,
            session_id=request.session_id,
            conversation_id=request.conversation_id,
            message_id=request.message_id,
        )
        return self.memory_runtime.extraction.process(request, context, transport=transport)

    def _analyze_current_turn(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
    ) -> CurrentTurnOverride | None:
        if not re.search(
            r"\b(?:changed my mind|instead|no longer|not anymore|forget|remove|correct)\b",
            prompt,
            re.I,
        ):
            return None
        try:
            result = self._run_extraction(
                prompt,
                chat_id=chat_id,
                message_id=message_id,
                history=history,
                mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
                transport="sync",
            )
            # "forget" matches the correction trigger, so a retraction is applied
            # here, in the foreground, and never appears in the post-turn result
            # that writes the message metadata.  Carry the count across, or the
            # turn is not recognised as a forget and its text keeps being
            # replayed into later prompts.
            self._foreground_forgotten = sum(
                getattr(decision, "outcome", None) in _MEMORY_FORGET_OUTCOMES
                for decision in (getattr(result, "decisions", ()) or ())
            )
            return result.current_turn_override if result is not None else None
        except Exception:
            return None

    def _extract_after_response(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        assistant_message_id: int,
        history: list[ChatMessage],
        transport: str,
    ) -> dict[str, object]:
        if not self.memory_enabled or self.memory_runtime is None:
            return {"status": "disabled"}
        if not turn_may_contain_memory(prompt):
            # A turn made only of questions states nothing to remember, and
            # running extraction on it spent a local model call re-asserting
            # facts read out of the supporting window as new candidates.
            return {"status": "skipped", "reason": "no_memory_bearing_statement"}
        database = self.db.get_bind()
        # Snapshot every ORM-backed input while the chat session is still owned
        # by this worker. Background threads must never dereference ChatMessage
        # instances while the parent session is committing/closing.
        try:
            request = self._extraction_request(
                prompt,
                chat_id=chat_id,
                message_id=message_id,
                history=history,
                transport=transport,
                mode=ExtractionMode.POST_TURN_AUTOMATIC,
            )
            context = self.memory_runtime.context(
                source_kind=SourceKind.CHAT_MESSAGE,
                source_id="chat",
                request_id=request.request_id,
                session_id=request.session_id,
                conversation_id=request.conversation_id,
                message_id=request.message_id,
            )
        except Exception as exc:
            _ROUTING_LOG.exception(
                "memory_extraction_snapshot_failed chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self._record_memory_extraction_failure(
                database,
                assistant_message_id=assistant_message_id,
                error=exc,
            )
            return {"status": "failed", "source_message_id": str(message_id)}

        def run() -> None:
            try:
                result = self.memory_runtime.extraction.process(
                    request,
                    context,
                    transport=transport,
                )
                self._record_memory_extraction_result(
                    database,
                    assistant_message_id=assistant_message_id,
                    result=result,
                )
            except Exception as exc:
                _ROUTING_LOG.exception(
                    "memory_extraction_failed chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                try:
                    self._record_memory_extraction_failure(
                        database,
                        assistant_message_id=assistant_message_id,
                        error=exc,
                    )
                except Exception:
                    _ROUTING_LOG.exception(
                        "memory_extraction_failure_status_write_failed "
                        "chat_id=%s message_id=%s",
                        chat_id,
                        message_id,
                    )
            finally:
                # Indexing ran only on the success path, so a turn that extracted
                # nothing — or failed — left the outbox untouched.  Because the queue
                # is drained by nothing else, one such turn stranded every earlier
                # write: profiles still hold `pending` events whose records were
                # written weeks ago and were never searchable.  Draining here means
                # any turn that reaches extraction clears the backlog, whatever this
                # turn itself produced.
                self._build_memory_indexes()

        Thread(
            target=run,
            name=f"memory-extraction-{chat_id}-{message_id}",
            daemon=True,
        ).start()
        return {"status": "scheduled", "source_message_id": str(message_id)}

    @staticmethod
    def _forgetting_turn(message: ChatMessage) -> bool:
        if message.role != "assistant":
            return False
        try:
            metadata = json.loads(message.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict):
            return False
        extraction = metadata.get("memory_extraction")
        if not isinstance(extraction, dict):
            return False
        return bool(extraction.get("forgotten_memories"))

    @classmethod
    def _history_turns(cls, messages: list[ChatMessage]) -> list[ChatTurn]:
        """Replay history with the turns that removed a memory blanked out.

        A delete only means something if the deleted value stops coming back.
        Purging the record is not enough while the request that named it, and the
        reply confirming it, are still replayed into every later prompt: the
        model reads the value there and repeats it, which is how "I no longer
        remember that you use a Garmin watch" got produced.  Both halves of the
        exchange go, since either one alone restates the fact.
        """

        redacted: set[int] = set()
        for index, message in enumerate(messages):
            if not cls._forgetting_turn(message):
                continue
            redacted.add(index)
            for previous in range(index - 1, -1, -1):
                if messages[previous].role == "user":
                    redacted.add(previous)
                    break
        return [
            ChatTurn(
                role=message.role,
                content=(
                    _FORGOTTEN_TURN_PLACEHOLDER if index in redacted else message.content
                ),
            )
            for index, message in enumerate(messages)
        ]

    def _record_memory_extraction_result(
        self,
        database,
        *,
        assistant_message_id: int,
        result,
    ) -> None:
        """Attach completed extraction status to View Thinking, never to the reply."""

        decisions = tuple(getattr(result, "decisions", ()) or ())
        saved = sum(
            getattr(decision, "outcome", None) in _MEMORY_SAVE_OUTCOMES
            for decision in decisions
        )
        factory = sessionmaker(bind=database, autoflush=False, expire_on_commit=False, future=True)
        with factory.begin() as database_session:
            message = database_session.get(ChatMessage, assistant_message_id)
            if message is None or message.role != "assistant":
                return
            try:
                metadata = json.loads(message.metadata_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            forgotten = sum(
                getattr(decision, "outcome", None) in _MEMORY_FORGET_OUTCOMES
                for decision in decisions
            ) + getattr(self, "_foreground_forgotten", 0)
            metadata["memory_extraction"] = {
                "status": "completed",
                "saved_durable_memories": saved,
                "forgotten_memories": forgotten,
                **self._extraction_reason_metadata(result),
            }
            message.metadata_json = json.dumps(metadata, sort_keys=True)
            if saved:
                summary = (
                    f"Saved {saved} durable memor{'y' if saved == 1 else 'ies'} "
                    "after extraction and review."
                )
                current = (message.thinking or "").strip()
                message.thinking = f"{current}\n\n{summary}" if current else summary

    @staticmethod
    def _extraction_reason_metadata(result) -> dict[str, object]:
        """Summarise why a completed extraction stored nothing.

        ``saved_durable_memories: 0`` reads identically whether the turn held no
        facts, the model returned an empty response, its output failed schema
        validation, or policy rejected every candidate.  The coordinator already
        builds an ``ExtractionDiagnostic`` carrying that answer, then files it in an
        in-memory deque that is rebuilt each turn and never read — so a fully broken
        extractor looked exactly like a quiet one for weeks.

        Only bounded codes and counts are copied here, never model output or user
        text, because this lands in chat metadata the user can read.
        """

        diagnostic = getattr(result, "diagnostic", None)
        summary = getattr(result, "model_summary", None)
        if diagnostic is None:
            return {}
        reasons = tuple(getattr(diagnostic, "reason_codes", ()) or ())
        schema_errors = tuple(getattr(diagnostic, "schema_error_codes", ()) or ())
        status = getattr(result, "status", None)
        payload: dict[str, object] = {
            "extraction_status": getattr(status, "value", status),
            "parse_outcome": getattr(diagnostic, "parse_outcome", None),
            "proposal_count": getattr(diagnostic, "proposal_count", None),
            "accepted_count": getattr(diagnostic, "accepted_count", None),
            "rejected_count": getattr(diagnostic, "rejected_count", None),
            "review_count": getattr(diagnostic, "review_count", None),
        }
        if reasons:
            payload["reason_codes"] = list(reasons[:20])
        if schema_errors:
            payload["schema_error_codes"] = list(schema_errors[:20])
        if summary is not None:
            payload["model_called"] = getattr(summary, "called", None)
            payload["model_assertion_count"] = getattr(summary, "assertion_count", None)
            payload["model_exclusion_count"] = getattr(summary, "exclusion_count", None)
        return {key: value for key, value in payload.items() if value is not None}

    def _build_memory_indexes(self) -> None:
        """Index memories just written so semantic recall can actually find them.

        Extraction only enqueues indexing work; nothing in the deployed app
        drained that queue, so the vector index stayed empty and recall fell
        back to literal word overlap.  This runs on the post-turn extraction
        thread, after the response has already been delivered, and never raises:
        an indexing failure must not lose a memory that was stored correctly.
        """

        if not self.memory_enabled or self.memory_runtime is None:
            return
        execution = self.memory_runtime.execution
        engine = build_engine(execution.database_url)
        try:
            drain_memory_outbox(
                engine,
                owner_id=execution.owner_id,
                database_identity=execution.database_identity,
                flags=self.memory_runtime.settings,
                settings=self.settings,
            )
        except Exception:
            _ROUTING_LOG.exception("memory_index_build_failed")
        finally:
            engine.dispose()

    @staticmethod
    def _record_memory_extraction_failure(
        database,
        *,
        assistant_message_id: int,
        error: Exception,
    ) -> None:
        """Persist a safe failure code so extraction never remains 'scheduled'."""

        factory = sessionmaker(bind=database, autoflush=False, expire_on_commit=False, future=True)
        with factory.begin() as database_session:
            message = database_session.get(ChatMessage, assistant_message_id)
            if message is None or message.role != "assistant":
                return
            try:
                metadata = json.loads(message.metadata_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["memory_extraction"] = {
                "status": "failed",
                "error_code": type(error).__name__,
            }
            message.metadata_json = json.dumps(metadata, sort_keys=True)

    def _direct_reply(self, prompt: str) -> str | None:
        if not self.memory_enabled or not self.memory_direct_answers_enabled:
            return None
        reply = self.direct_answers.answer(
            prompt,
            context=self._memory_query_context(prompt),
        )
        self.last_memory_selection = self.direct_answers.last_selection
        return reply

    def _resolve_search_intent(
        self,
        prompt: str,
        prior_messages: list[ChatMessage],
        *,
        timezone: str | None,
        locale: str | None,
    ) -> ResolvedSearchIntent:
        previous = self._previous_search_intent(prior_messages)
        intent = self.search_intent_resolver.resolve_with_model(
            prompt,
            llm=self.ollama,
            previous=previous,
            timezone=timezone,
            locale=locale,
        )
        self.last_search_intent = intent
        return intent

    def _previous_search_intent(
        self,
        messages: list[ChatMessage],
    ) -> ResolvedSearchIntent | None:
        for message in reversed(messages):
            if message.role != "user":
                continue
            if message.metadata_json:
                try:
                    payload = json.loads(message.metadata_json)
                    return ResolvedSearchIntent.model_validate(payload["search_intent"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    pass
            # Backward compatibility for the most recent turn created before
            # structured intent metadata existed.
            return resolve_search_intent(message.content)
        return None

    def _profile_timezone(self) -> str | None:
        return None

    def _structured_live_answer(
        self,
        prompt: str,
        intent: ResolvedSearchIntent,
        *,
        timezone: str | None,
        locale: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        started = time.perf_counter()
        if intent.kind == SearchIntentKind.LOCAL_DATETIME:
            result = local_datetime_answer(
                prompt,
                browser_timezone=timezone,
                profile_timezone=self._profile_timezone(),
                fallback_timezone=self.settings.default_timezone,
                locale=locale,
            )
            return result.answer, {
                "response_kind": "local_datetime",
                "provider_name": "Neo local clock",
                "route_name": "local_datetime",
                "finish_reason": "stop",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {
                    "timezone": result.timezone,
                    "locale": result.locale,
                    "used_web": False,
                    "search_intent": intent.model_dump(mode="json"),
                },
            }

        try:
            if intent.kind == SearchIntentKind.CURRENCY:
                if not intent.from_currency or not intent.to_currency or intent.amount is None:
                    return None
                quote = FrankfurterClient().convert(
                    intent.amount,
                    intent.from_currency,
                    intent.to_currency,
                )
                reply = (
                    f"{quote.amount} {quote.from_currency} is "
                    f"{quote.converted_amount:,.2f} {quote.to_currency}. "
                    f"The rate is 1 {quote.from_currency} = {quote.rate} "
                    f"{quote.to_currency}, dated {quote.reference_date}, from "
                    f"{quote.provider}.\n\nSource: {quote.source_url}"
                )
                return reply, {
                    "response_kind": "structured_currency",
                    "provider_name": quote.provider,
                    "route_name": "currency",
                    "finish_reason": "stop",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "metadata": {
                        "quote": quote.model_dump(mode="json"),
                        "search_intent": intent.model_dump(mode="json"),
                    },
                }

            if intent.kind == SearchIntentKind.WEATHER:
                if not intent.location:
                    return None
                weather = OpenMeteoClient()
                if intent.date == "tomorrow":
                    report = weather.forecast_weather(
                        intent.location,
                        day="tomorrow",
                        locale=locale or "en",
                        timezone=timezone or "auto",
                    )
                    place = ", ".join(item for item in (report.location, report.country) if item)
                    rain = (
                        f" The maximum precipitation probability is "
                        f"{report.precipitation_probability_max}%."
                        if report.precipitation_probability_max is not None
                        else ""
                    )
                    reply = (
                        f"The forecast for {place} on {report.forecast_date} is "
                        f"{report.condition}, with a low of {report.temperature_min_c}°C "
                        f"and a high of {report.temperature_max_c}°C.{rain} "
                        f"Provided by {report.provider}.\n\nSource: {report.source_url}"
                    )
                else:
                    report = weather.current_weather(
                        intent.location,
                        locale=locale or "en",
                        timezone=timezone or "auto",
                    )
                    apparent = (
                        f", feels like {report.apparent_temperature_c}°C"
                        if report.apparent_temperature_c is not None
                        else ""
                    )
                    place = ", ".join(item for item in (report.location, report.country) if item)
                    reply = (
                        f"In {place}, it is {report.temperature_c}°C{apparent} with "
                        f"{report.condition}. This observation is from "
                        f"{report.observed_at} ({report.timezone}), provided by "
                        f"{report.provider}.\n\nSource: {report.source_url}"
                    )
                return reply, {
                    "response_kind": "structured_weather",
                    "provider_name": report.provider,
                    "route_name": "weather",
                    "finish_reason": "stop",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "metadata": {
                        "weather": report.model_dump(mode="json"),
                        "search_intent": intent.model_dump(mode="json"),
                    },
                }
        except LiveDataError as exc:
            provider = "Frankfurter" if intent.kind == SearchIntentKind.CURRENCY else "Open-Meteo"
            return str(exc), {
                "response_kind": (
                    "structured_currency"
                    if intent.kind == SearchIntentKind.CURRENCY
                    else "structured_weather"
                ),
                "provider_name": provider,
                "route_name": intent.kind.value,
                "finish_reason": "provider_error",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {
                    "search_intent": intent.model_dump(mode="json"),
                    "error": str(exc),
                },
            }
        return None

    def _connector_answer(
        self,
        prompt: str,
        intent: ResolvedSearchIntent,
    ) -> tuple[str, dict[str, Any]] | None:
        """Invoke only a uniquely selected read, or queue an explicit write."""

        started = time.perf_counter()
        explicit = intent.kind == SearchIntentKind.CONNECTOR_TOOL
        if not explicit and _CONNECTOR_INFORMATIONAL_REQUEST.match(prompt):
            return None
        service = ToolsService()
        tool = None
        explicit_tool = re.search(
            r"\btool\s*[:=]\s*(?P<id>[A-Za-z0-9._-]{1,120})\b",
            prompt,
            re.IGNORECASE,
        )
        if explicit_tool is not None:
            wanted_id = explicit_tool.group("id")
            tool = next(
                (
                    item
                    for item in service.list_tools(include_disabled=False)
                    if item.id == wanted_id
                ),
                None,
            )
            if tool is None:
                return self._connector_clarification(
                    f"I could not find an enabled connector tool named {wanted_id}.",
                    intent,
                    started,
                )
        else:
            tool = service.select_enabled_read_tool(prompt, intent=prompt)
            if tool is None and explicit:
                normalized_prompt = re.sub(r"[^a-z0-9]+", " ", prompt.lower())
                named = []
                for candidate in service.list_tools(include_disabled=False):
                    if candidate.built_in:
                        continue
                    names = {
                        re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
                        for value in (candidate.name, candidate.display_name or "")
                        if value
                    }
                    if any(name and f" {name} " in f" {normalized_prompt} " for name in names):
                        named.append(candidate)
                if len(named) == 1:
                    tool = named[0]

        if tool is None:
            if explicit:
                return self._connector_clarification(
                    "I could not identify one connector with enough confidence. "
                    "Please name the connector tool and provide its required inputs.",
                    intent,
                    started,
                )
            return None

        arguments, missing = self._connector_arguments(prompt, tool.input_schema)
        if missing:
            if explicit:
                return self._connector_clarification(
                    "Before I call that connector, provide: " + ", ".join(missing) + ".",
                    intent,
                    started,
                    tool_id=tool.id,
                )
            return None

        try:
            invocation = service.invoke_connector(
                tool_id=tool.id,
                arguments=arguments,
                capability=prompt,
                intent=prompt,
            )
        except (ToolValidationError, ValueError) as exc:
            return self._connector_clarification(
                f"The connector request was rejected safely: {exc}",
                intent,
                started,
                tool_id=tool.id,
            )

        status = invocation.get("status")
        if invocation.get("approval_required"):
            reply = (
                f"{tool.display_name or tool.name} is a write-capable connector. "
                "I queued this call for your explicit approval in Tools & Skills; "
                "nothing has been changed yet."
            )
            finish_reason = "approval_required"
        elif status == "completed":
            result = invocation.get("result")
            serialized = json.dumps(result, indent=2, sort_keys=True, default=str)
            reply = (
                f"{tool.display_name or tool.name} returned this untrusted external data. "
                f"Neo did not treat its contents as instructions:\n\n{serialized}"
            )
            finish_reason = "stop"
        else:
            error = invocation.get("error") or "The connector did not complete."
            reply = f"{tool.display_name or tool.name} could not complete: {error}"
            finish_reason = str(status or "connector_error")

        return reply, {
            "response_kind": "connector",
            "provider_name": str(
                (invocation.get("provenance") or {}).get("connector_name")
                or tool.display_name
                or tool.name
            ),
            "route_name": "connector",
            "finish_reason": finish_reason,
            "trace_id": invocation.get("call_id"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "metadata": {
                "connector_trace": invocation,
                "search_intent": intent.model_dump(mode="json"),
            },
        }

    def _connector_clarification(
        self,
        reply: str,
        intent: ResolvedSearchIntent,
        started: float,
        *,
        tool_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        return reply, {
            "response_kind": "connector",
            "provider_name": "Neo connector router",
            "route_name": "connector",
            "finish_reason": "clarification_required",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "metadata": {
                "connector_trace": {
                    "status": "clarification_required",
                    "tool_id": tool_id,
                },
                "search_intent": intent.model_dump(mode="json"),
            },
        }

    def _connector_arguments(
        self,
        prompt: str,
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        properties = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        arguments: dict[str, Any] = {}

        object_match = re.search(r"\{.*\}", prompt, re.DOTALL)
        if object_match is not None:
            try:
                parsed = json.loads(object_match.group(0))
                if isinstance(parsed, dict):
                    arguments.update(parsed)
            except json.JSONDecodeError:
                pass

        for key, specification in properties.items():
            if key in arguments:
                continue
            match = re.search(
                rf"\b{re.escape(str(key))}\s*(?:=|:)\s*"
                r"(?P<value>\"[^\"]*\"|'[^']*'|[^,;]+)",
                prompt,
                re.IGNORECASE,
            )
            if match is not None:
                raw_value = match.group("value").strip().strip("\"'")
                arguments[str(key)] = self._coerce_connector_value(
                    raw_value,
                    specification,
                )

        missing = [str(key) for key in required if key not in arguments]
        if len(missing) == 1:
            key = missing[0]
            specification = properties.get(key) or {}
            if specification.get("type") in {None, "string"}:
                contextual = re.search(
                    r"\b(?:for|with|about)\s+(?P<value>.+)$",
                    prompt,
                    re.IGNORECASE,
                )
                if contextual is not None:
                    arguments[key] = contextual.group("value").strip(" .?!")
                elif key.lower() in {"input", "prompt", "q", "query", "text"}:
                    arguments[key] = prompt
        return arguments, [str(key) for key in required if key not in arguments]

    @staticmethod
    def _coerce_connector_value(value: str, specification: dict[str, Any]) -> Any:
        expected = specification.get("type")
        if expected == "integer":
            try:
                return int(value)
            except ValueError:
                return value
        if expected == "number":
            try:
                return float(value)
            except ValueError:
                return value
        if expected == "boolean":
            normalized = value.lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        return value

    def _routing_diagnostic(
        self,
        chat_id: int,
        prompt: str,
        *,
        message_id: int | None,
        selected_route: str,
        component: str,
        matched_intent: str | None = None,
        confidence: float | None = None,
        fuzzy_candidate: str | None = None,
        direct_feature_service: str | None = None,
        provider_invoked: bool = False,
        provider: str | None = None,
        model: str | None = None,
        fallback_reason: str | None = None,
        response_source: str | None = None,
        final_status: str | None = None,
    ) -> dict[str, Any]:
        """Emit a privacy-safe trace for the production chat-routing decision."""

        normalized = re.sub(r"\s+", " ", (prompt or "").strip())
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "normalized_input_length": len(normalized),
            "input_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            "selected_route": selected_route,
            "component": component,
            "matched_intent": matched_intent,
            "confidence": confidence,
            "fuzzy_candidate": fuzzy_candidate,
            "direct_feature_service": direct_feature_service,
            "provider_invoked": provider_invoked,
            "provider": provider,
            "model": model,
            "fallback_reason": fallback_reason,
            "response_source": response_source,
            "final_status": final_status,
        }
        self.last_routing_debug = payload
        _ROUTING_LOG.warning("chat_routing=%s", json.dumps(payload, sort_keys=True))
        return payload

    def _web_query_with_memory_region(self, query: str, context: ContextPackage) -> str:
        """Return the query unchanged; stored memory never reaches a search engine.

        This used to append the user's country, read out of memory, to release
        date lookups.  It sharpened a narrow class of query at the cost of
        sending a personal fact to a third-party search provider on a turn where
        the user asked only about a release date.  Memory is stored locally so it
        stays the user's; a small ranking gain does not justify spending it.
        """

        return query

    def _is_release_date_query(self, query: str) -> bool:
        return resolve_search_intent(query).kind == SearchIntentKind.RELEASE_DATE

    def _compact_context(self, context: ContextPackage) -> str:
        lines: list[str] = []
        lines.extend(f"profile: {item.key} = {item.value}" for item in context.profile)
        lines.extend(
            f"preference: {item.category} = {item.value} (importance {item.importance})"
            for item in context.preferences
        )
        lines.extend(
            f"goal: {item.goal}" + (f" - {item.description}" if item.description else "")
            for item in context.goals
        )
        lines.extend(
            f"project: {item.name}" + (f" - {item.description}" if item.description else "")
            for item in context.projects
        )
        lines.extend(f"memory #{item.id}: {item.memory_text}" for item in context.relevant_memories)
        lines.extend(f"event: {item.event}" for item in context.events)
        if not lines:
            return "No relevant personal memory loaded."
        return "\n".join(lines[:18])

    def _compact_web_context(self, web_context: WebContext | None) -> str:
        if web_context is None or not web_context.needed:
            return "No web context loaded."
        if web_context.warning and not web_context.context_text:
            return f"Web search attempted but unavailable: {web_context.warning}"
        if not web_context.context_text:
            return "Web search ran, but no usable page text was fetched."
        return web_context.context_text

    def _web_failure_reply(self, web_context: WebContext | None) -> str | None:
        if web_context is None or not web_context.needed:
            return None
        if web_context.citations:
            return None
        reason = web_context.warning or "No fetched web sources were available."
        if reason == GROUNDING_FAILURE_MESSAGE:
            return GROUNDING_FAILURE_MESSAGE
        if reason == EXTRACTION_FAILURE_MESSAGE:
            return EXTRACTION_FAILURE_MESSAGE
        return f"I tried to search the web, but could not build a cited answer: {reason}"

    def _with_web_citations(self, reply: str, web_context: WebContext | None) -> str:
        body = _strip_llm_sources_block(reply)
        if web_context is None or not web_context.needed:
            # No web search was involved, so a link here is ordinary content — a repo or
            # documentation URL the user asked for — not a citation claiming evidence.
            # Deleting those corrupted normal answers, so they are left alone.
            return self._strip_orphan_citation_markers(body)
        if not web_context.citations:
            # A search was attempted but produced nothing citable, so any source URL the
            # model offers cannot be backed by evidence and is dropped.
            body = self._strip_orphan_citation_markers(body)
            return _strip_fabricated_urls(body, set())
        valid_urls = {citation.url for citation in web_context.citations}
        body = _strip_fabricated_urls(body, valid_urls)
        citations = self._citations_for_body(body, web_context)
        if not citations:
            return self._strip_orphan_citation_markers(body)
        return f"{body.strip()}\n\n{citations}"

    def _citations_for_body(self, body: str, web_context: WebContext) -> str:
        """Format only the sources the answer actually cites.

        Listing every fetched page implies the answer rests on all of them. It
        also reads as padding: a four-line answer citing [1] and [2] would still
        print a third source it never used. Indices are left as they are rather
        than renumbered, so the markers already written in the body stay valid
        and a gap simply means that source went uncited.
        """
        referenced = _referenced_citation_indices(body)
        cited = [citation for citation in web_context.citations if citation.index in referenced]
        # An uncited answer should not silently lose its sources; callers guard
        # this path with _has_web_citation_marker, so this is belt and braces.
        return self.citation_formatter.format_citations(cited or web_context.citations)

    def _strip_orphan_citation_markers(self, reply: str) -> str:
        cleaned = re.sub(r"\s*\[(?:\d{1,2})(?:\s*,\s*\d{1,2})*\]", "", reply)
        cleaned = re.sub(r" {2,}", " ", cleaned)
        return cleaned.strip()

    def _has_web_citation_marker(self, reply: str, web_context: WebContext) -> bool:
        validation = validate_citation_markers(
            reply,
            web_context.citations,
            supported_indices={chunk.source_index for chunk in web_context.evidence_chunks},
            require_marker=True,
        )
        return validation.valid

    def _web_generation_fallback(
        self, prompt: str, web_context: WebContext, error: Exception
    ) -> str:
        grounded_prompt = web_context.query or prompt
        price_clarification = _price_query_clarification(grounded_prompt)
        if price_clarification is not None:
            return price_clarification
        if web_context.answer_mode == "fact_lookup":
            release_answer = self._verified_release_answer(grounded_prompt, web_context)
            if release_answer is not None:
                return release_answer
            fact = run_extractors(grounded_prompt, web_context.evidence_chunks)
            if fact is not None:
                answer = self._format_fact_answer(grounded_prompt, fact)
                citations = self.citation_formatter.format_citations(
                    [
                        citation
                        for citation in web_context.citations
                        if citation.index == fact.source_index
                    ]
                )
                return f"{answer}\n\n{citations}" if citations else answer
            return (
                "I searched the web but could not find sufficiently reliable "
                "evidence to answer that."
            )
        if web_context.answer_mode in {"news_summary", "overview"}:
            return self._evidence_digest(grounded_prompt, web_context)
        return (
            "I searched the web but could not find sufficiently reliable evidence to answer that."
        )

    def _direct_web_reply(self, prompt: str, web_context: WebContext) -> str | None:
        if not web_context.needed or not web_context.evidence_chunks or not web_context.citations:
            return None
        price_clarification = _price_query_clarification(prompt)
        if price_clarification is not None:
            return price_clarification
        if web_context.answer_mode == "fact_lookup":
            release_answer = self._verified_release_answer(prompt, web_context)
            if release_answer is not None:
                return release_answer
            fact = run_extractors(prompt, web_context.evidence_chunks)
            if fact is not None:
                answer = self._format_fact_answer(prompt, fact)
                citations = self.citation_formatter.format_citations(
                    [
                        citation
                        for citation in web_context.citations
                        if citation.index == fact.source_index
                    ]
                )
                return f"{answer}\n\n{citations}" if citations else answer
            planned_match = self._planned_seasons_from_evidence(prompt, web_context)
            if planned_match is not None:
                planned, source_index = planned_match
                answer = (
                    f"Robert Kirkman has described the plan as {planned} seasons [{source_index}]."
                )
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            if re.search(
                r"\b(weather|forecast|temperature|how hot|how cold)\b",
                prompt,
                re.IGNORECASE,
            ):
                return (
                    "I found weather sources, but could not extract a reliable current "
                    "temperature from them. Please try again or specify the city and date."
                )
            return None
        # news_summary and overview deliberately fall through to the model.
        # Rendering the evidence chunks straight out reads as extracted snippets
        # rather than an answer; summarising them is what the model is for. The
        # deterministic rendering is kept in _evidence_digest and used by
        # _web_generation_fallback when generation fails or skips its citations,
        # which is the same shape fact_lookup already relies on.
        return None

    def _evidence_digest(self, prompt: str, web_context: WebContext) -> str:
        """Render evidence chunks directly, for when generation cannot be used."""
        clusters = _cluster_evidence_by_entity(prompt, web_context.evidence_chunks)
        if len(clusters) > 1:
            lines = ["I found results for multiple topics:"]
            for cluster_label, cluster_chunks in clusters.items():
                lines.append(f"\n**{cluster_label}:**")
                for chunk in cluster_chunks[:2]:
                    lines.append(
                        f"- {_clean_snippet_text(chunk.text[:350])} [{chunk.source_index}]"
                    )
        else:
            lines = [
                "Here are the source-backed updates I found:"
                if web_context.answer_mode == "news_summary"
                else "Here is what the sources say:"
            ]
            chunks = sorted(
                web_context.evidence_chunks,
                key=lambda chunk: (self._source_priority(chunk.source_url), -chunk.relevance_score),
            )
            for chunk in chunks[:4]:
                lines.append(f"- {_clean_snippet_text(chunk.text[:420])} [{chunk.source_index}]")
        citations = self._citations_for_body("\n".join(lines), web_context)
        if citations:
            lines.extend(["", citations])
        return "\n".join(lines)

    def _format_fact_answer(self, prompt: str, fact: FactResult) -> str:
        """Format a structured fact extraction result into a user-facing answer."""
        lowered = prompt.lower()
        if re.search(r"\b(season|seasons)\b", lowered) and not re.search(
            r"\b(episode|episodes)\b", lowered
        ):
            return f"The series has {fact.answer} [{fact.source_index}]."
        if re.search(r"\b(episode|episodes)\b", lowered):
            return f"The listed episode count is {fact.answer} [{fact.source_index}]."
        if re.search(r"\b(champion|ranking|rankings|rated|rating|highest rated)\b", lowered):
            if "champion" in fact.match_reason:
                return f"The current world chess champion is {fact.answer} [{fact.source_index}]."
            return f"The top-rated player is {fact.answer} [{fact.source_index}]."
        if re.search(r"\b(version|latest)\b", lowered) and re.search(
            r"\b(next\.?js|react|node|npm|python)\b", lowered
        ):
            return f"The latest version is {fact.answer} [{fact.source_index}]."
        if re.search(r"\b(price|cost|how much)\b", lowered):
            region = self._target_region(prompt)
            prefix = "In India, the" if region == "india" else "The"
            return f"{prefix} listed price is {fact.answer} [{fact.source_index}]."
        if re.search(r"\b(release|released|premiere|when|coming out|date)\b", lowered):
            region = self._target_region(prompt)
            prefix = (
                "In India, the listed release date is"
                if region == "india"
                else "The listed release date is"
            )
            return f"{prefix} {fact.answer} [{fact.source_index}]."
        return f"{fact.answer} [{fact.source_index}]."

    def _verified_release_answer(
        self,
        prompt: str,
        web_context: WebContext,
    ) -> str | None:
        """Return a release answer only through the shared verified extractor."""

        if not self._is_release_date_query(prompt):
            return None
        fact = extract_release_date(prompt, web_context.evidence_chunks)
        if fact is None:
            answer = (
                "The fetched sources did not provide a release date that passed "
                "verification, so I cannot report a verified date yet."
            )
            citations = self.citation_formatter.format_citations(web_context.citations)
            return f"{answer}\n\n{citations}" if citations else answer

        prefix = (
            "In India, the verified release date is"
            if self._target_region(prompt) == "india"
            else "The verified release date is"
        )
        answer = f"{prefix} {fact.answer} [{fact.source_index}]."
        citations = self.citation_formatter.format_citations(
            [citation for citation in web_context.citations if citation.index == fact.source_index]
        )
        return f"{answer}\n\n{citations}" if citations else answer

    def _source_priority(self, url: str) -> int:
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        official_domains = {
            "bcci.tv",
            "icc-cricket.com",
            "marvel.com",
            "nextjs.org",
            "openai.com",
            "primevideo.com",
            "registry.npmjs.org",
            "sonypictures.com",
            "sonypictures.in",
            "x.ai",
        }
        return 0 if domain in official_domains else 1

    def _planned_seasons_from_evidence(
        self, prompt: str, web_context: WebContext
    ) -> tuple[str, int] | None:
        if not re.search(r"\b(kirkman|planning|planned|how many seasons)\b", prompt, re.IGNORECASE):
            return None
        for position, chunk in enumerate(web_context.evidence_chunks):
            text = f"{chunk.source_title}. {chunk.text}"
            if re.search(
                r"\b(7-9|7\s+to\s+9|seven,\s*eight,\s*or\s*nine|seven\s+or\s+eight\s+or\s+nine)\s+seasons\b",
                text,
                re.IGNORECASE,
            ):
                return "seven to nine", chunk.source_index or position + 1
            if re.search(r"\b(7-8|7\s+to\s+8|seven\s+to\s+eight)\s+seasons\b", text, re.IGNORECASE):
                return "seven to eight", chunk.source_index or position + 1
        return None

    def _target_region(self, prompt: str) -> str | None:
        if re.search(r"\b(india|indian|in india)\b", prompt, re.IGNORECASE):
            return "india"
        return None

    def _web_debug(
        self,
        web_context: WebContext | None,
        context: ContextPackage | None = None,
        web_context_in_prompt: bool = False,
        final_answer: str | None = None,
    ) -> dict[str, Any]:
        search = web_context.search if web_context is not None else None
        memory_context_loaded = False
        if context is not None:
            memory_context_loaded = bool(
                context.profile
                or context.preferences
                or context.goals
                or context.projects
                or context.relevant_memories
                or context.events
                or context.archive_results
            )
        return {
            "web_search_needed": bool(web_context and web_context.needed),
            "web_search_provider": search.provider
            if search is not None
            else self.web_search.provider.name,
            "web_provider_query": search.provider_query if search is not None else None,
            "web_search_called": search is not None,
            "web_decision_warning": web_context.warning if web_context is not None else None,
            "web_results_count": len(search.results) if search is not None else 0,
            "web_provider_attempts": search.provider_attempts if search is not None else [],
            "web_rejected_results": (
                [
                    {
                        "title": result.title,
                        "url": result.url,
                        "reason": "not_selected_for_verified_evidence",
                    }
                    for result in search.results
                    if web_context is not None
                    and result.url
                    not in {selected.url for selected in web_context.selected_results}
                ]
                if search is not None
                else []
            ),
            "web_results": (
                [
                    {
                        "rank": result.rank,
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "relevance_score": result.relevance_score,
                        "relevance_reasons": result.relevance_reasons,
                    }
                    for result in search.results[:10]
                ]
                if search is not None
                else []
            ),
            "web_selected_results": (
                [
                    {
                        "rank": result.rank,
                        "title": result.title,
                        "url": result.url,
                        "relevance_score": result.relevance_score,
                        "relevance_reasons": result.relevance_reasons,
                    }
                    for result in web_context.selected_results[:10]
                ]
                if web_context is not None
                else []
            ),
            "web_selected_results_count": len(web_context.selected_results)
            if web_context is not None
            else 0,
            "web_fetched_count": (
                sum(1 for page in web_context.pages if page.fetched)
                if web_context is not None
                else 0
            ),
            "web_fetched_pages": (
                [
                    {
                        "url": page.url,
                        "title": page.title,
                        "text_length": len(page.text),
                        "fetched": page.fetched,
                        "error": page.error,
                    }
                    for page in web_context.pages
                ]
                if web_context is not None
                else []
            ),
            "web_sources_count": len(web_context.citations) if web_context is not None else 0,
            "web_context_length": len(web_context.context_text) if web_context is not None else 0,
            "web_evidence_chunks_count": len(web_context.evidence_chunks)
            if web_context is not None
            else 0,
            "web_evidence": (
                [
                    {
                        "source_index": chunk.source_index,
                        "source_url": chunk.source_url,
                        "relevance_score": chunk.relevance_score,
                        "text": chunk.text[:500],
                    }
                    for chunk in web_context.evidence_chunks
                ]
                if web_context is not None
                else []
            ),
            "web_citation_decisions": (
                [
                    {
                        "index": citation.index,
                        "url": citation.url,
                        "fetched": citation.fetched,
                        "accepted": True,
                    }
                    for citation in web_context.citations
                ]
                if web_context is not None
                else []
            ),
            "web_freshness": {
                "required": bool(
                    web_context
                    and re.search(
                        r"\b(?:latest|current|today|recent|newest|right now)\b",
                        web_context.query,
                        re.IGNORECASE,
                    )
                ),
                "published_dates": (
                    [
                        result.published_date
                        for result in web_context.selected_results
                        if result.published_date
                    ]
                    if web_context is not None
                    else []
                ),
            },
            "web_answer_mode": web_context.answer_mode if web_context is not None else None,
            "memory_context_loaded": memory_context_loaded,
            "web_context_entered_final_prompt": web_context_in_prompt,
            "final_answer_included_sources": bool(final_answer and "Sources:" in final_answer),
        }

    #: Tokens an English word costs. Measured against local models: a 1500-word answer
    #: ran ~2400 tokens on qwen3-coder and overran 2550 on the wordier gemma4, so this
    #: carries deliberate headroom. Overshooting costs nothing -- generation stops at the
    #: end of the answer -- while undershooting truncates mid-sentence.
    _TOKENS_PER_WORD = 2.3
    #: Ceiling for an explicitly requested length, so one prompt cannot exhaust the context.
    _MAX_REQUESTED_PREDICT = 6144

    @staticmethod
    def _requested_output_tokens(prompt: str) -> int | None:
        """Tokens needed for an explicit length request such as "in about 1500 words".

        Without this every long-form request is capped at ``chat_num_predict`` and comes
        back truncated no matter how many continuations run.
        """
        match = re.search(r"\b(\d{2,5})\s*[- ]?\s*words?\b", prompt.lower())
        if not match:
            return None
        words = int(match.group(1))
        if words < 50:
            return None
        return int(
            min(words * NeoChatService._TOKENS_PER_WORD, NeoChatService._MAX_REQUESTED_PREDICT)
        )

    def _generate_complete(
        self, messages: list[LLMMessage], *, num_predict: int
    ) -> LLMChatResult:
        """Generate, continuing when the model stops because it hit the output limit.

        The streaming path already does this. Without it here a long answer comes back
        silently truncated mid-word, because the caller has no way to tell a finished
        answer from one that ran out of room.
        """
        result = self.ollama.chat_with_metadata(
            messages, temperature=0.2, num_predict=num_predict
        )
        content = result.content
        prompt_tokens = result.prompt_tokens or 0
        completion_tokens = result.completion_tokens or 0
        duration_ms = result.duration_ms or 0

        attempts = 0
        while result.finish_reason == "length" and attempts < 2:
            attempts += 1
            follow_up = [
                *messages,
                *([LLMMessage(role="assistant", content=content)] if content.strip() else []),
                LLMMessage(
                    role="user",
                    content=(
                        "Continue the same answer exactly where it stopped. "
                        "Do not repeat earlier text. Finish the requested answer."
                    ),
                ),
            ]
            result = self.ollama.chat_with_metadata(
                follow_up, temperature=0.2, num_predict=min(num_predict * (attempts + 1), 8192)
            )
            content = _append_without_overlap(content, result.content)
            prompt_tokens += result.prompt_tokens or 0
            completion_tokens += result.completion_tokens or 0
            duration_ms += result.duration_ms or 0

        return result.model_copy(
            update={
                "content": content,
                "prompt_tokens": prompt_tokens or None,
                "completion_tokens": completion_tokens or None,
                "total_tokens": (prompt_tokens + completion_tokens) or None,
                "duration_ms": duration_ms or None,
            }
        )

    def _num_predict(self, prompt: str, context: ContextPackage) -> int:
        requested = self._requested_output_tokens(prompt)
        if requested:
            return max(requested, self.settings.chat_num_predict)
        has_memory = bool(
            context.profile
            or context.preferences
            or context.goals
            or context.projects
            or context.relevant_memories
            or context.events
            or context.archive_results
        )
        if not has_memory and len(prompt) < 120:
            return self.settings.simple_chat_num_predict
        if re.search(
            r"\b(explain|detail|detailed|compare|summarize|roadmap|what should|"
            r"recommend|suggest|build next|documentation|failure cases?)\b",
            prompt.lower(),
        ):
            return self.settings.chat_num_predict
        return max(self.settings.simple_chat_num_predict, self.settings.chat_num_predict)


def _append_without_overlap(existing: str, continuation: str) -> str:
    """Join provider continuations without repeating an overlapping prefix."""

    if not continuation:
        return existing
    limit = min(len(existing), len(continuation), 1000)
    for overlap in range(limit, 0, -1):
        if existing[-overlap:] == continuation[:overlap]:
            return existing + continuation[overlap:]
    separator = (
        "" if existing.endswith((" ", "\n")) or continuation.startswith((" ", "\n")) else " "
    )
    return existing + separator + continuation


def _merge_generation_metadata(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    merged = {**first, **second}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "duration_ms"):
        values = [item.get(key) for item in (first, second)]
        numeric = [value for value in values if isinstance(value, int)]
        merged[key] = sum(numeric) if numeric else None
    return merged


ENTITY_CLUSTER_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "Xbox/Video Game",
        [
            re.compile(
                r"\b(xbox|playstation|ps5|ps4|nintendo|game(?:play)?|rpg|"
                r"lionhead|playground games|fable (?:game|reboot|remake|trilogy))\b",
                re.IGNORECASE,
            )
        ],
    ),
    (
        "AI/Technology",
        [
            re.compile(
                r"\b(ai|artificial intelligence|model|llm|openai|"
                r"gpt|machine learning|neural|fable\s+\d)\b",
                re.IGNORECASE,
            )
        ],
    ),
    (
        "TV Series",
        [
            re.compile(
                r"\b(tv|television|series|season|episode|streaming|netflix|"
                r"hulu|peacock|paramount|prime video|showrunner|renewed|"
                r"cancelled|canceled)\b",
                re.IGNORECASE,
            )
        ],
    ),
    (
        "Movie/Film",
        [
            re.compile(
                r"\b(movie|film|cinema|theatrical|box office|director|starring|trailer)\b",
                re.IGNORECASE,
            )
        ],
    ),
]


def _cluster_evidence_by_entity(query: str, chunks: list) -> dict[str, list]:
    """Detect if evidence chunks belong to clearly different entity categories."""
    if len(chunks) < 2:
        return {}

    chunk_labels: list[tuple[str, object]] = []
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text[:500]}".lower()
        best_label = "General"
        best_score = 0
        for label, patterns in ENTITY_CLUSTER_PATTERNS:
            score = sum(1 for p in patterns if p.search(text))
            if score > best_score:
                best_score = score
                best_label = label
        chunk_labels.append((best_label if best_score > 0 else "General", chunk))

    clusters: dict[str, list] = {}
    for label, chunk in chunk_labels:
        clusters.setdefault(label, []).append(chunk)

    non_general = {k: v for k, v in clusters.items() if k != "General"}
    if len(non_general) < 2:
        return {}

    if "General" in clusters:
        for chunk in clusters["General"]:
            largest = max(non_general, key=lambda k: len(non_general[k]))
            non_general[largest].append(chunk)

    return non_general


def _referenced_citation_indices(text: str) -> set[int]:
    """Citation numbers the text actually cites, including grouped [1, 2] markers."""
    indices: set[int] = set()
    for group in re.findall(r"\[((?:\d{1,2})(?:\s*,\s*\d{1,2})*)\]", text):
        indices.update(int(number) for number in re.findall(r"\d{1,2}", group))
    return indices


def _strip_llm_sources_block(reply: str) -> str:
    """Remove any Sources/References block the LLM generated — backend appends its own."""
    cleaned = re.split(
        r"(?:^|\s)(?:Sources|References|Citations)\s*:",
        reply,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*\n(?:\s*\[?\d{1,2}\]?\s*.*\n?)*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*$", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\s*[\[(]\s*$", "", cleaned)
    return cleaned.strip()


def _price_query_clarification(query: str) -> str | None:
    """Require product and market specificity before presenting a single tech price."""

    lowered = query.lower()
    if not re.search(r"\b(price|prices|cost|how much|pricing)\b", lowered):
        return None
    if not re.search(
        r"\b(iphone|ipad|macbook|pixel|galaxy|smartphone|phone|laptop)\b",
        lowered,
    ):
        return None
    has_model = bool(
        re.search(
            r"\b(?:iphone|ipad|pixel|galaxy|macbook)\s+"
            r"(?:\d{1,3}|m\d|air|pro|max|plus|mini|ultra|fold)\b",
            lowered,
        )
    )
    has_market = bool(
        re.search(
            r"\b(india|united states|usa|uk|canada|australia|"
            r"usd|inr|gbp|eur|dollars?|rupees?|pounds?|euros?)\b",
            lowered,
        )
    )
    if has_model and has_market:
        return None
    return (
        "Which exact model and country or currency should I price? "
        "Product families have multiple current models and region-specific prices."
    )


_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(\s*(https?://[^\s)]+)\s*\)")
_BARE_URL = re.compile(r"https?://\S+")


def _strip_fabricated_urls(reply: str, valid_urls: set[str]) -> str:
    """Remove inline URLs from an answer body that are not in the valid citation set.

    Only reached once a web search has actually run, so an uncited URL here is a
    source the model invented rather than ordinary content.

    A Markdown link collapses to its own label instead of losing only the target:
    deleting the URL alone left ``[label](`` behind, which reads as broken output and
    renders as literal text.
    """

    def _cited(url: str) -> bool:
        trimmed = url.rstrip(".,;:)>]")
        if trimmed in valid_urls:
            return True
        return any(trimmed.startswith(valid) or valid.startswith(trimmed) for valid in valid_urls)

    def _replace_link(match: re.Match) -> str:
        return match.group(0) if _cited(match.group(2)) else match.group(1)

    def _replace_url(match: re.Match) -> str:
        return match.group(0) if _cited(match.group(0)) else ""

    cleaned = _MARKDOWN_LINK.sub(_replace_link, reply)
    cleaned = _BARE_URL.sub(_replace_url, cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def _clean_snippet_text(text: str) -> str:
    """Strip raw search-result labels that should never appear in output."""
    cleaned = re.sub(r"^Search result title:\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.\s*Search result snippet:\s*", ". ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Search result snippet:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


FOLLOW_UP_SEARCH_COMMAND = re.compile(
    r"^(can you |could you |please )?(look|search|check|find)\s+(it|this|that)\s+up[.?!\s]*$",
    re.IGNORECASE,
)


_UNCERTAINTY_MARKERS = re.compile(
    r"(?:I don'?t know|I'?m not sure|I couldn'?t find|I can look it up|"
    r"I don'?t have (?:that|this) information|I'?m unable to|"
    r"I don'?t have enough|not in my memory|I couldn'?t locate)",
    re.IGNORECASE,
)

_FACTUAL_ENTITY_QUERY = re.compile(
    r"\b("
    r"how many (?:seasons?|episodes?|parts?|volumes?|runs?|goals?|points?)|"
    r"who (?:created|wrote|directed|produced|made|invented|designed|"
    r"founded|built|developed|started|launched)|"
    r"who (?:is|are|was|were) the (?:creator|writer|director|founder|"
    r"maker|developer|original creator)s? of|"
    r"who (?:is|are|was|were) the (?:original |founding )?(?:creator|"
    r"writer|director|founder|maker|developer|team)s? (?:of|behind)|"
    r"cast of|release date of|"
    r"when did .+ (?:release|end|start|premiere|air|come out)|"
    r"when was .+ (?:released|made|created|published)|"
    r"when did .+ (?:score|win|play|debut)|"
    r"(?:tv|television) series|"
    r"how many .+ (?:does|did|do|has|have)"
    r")\b",
    re.IGNORECASE,
)


def _reply_expresses_uncertainty(reply: str) -> bool:
    return bool(_UNCERTAINTY_MARKERS.search(reply[:400]))


def _is_factual_entity_query(prompt: str) -> bool:
    return bool(_FACTUAL_ENTITY_QUERY.search(prompt))


def _is_follow_up_search(prompt: str) -> bool:
    cleaned = prompt.strip()
    return bool(
        FOLLOW_UP_SEARCH_COMMAND.match(cleaned)
        or WebSearchDecisionService.BARE_COMMAND.match(cleaned)
    )


_CONTEXTUAL_WEB_FOLLOW_UP = re.compile(
    r"^(?:and\s+)?(?:"
    r"(?:when|where|how|who|what|which|is|are|was|were|does|do|did|"
    r"will|would|can|could|should)\b.{0,100}\b"
    r"(?:it|this|that|they|them|these|those|there)\b|"
    r"what\s+about\b|"
    r"(?:tell|show)\s+me\s+more\b|"
    r"(?:in|for)\s+[a-z][a-z .'-]{1,40}\??$"
    r")",
    re.IGNORECASE,
)


def _is_contextual_web_follow_up(prompt: str) -> bool:
    cleaned = " ".join(prompt.split())
    if re.search(r"\bit\s+(?:rain|snow|hail)\b", cleaned, re.IGNORECASE):
        return False
    return bool(_CONTEXTUAL_WEB_FOLLOW_UP.match(cleaned))


def resolve_web_search_query(prompt: str, history: list[ChatTurn]) -> str:
    cleaned = prompt.strip()
    bare_command = bool(
        WebSearchDecisionService.BARE_COMMAND.match(cleaned)
        or FOLLOW_UP_SEARCH_COMMAND.match(cleaned)
    )
    contextual_follow_up = _is_contextual_web_follow_up(cleaned)
    if not bare_command and not contextual_follow_up:
        return prompt
    decision_service = WebSearchDecisionService()
    for turn in reversed(history):
        if turn.role != "user":
            continue
        previous = turn.content.strip()
        if (
            previous
            and not WebSearchDecisionService.BARE_COMMAND.match(previous)
            and not FOLLOW_UP_SEARCH_COMMAND.match(previous)
            and not _is_contextual_web_follow_up(previous)
            and (
                bare_command
                or decision_service.decide(previous).needed
                or _is_factual_entity_query(previous)
            )
        ):
            if bare_command:
                return previous
            return f"{previous.rstrip(' .?!')} Follow-up: {cleaned}"
    return prompt
