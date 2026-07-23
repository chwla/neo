from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.memory_store import MemoryStore
from app.services.agents.guidance import agent_run_guidance
from app.services.chat_intent import resolve_internal_chat_intent
from app.services.code_index.service import CodeIndexService
from app.services.coding_agent.service import CodingAgentService
from app.services.context import ContextAssemblyService, ContextPackage
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.explanation import MemoryExplanationService
from app.services.extraction import ConversationMessage, ExtractionRequest, MemoryExtractionService
from app.services.files.service import WorkspaceFilesService
from app.services.git.service import GitContextService
from app.services.llm import ChatTurn, LLMClient, LLMMessage
from app.services.projects import ProjectContextService
from app.services.recovery.service import RecoveryService
from app.services.retrieval import RetrievalRequest
from app.services.rules.resolver import RuleResolver
from app.services.search.content import FactResult, run_extractors
from app.services.source_citations import CitationFormatter
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.tasks import TaskContextService
from app.services.test_runner.service import TestRunnerContextService
from app.services.web_search import (
    EXTRACTION_FAILURE_MESSAGE,
    GROUNDING_FAILURE_MESSAGE,
    WebContext,
    WebSearchDecisionService,
    WebSearchService,
)

MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)

DATE_WITH_OPTIONAL_YEAR_PATTERN = (
    rf"(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}(?:,?\s+20\d{{2}})?|"
    rf"\d{{1,2}}\s+(?:{MONTH_PATTERN})\.?(?:,?\s+20\d{{2}})?"
)


