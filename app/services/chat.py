from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.memory_store import MemoryStore
from app.services.context import ContextAssemblyService, ContextPackage
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import ConversationMessage, ExtractionRequest, MemoryExtractionService
from app.services.explanation import MemoryExplanationService
from app.services.ollama_client import ChatTurn, OllamaClient, OllamaMessage
from app.services.retrieval import RetrievalRequest
from app.services.source_citations import CitationFormatter
from app.services.web_search import EXTRACTION_FAILURE_MESSAGE, GROUNDING_FAILURE_MESSAGE, WebContext, WebSearchService


class NeoChatService:
    """Connects memory context, Ollama generation, archiving, and extraction."""

    def __init__(
        self,
        db: Session,
        ollama: OllamaClient | None = None,
        extractor: MemoryExtractionService | None = None,
    ) -> None:
        self.db = db
        self.store = MemoryStore(db)
        self.ollama = ollama or OllamaClient()
        self.extractor = extractor or MemoryExtractionService()
        self.context_assembler = ContextAssemblyService()
        self.explainer = MemoryExplanationService()
        self.direct_answers = DirectMemoryAnswerService()
        self.web_search = WebSearchService()
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
    ) -> list[OllamaMessage]:
        web_section = self._compact_web_context(web_context)
        system_prompt = (
            "You are Neo, a local personal AI assistant. Use the provided memory context "
            "when it is relevant. Do not claim memories that are not present. If memory "
            "context conflicts, prefer active goals, active projects, current profile facts, "
            "and current preferences. For personal questions about the user's name, age, "
            "location, preferences, goals, or projects, answer only from memory context or "
            "conversation history. If the fact is not present, say you do not know yet. "
            "Memory context and web context are separate. Use web context only for current, "
            "recent, or explicitly searched information. When web context is provided, cite "
            "web-grounded claims using bracket markers like [1]. For web-grounded prompts, "
            "do not use memory, conversation history, or general knowledge to fill gaps in "
            "the retrieved web evidence. The web context contains extracted evidence only; "
            "do not infer beyond it. "
            "Answer directly and concisely.\n\n"
            f"Memory context:\n{self._compact_context(context)}\n\n"
            f"Web context:\n{web_section}"
        )
        messages = [OllamaMessage(role="system", content=system_prompt)]
        messages.extend(
            OllamaMessage(role=turn.role, content=turn.content)
            for turn in history[-12:]
        )
        messages.append(OllamaMessage(role="user", content=prompt))
        return messages

    def send_message(self, chat_id: int, prompt: str) -> str:
        history = [
            ChatTurn(role=message.role, content=message.content)
            for message in self.store.list_chat_messages(chat_id)
        ]
        self.store.add_chat_message(chat_id, "user", prompt)
        self.store.rename_chat_from_prompt(chat_id, prompt)
        self.db.commit()

        try:
            self.extract_user_prompt(prompt, chat_id)
        except Exception:
            self.db.rollback()
        web_context = self.web_search.build_context(prompt)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.last_web_debug = self._web_debug(web_context, final_answer=direct_reply)
            return direct_reply
        context = self.build_context(prompt)
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
        messages = self.build_messages(prompt, history, context, web_context)
        self.db.rollback()

        try:
            result = self.ollama.chat_with_metadata(
                messages,
                temperature=0.2,
                num_predict=self._num_predict(prompt, context),
            )
            if web_context.citations and not self._has_web_citation_marker(result.content, web_context):
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
    ) -> Iterator[dict[str, Any]]:
        history = [
            ChatTurn(role=message.role, content=message.content)
            for message in self.store.list_chat_messages(chat_id)
        ]
        self.store.add_chat_message(chat_id, "user", prompt)
        self.store.rename_chat_from_prompt(chat_id, prompt)
        self.db.commit()

        try:
            self.extract_user_prompt(prompt, chat_id)
        except Exception:
            self.db.rollback()
        web_context = self.web_search.build_context(prompt)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = self._web_debug(web_context, final_answer=direct_reply)
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
        context = self.build_context(prompt)
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
        messages = self.build_messages(prompt, history, context, web_context)
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

    def _direct_reply(self, prompt: str) -> str | None:
        if not self.explainer.should_handle(prompt):
            return self.direct_answers.answer(self.store, prompt)
        return self.explainer.answer(self.store, prompt)

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
        lines.extend(
            f"memory #{item.id}: {item.memory_text}"
            for item in context.relevant_memories
        )
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
        if web_context is None or not web_context.needed or not web_context.citations:
            return reply
        citations = self.citation_formatter.format_citations(web_context.citations)
        if not citations:
            return reply
        return f"{reply.strip()}\n\n{citations}"

    def _has_web_citation_marker(self, reply: str, web_context: WebContext) -> bool:
        return any(f"[{citation.index}]" in reply for citation in web_context.citations)

    def _web_generation_fallback(self, prompt: str, web_context: WebContext, error: Exception) -> str:
        lines = [
            "I found these source-backed passages:",
        ]
        for chunk in web_context.evidence_chunks[:4]:
            lines.append(f"- {chunk.text[:420]} [{chunk.source_index}]")
        lines.append(f"Grounding fallback reason: {error}")
        citations = self.citation_formatter.format_citations(web_context.citations)
        if citations:
            lines.extend(["", citations])
        return "\n".join(lines)

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
            "web_search_provider": search.provider if search is not None else self.web_search.provider.name,
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
            "web_evidence_chunks_count": len(web_context.evidence_chunks) if web_context is not None else 0,
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
        if re.search(r"\b(summarize|roadmap|what should|recommend|suggest|build next)\b", prompt.lower()):
            return self.settings.chat_num_predict
        return min(self.settings.chat_num_predict, 128)

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