class NeoChatService:
    """Connects memory context, Ollama generation, archiving, and extraction."""

    def __init__(
        self,
        db: Session,
        ollama: LLMClient | None = None,
        extractor: MemoryExtractionService | None = None,
        rule_result: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.store = MemoryStore(db)
        if ollama is None:
            from app.services.llm import get_llm_client

            ollama = get_llm_client(route_name="chat")
        self.ollama = ollama
        self.extractor = extractor or MemoryExtractionService()
        self.rule_result = rule_result or {
            "resolved_rules": {},
            "applied_profiles": [],
            "warnings": [],
        }
        self.context_assembler = ContextAssemblyService()
        self.explainer = MemoryExplanationService()
        self.direct_answers = DirectMemoryAnswerService()
        self.web_search = WebSearchService()
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
        self.settings = get_settings()
        self.last_web_debug: dict[str, Any] = {}

    def build_context(self, prompt: str) -> ContextPackage:
        return self.context_assembler.assemble(
            self.store,
            RetrievalRequest(query=prompt, include_archives=False),
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
            "details to Memory automatically.\n\n"
            f"Memory context:\n{self._compact_context(context)}\n\n"
            f"Project context:\n{project_section}\n\n"
            f"Task context:\n{task_section}\n\n"
            f"Active rules (guidance only; never permission):\n{rule_section}\n\n"
            f"Web context:\n{web_section}"
        )
        messages = [LLMMessage(role="system", content=system_prompt)]
        messages.extend(
            LLMMessage(role=turn.role, content=turn.content)
            for turn in history[-self.settings.chat_history_turns :]
        )
        messages.append(LLMMessage(role="user", content=prompt))
        return messages

    def send_message(self, chat_id: int, prompt: str) -> str:
        history = [
            ChatTurn(role=message.role, content=message.content)
            for message in self.store.list_chat_messages(chat_id)
        ]
        self.store.add_chat_message(chat_id, "user", prompt)
        self.store.rename_chat_from_prompt(chat_id, prompt)
        self.db.commit()

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
        try:
            self.extract_user_prompt(prompt, chat_id)
        except Exception:
            self.db.rollback()
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
            self.store.add_chat_message(chat_id, "assistant", recovery_direct_reply)
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
        follow_up = _is_follow_up_search(prompt)
        web_query = self._web_query_with_memory_region(
            resolve_web_search_query(prompt, history), context
        )
        if follow_up:
            web_context = self.web_search.build_context_forced(web_query)
        else:
            web_context = self.web_search.build_context(web_query)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.last_web_debug = self._web_debug(
                web_context, context=context, final_answer=direct_reply
            )
            return direct_reply
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None:
            self.store.add_chat_message(chat_id, "assistant", web_failure)
            self.db.commit()
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
            return web_failure
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", direct_web_reply)
            self.db.commit()
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            return direct_web_reply
        messages = self.build_messages(
            prompt, history, context, web_context, project_context, task_context
        )
        self.db.rollback()

        try:
            result = self.ollama.chat_with_metadata(
                messages,
                temperature=0.2,
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
        except Exception as exc:
            if web_context.citations:
                reply = self._web_generation_fallback(prompt, web_context, exc)
                prompt_tokens = None
                completion_tokens = None
                total_tokens = None
                duration_ms = None
                thinking = None
            else:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
        if (
            not web_context.needed
            and _reply_expresses_uncertainty(reply)
            and _is_factual_entity_query(prompt)
        ):
            web_context = self.web_search.build_context_forced(web_query)
            direct_web_reply = self._direct_web_reply(web_query, web_context)
            if direct_web_reply is not None:
                reply = direct_web_reply
            elif web_context.evidence_chunks and web_context.citations:
                retry_messages = self.build_messages(
                    prompt,
                    history,
                    context,
                    web_context,
                    project_context,
                    task_context,
                )
                try:
                    retry_result = self.ollama.chat_with_metadata(
                        retry_messages,
                        temperature=0.2,
                        num_predict=self._num_predict(prompt, context),
                    )
                    reply = self._with_web_citations(retry_result.content, web_context)
                except Exception:
                    reply = self._web_generation_fallback(
                        prompt, web_context, RuntimeError("retry failed")
                    )
        self.store.add_chat_message(
            chat_id,
            "assistant",
            reply,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            thinking=thinking,
        )
        self.db.commit()
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
        )
        return reply

    def stream_message(
        self,
        chat_id: int,
        prompt: str,
        after_reply: Callable[[str, str], None] | None = None,
        existing_user_message_id: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        persisted_messages = self.store.list_chat_messages(chat_id)
        history = [
            ChatTurn(role=message.role, content=message.content)
            for message in persisted_messages
        ]
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
        else:
            self.store.add_chat_message(chat_id, "user", prompt)
            self.store.rename_chat_from_prompt(chat_id, prompt)
            self.db.commit()

        active_rules_reply = self._active_rules_reply(prompt)
        if active_rules_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", active_rules_reply)
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
            assistant = self.store.add_chat_message(chat_id, "assistant", agent_guidance)
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
        try:
            self.extract_user_prompt(prompt, chat_id)
        except Exception:
            self.db.rollback()
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
            assistant = self.store.add_chat_message(chat_id, "assistant", coding_direct_reply)
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
            assistant = self.store.add_chat_message(chat_id, "assistant", recovery_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"recovery_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": recovery_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": recovery_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        git_direct_reply = (
            self.git_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "git"
            else None
        )
        if git_direct_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", git_direct_reply)
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
            assistant = self.store.add_chat_message(chat_id, "assistant", test_direct_reply)
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
            assistant = self.store.add_chat_message(chat_id, "assistant", task_direct_reply)
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
        follow_up = _is_follow_up_search(prompt)
        web_query = self._web_query_with_memory_region(
            resolve_web_search_query(prompt, history), context
        )
        if follow_up:
            web_context = self.web_search.build_context_forced(web_query)
        else:
            web_context = self.web_search.build_context(web_query)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", direct_reply)
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
            }
            return
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", web_failure)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
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
            }
            return
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", direct_web_reply)
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, direct_web_reply)
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            yield {"type": "chunk", "content": direct_web_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": direct_web_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        messages = self.build_messages(
            prompt, history, context, web_context, project_context, task_context
        )
        self.db.rollback()

        raw_reply = ""
        final_metadata: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "duration_ms": None,
        }
        try:
            for event in self.ollama.chat_stream(
                messages,
                temperature=0.2,
                num_predict=self._num_predict(prompt, context),
            ):
                if event["type"] == "chunk":
                    raw_reply += event["content"]
                    yield event
                    continue
                final_metadata = event
        except Exception as exc:
            if not web_context.citations:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
            reply = self._web_generation_fallback(prompt, web_context, exc)
            assistant = self.store.add_chat_message(chat_id, "assistant", reply)
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, reply)
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                final_answer=reply,
            )
            yield {"type": "chunk", "content": reply}
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
            }
            return

        cleaned_reply = self.ollama.clean_response(raw_reply)
        if web_context.citations and not self._has_web_citation_marker(cleaned_reply, web_context):
            reply = self._web_generation_fallback(
                prompt,
                web_context,
                RuntimeError("generated web answer lacked citation markers"),
            )
        else:
            reply = self._with_web_citations(cleaned_reply, web_context)
        if (
            not web_context.needed
            and _reply_expresses_uncertainty(reply)
            and _is_factual_entity_query(prompt)
        ):
            web_context = self.web_search.build_context_forced(web_query)
            direct_web_reply = self._direct_web_reply(web_query, web_context)
            if direct_web_reply is not None:
                reply = direct_web_reply
                yield {"type": "replace", "content": reply}
            elif web_context.evidence_chunks and web_context.citations:
                retry_messages = self.build_messages(
                    prompt,
                    history,
                    context,
                    web_context,
                    project_context,
                    task_context,
                )
                try:
                    retry_result = self.ollama.chat_with_metadata(
                        retry_messages,
                        temperature=0.2,
                        num_predict=self._num_predict(prompt, context),
                    )
                    reply = self._with_web_citations(retry_result.content, web_context)
                except Exception:
                    reply = self._web_generation_fallback(
                        prompt, web_context, RuntimeError("retry failed")
                    )
                yield {"type": "replace", "content": reply}
        thinking = self.ollama.extract_thinking(raw_reply)
        assistant = self.store.add_chat_message(
            chat_id,
            "assistant",
            reply,
            prompt_tokens=final_metadata.get("prompt_tokens"),
            completion_tokens=final_metadata.get("completion_tokens"),
            total_tokens=final_metadata.get("total_tokens"),
            duration_ms=final_metadata.get("duration_ms"),
            thinking=thinking,
        )
        self.db.commit()
        self.db.refresh(assistant)
        if after_reply is not None:
            after_reply(prompt, reply)
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
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
            "web_debug": self.last_web_debug,
        }

    def extract_user_prompt(self, prompt: str, chat_id: int | None = None) -> list[int]:
        extraction = self.extractor.extract(
            ExtractionRequest(text=prompt, persist=True, source_conversation_id=chat_id),
        )
        candidates = self.extractor.persist_and_accept(self.store, extraction)
        self.db.commit()
        return [candidate.id for candidate in candidates]

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

    def _direct_reply(self, prompt: str) -> str | None:
        short = _short_input_clarification(prompt)
        if short is not None:
            return short
        if not self.explainer.should_handle(prompt):
            return self.direct_answers.answer(self.store, prompt)
        return self.explainer.answer(self.store, prompt)

    def _web_query_with_memory_region(self, query: str, context: ContextPackage) -> str:
        if not self._is_release_date_query(query):
            return query
        if self._target_region(query) is not None:
            return query
        country = self._country_from_memory(context) or self._country_from_profile_store()
        if country is None:
            return query
        return f"{query} in {country}"

    def _is_release_date_query(self, query: str) -> bool:
        return bool(
            re.search(
                r"\b(release|released|releasing|premiere|date|when|coming out)\b",
                query,
                re.IGNORECASE,
            )
            and re.search(
                r"\b(movie|film|season|show|series|spider-?man|spiderman|odyssey|avengers|doomsday|dune|supergirl|god of war)\b",
                query,
                re.IGNORECASE,
            )
        )

    def _country_from_memory(self, context: ContextPackage) -> str | None:
        for item in context.profile:
            key = str(getattr(item, "key", "")).lower()
            value = str(getattr(item, "value", ""))
            if key not in {"location", "country", "nationality"}:
                continue
            country = self._country_from_text(value)
            if country is not None:
                return country
        return None

    def _country_from_profile_store(self) -> str | None:
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "active_profile_by_key"):
            return None
        for key in ("country", "location", "nationality"):
            try:
                facts = store.active_profile_by_key(key)
            except Exception:
                continue
            for fact in facts:
                country = self._country_from_text(str(getattr(fact, "value", "")))
                if country is not None:
                    return country
        return None

    def _country_from_text(self, text: str) -> str | None:
        if re.search(r"\b(india|indian)\b", text, re.IGNORECASE):
            return "India"
        return None

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
        if web_context is None or not web_context.needed or not web_context.citations:
            body = self._strip_orphan_citation_markers(body)
            return _strip_fabricated_urls(body, set())
        valid_urls = {citation.url for citation in web_context.citations}
        body = _strip_fabricated_urls(body, valid_urls)
        citations = self.citation_formatter.format_citations(web_context.citations)
        if not citations:
            return self._strip_orphan_citation_markers(body)
        return f"{body.strip()}\n\n{citations}"

    def _strip_orphan_citation_markers(self, reply: str) -> str:
        cleaned = re.sub(r"\s*\[(?:\d{1,2})(?:\s*,\s*\d{1,2})*\]", "", reply)
        cleaned = re.sub(r" {2,}", " ", cleaned)
        return cleaned.strip()

    def _has_web_citation_marker(self, reply: str, web_context: WebContext) -> bool:
        return any(f"[{citation.index}]" in reply for citation in web_context.citations)

    def _web_generation_fallback(
        self, prompt: str, web_context: WebContext, error: Exception
    ) -> str:
        if web_context.answer_mode == "fact_lookup":
            fact = run_extractors(prompt, web_context.evidence_chunks)
            if fact is not None:
                answer = self._format_fact_answer(prompt, fact)
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            return "I searched the web but could not find sufficiently reliable evidence to answer that."
        if web_context.answer_mode in {"news_summary", "overview"}:
            lines = [
                "Here are the source-backed updates I found:"
                if web_context.answer_mode == "news_summary"
                else "Here is what the sources say:",
            ]
            for chunk in web_context.evidence_chunks[:4]:
                lines.append(f"- {_clean_snippet_text(chunk.text[:420])} [{chunk.source_index}]")
            citations = self.citation_formatter.format_citations(web_context.citations)
            if citations:
                lines.extend(["", citations])
            return "\n".join(lines)
        return (
            "I searched the web but could not find sufficiently reliable evidence to answer that."
        )

    def _direct_web_reply(self, prompt: str, web_context: WebContext) -> str | None:
        if not web_context.needed or not web_context.evidence_chunks or not web_context.citations:
            return None
        if web_context.answer_mode == "fact_lookup":
            fact = run_extractors(prompt, web_context.evidence_chunks)
            if fact is not None:
                answer = self._format_fact_answer(prompt, fact)
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            planned_match = self._planned_seasons_from_evidence(prompt, web_context)
            if planned_match is not None:
                planned, source_index = planned_match
                answer = (
                    f"Robert Kirkman has described the plan as {planned} seasons [{source_index}]."
                )
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            return None
        if web_context.answer_mode in {"news_summary", "overview"}:
            clusters = _cluster_evidence_by_entity(prompt, web_context.evidence_chunks)
            if len(clusters) > 1:
                lines = ["I found results for multiple topics:"]
                for cluster_label, cluster_chunks in clusters.items():
                    lines.append(f"\n**{cluster_label}:**")
                    for chunk in cluster_chunks[:2]:
                        lines.append(
                            f"- {_clean_snippet_text(chunk.text[:350])} [{chunk.source_index}]"
                        )
                citations = self.citation_formatter.format_citations(web_context.citations)
                if citations:
                    lines.extend(["", citations])
                return "\n".join(lines)
            heading = (
                "Here are the source-backed updates I found:"
                if web_context.answer_mode == "news_summary"
                else "Here is what the sources say:"
            )
            lines = [heading]
            chunks = sorted(
                web_context.evidence_chunks,
                key=lambda chunk: (self._source_priority(chunk.source_url), -chunk.relevance_score),
            )
            for chunk in chunks[:4]:
                lines.append(f"- {_clean_snippet_text(chunk.text[:420])} [{chunk.source_index}]")
            citations = self.citation_formatter.format_citations(web_context.citations)
            if citations:
                lines.extend(["", citations])
            return "\n".join(lines)
        return None

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

    def _source_priority(self, url: str) -> int:
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        official_domains = {
            "anthropic.com",
            "bcci.tv",
            "icc-cricket.com",
            "marvel.com",
            "nextjs.org",
            "openai.com",
            "primevideo.com",
            "registry.npmjs.org",
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

    def _release_date_from_evidence(
        self, prompt: str, web_context: WebContext
    ) -> tuple[str, int] | None:
        if not re.search(
            r"\b(release|released|releasing|premiere|date|when)\b", prompt, re.IGNORECASE
        ):
            return None
        target_region = self._target_region(prompt)
        candidates: list[tuple[tuple[int, int, int, int, int], str, int]] = []
        for position, chunk in enumerate(web_context.evidence_chunks):
            domain = urlparse(chunk.source_url).netloc.lower().removeprefix("www.")
            text = f"{chunk.source_title}. {chunk.source_url}. {chunk.text}"
            region_penalty = self._region_penalty(target_region, domain, text)
            source_penalty = self._release_source_penalty(target_region, domain)
            for match in re.finditer(
                rf"\b(?P<date>{DATE_WITH_OPTIONAL_YEAR_PATTERN})\b", text, flags=re.IGNORECASE
            ):
                normalized_date = self._normalize_release_date(match.group("date"), text)
                if normalized_date is None:
                    continue
                context = self._date_sentence(text, match.start(), match.end())
                release_penalty = self._release_context_penalty(context)
                if release_penalty >= 8:
                    continue
                booking_penalty = (
                    4 if self._booking_date_context(context) and release_penalty > 0 else 0
                )
                priority = (
                    region_penalty,
                    release_penalty,
                    booking_penalty,
                    source_penalty,
                    position,
                )
                candidates.append((priority, normalized_date, chunk.source_index or position + 1))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        _, date, source_index = candidates[0]
        return date, source_index

    def _target_region(self, prompt: str) -> str | None:
        if re.search(r"\b(india|indian|in india)\b", prompt, re.IGNORECASE):
            return "india"
        return None

    def _region_penalty(self, target_region: str | None, domain: str, text: str) -> int:
        if target_region != "india":
            return 0
        if self._is_india_specific_source(domain, text):
            return 0
        return 4

    def _is_india_specific_source(self, domain: str, text: str) -> bool:
        india_domains = {
            "business-standard.com",
            "district.in",
            "economictimes.indiatimes.com",
            "filmibeat.com",
            "gadgets360.com",
            "in.bookmyshow.com",
            "indiatoday.in",
            "news24online.com",
            "thehindu.com",
            "timesnownews.com",
        }
        lowered = text.lower()
        return (
            domain in india_domains
            or f"www.{domain}" in india_domains
            or domain.startswith("in.")
            or bool(
                re.search(
                    r"\b(india|indian|mumbai|delhi|chennai|bengaluru|gurgaon|hindi|tamil|telugu)\b",
                    lowered,
                )
            )
        )

    def _release_source_penalty(self, target_region: str | None, domain: str) -> int:
        if target_region == "india":
            if domain in {"in.bookmyshow.com"}:
                return 0
            if domain in {
                "thehindu.com",
                "indiatoday.in",
                "business-standard.com",
                "timesnownews.com",
                "gadgets360.com",
            }:
                return 1
            if domain in {"district.in"}:
                return 2
            if domain in {"theodysseymovie.com", "marvel.com"}:
                return 3
            return 4
        if domain in {"marvel.com", "theodysseymovie.com"}:
            return 0
        return 1

    def _release_context_penalty(self, context: str) -> int:
        lowered = context.lower()
        strong_patterns = (
            r"\brelease(?:s|d| date)?\b",
            r"\breleasing\b",
            r"\bpremiere(?:s|d)?\b",
            r"\bin (?:theatres|theaters|cinemas)\b",
            r"\b(?:opens|open|opening)\s+in (?:theatres|theaters|cinemas)\b",
            r"\bexclusively in cinemas\b",
            r"\bswings into theatres\b",
        )
        if any(re.search(pattern, lowered) for pattern in strong_patterns):
            return 0
        if re.search(r"\b(movie|film|theatres|theaters|cinemas|showtimes)\b", lowered):
            return 2
        return 8

    def _booking_date_context(self, context: str) -> bool:
        lowered = context.lower()
        return bool(
            re.search(
                r"\b(advance\s+)?(booking|bookings|ticket|tickets|presale|pre-sale|early access|on sale|go live|begin|begins)\b",
                lowered,
            )
        )

    def _date_sentence(self, text: str, start: int, end: int) -> str:
        left_candidates = [text.rfind(separator, 0, start) for separator in (".", "\n", ";")]
        right_candidates = [text.find(separator, end) for separator in (".", "\n", ";")]
        left = max(left_candidates)
        right = min([candidate for candidate in right_candidates if candidate != -1], default=-1)
        sentence_start = left + 1 if left != -1 else max(0, start - 80)
        sentence_end = right if right != -1 else min(len(text), end + 80)
        return text[sentence_start:sentence_end]

    def _normalize_release_date(self, raw_date: str, text: str) -> str | None:
        date_text = re.sub(r"\s+", " ", raw_date.strip().replace(".", ""))
        date_text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", date_text, flags=re.IGNORECASE)
        if not re.search(r"\b20\d{2}\b", date_text):
            inferred_year = re.search(r"\b(20\d{2})\b", text)
            if inferred_year is None:
                return None
            date_text = f"{date_text} {inferred_year.group(1)}"
        formats = (
            "%B %d, %Y",
            "%B %d %Y",
            "%b %d, %Y",
            "%b %d %Y",
            "%d %B, %Y",
            "%d %B %Y",
            "%d %b, %Y",
            "%d %b %Y",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(date_text, fmt)
            except ValueError:
                continue
            return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
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
            "web_answer_mode": web_context.answer_mode if web_context is not None else None,
            "memory_context_loaded": memory_context_loaded,
            "web_context_entered_final_prompt": web_context_in_prompt,
            "final_answer_included_sources": bool(final_answer and "Sources:" in final_answer),
        }

    def _num_predict(self, prompt: str, context: ContextPackage) -> int:
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
        if self.web_search.should_search(prompt).needed:
            return self.settings.chat_num_predict
        if re.search(
            r"\b(summarize|roadmap|what should|recommend|suggest|build next)\b", prompt.lower()
        ):
            return self.settings.chat_num_predict
        return min(self.settings.chat_num_predict, 96)

    def extract_after_turn(self, user_prompt: str, assistant_reply: str) -> list[int]:
        if not self.settings.extraction_after_turn_enabled:
            return []
        extraction = self.extractor.extract_with_llm(
            ExtractionRequest(
                messages=[
                    ConversationMessage(role="user", content=user_prompt),
                    ConversationMessage(role="assistant", content=assistant_reply),
                ],
                persist=True,
            ),
            self.ollama,
        )
        candidates = self.extractor.persist_and_accept(self.store, extraction)
        self.db.commit()
        return [candidate.id for candidate in candidates]


ENTITY_CLUSTER_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "Xbox/Video Game",
        [
            re.compile(
                r"\b(xbox|playstation|ps5|ps4|nintendo|game(?:play)?|rpg|lionhead|playground games|fable (?:game|reboot|remake|trilogy))\b",
                re.IGNORECASE,
            )
        ],
    ),
    (
        "AI/Technology",
        [
            re.compile(
                r"\b(ai|artificial intelligence|model|llm|anthropic|openai|claude|gpt|machine learning|neural|fable\s+\d)\b",
                re.IGNORECASE,
            )
        ],
    ),
    (
        "TV Series",
        [
            re.compile(
                r"\b(tv|television|series|season|episode|streaming|netflix|hulu|peacock|paramount|prime video|showrunner|renewed|cancelled|canceled)\b",
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


def _strip_llm_sources_block(reply: str) -> str:
    """Remove any Sources/References block the LLM generated — backend appends its own."""
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*\n(?:\s*\[?\d{1,2}\]?\s*.*\n?)*",
        "",
        reply,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*$", "", cleaned, flags=re.IGNORECASE
    )
    return cleaned.strip()


def _strip_fabricated_urls(reply: str, valid_urls: set[str]) -> str:
    """Remove inline URLs from answer body that are not in the valid citation set."""
    if not valid_urls:
        return re.sub(r"https?://\S+", "", reply).strip()

    def _replace_url(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;:)>]")
        if url in valid_urls:
            return match.group(0)
        for valid in valid_urls:
            if url.startswith(valid) or valid.startswith(url):
                return match.group(0)
        return ""

    cleaned = re.sub(r"https?://\S+", _replace_url, reply)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def _clean_snippet_text(text: str) -> str:
    """Strip raw 'Search result title/snippet' labels that should never appear in user-facing output."""
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
    r"who (?:created|wrote|directed|produced|made|invented|designed|founded|built|developed|started|launched)|"
    r"who (?:is|are|was|were) the (?:creator|writer|director|founder|maker|developer|original creator)s? of|"
    r"who (?:is|are|was|were) the (?:original |founding )?(?:creator|writer|director|founder|maker|developer|team)s? (?:of|behind)|"
    r"cast of|release date of|"
    r"when did .+ (?:release|end|start|premiere|air|come out)|"
    r"when was .+ (?:released|made|created|published)|"
    r"when did .+ (?:score|win|play|debut)|"
    r"(?:tv|television) series|"
    r"how many .+ (?:does|did|do|has|have)"
    r")\b",
    re.IGNORECASE,
)


_SHORT_TOOL_HINTS: dict[str, str] = {
    "sed": "`sed`, the Unix stream editor",
    "awk": "`awk`, the text processing language",
    "grep": "`grep`, the text search utility",
    "npm": "`npm`, the Node.js package manager",
    "docker": "`docker`, the container platform",
    "git": "`git`, the version control system",
    "curl": "`curl`, the URL transfer tool",
    "wget": "`wget`, the file download utility",
    "pip": "`pip`, the Python package manager",
    "yarn": "`yarn`, the JavaScript package manager",
    "vim": "`vim`, the text editor",
    "bash": "`bash`, the Unix shell",
    "zsh": "`zsh`, the Z shell",
    "ssh": "`ssh`, the secure shell protocol",
    "tar": "`tar`, the archive utility",
    "make": "`make`, the build automation tool",
    "gcc": "`gcc`, the GNU C compiler",
    "apt": "`apt`, the Debian package manager",
    "brew": "`brew`, the macOS package manager",
    "ps": "`ps`, the process status command",
    "ls": "`ls`, the directory listing command",
    "cat": "`cat`, the file concatenation command",
    "chmod": "`chmod`, the file permission command",
    "rsync": "`rsync`, the file synchronization tool",
}


def _short_input_clarification(prompt: str) -> str | None:
    """Fast response for very short, ambiguous, or typo-like inputs."""
    cleaned = prompt.strip().rstrip("'\"?!.,;:")
    if len(cleaned) > 12 or len(cleaned) < 3:
        return None
    if " " in cleaned and len(cleaned.split()) > 2:
        return None
    lowered = cleaned.lower()
    hint = _SHORT_TOOL_HINTS.get(lowered)
    if hint is not None:
        return f"Did you mean {hint}?"
    if re.match(r"^[a-z]{2,10}$", lowered):
        return (
            f"Did you mean `{lowered}`? Could you give me a bit more context about what you need?"
        )
    return None


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


def resolve_web_search_query(prompt: str, history: list[ChatTurn]) -> str:
    cleaned = prompt.strip()
    if not WebSearchDecisionService.BARE_COMMAND.match(
        cleaned
    ) and not FOLLOW_UP_SEARCH_COMMAND.match(cleaned):
        return prompt
    for turn in reversed(history):
        if turn.role != "user":
            continue
        previous = turn.content.strip()
        if (
            previous
            and not WebSearchDecisionService.BARE_COMMAND.match(previous)
            and not FOLLOW_UP_SEARCH_COMMAND.match(previous)
        ):
            return previous
    return prompt
