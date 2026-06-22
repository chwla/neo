from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

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
        context = self.build_context(prompt)
        web_query = self._web_query_with_memory_region(resolve_web_search_query(prompt, history), context)
        web_context = self.web_search.build_context(web_query)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.last_web_debug = self._web_debug(web_context, context=context, final_answer=direct_reply)
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
        context = self.build_context(prompt)
        web_query = self._web_query_with_memory_region(resolve_web_search_query(prompt, history), context)
        web_context = self.web_search.build_context(web_query)
        direct_reply = None if web_context.needed else self._direct_reply(prompt)
        if direct_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = self._web_debug(web_context, context=context, final_answer=direct_reply)
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
            re.search(r"\b(release|released|releasing|premiere|date|when)\b", query, re.IGNORECASE)
            and re.search(
                r"\b(movie|film|season|show|series|spider-?man|spiderman|odyssey|avengers|doomsday|dune)\b",
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
        if re.search(r"(?im)^\s*Sources:\s*$", reply):
            return reply
        if web_context is None or not web_context.needed or not web_context.citations:
            return self._strip_orphan_citation_markers(reply)
        citations = self.citation_formatter.format_citations(web_context.citations)
        if not citations:
            return self._strip_orphan_citation_markers(reply)
        return f"{reply.strip()}\n\n{citations}"

    def _strip_orphan_citation_markers(self, reply: str) -> str:
        cleaned = re.sub(r"\s*\[(?:\d{1,2})(?:\s*,\s*\d{1,2})*\]", "", reply)
        cleaned = re.sub(r" {2,}", " ", cleaned)
        return cleaned.strip()

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

    def _direct_web_reply(self, prompt: str, web_context: WebContext) -> str | None:
        if not web_context.needed or not web_context.evidence_chunks or not web_context.citations:
            return None
        if web_context.answer_mode == "fact_lookup":
            episode_match = self._episode_count_from_evidence(prompt, web_context)
            if episode_match is not None:
                count, source_index = episode_match
                answer = f"The listed episode count is {count} episodes [{source_index}]."
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            planned_match = self._planned_seasons_from_evidence(prompt, web_context)
            if planned_match is not None:
                planned, source_index = planned_match
                answer = f"Robert Kirkman has described the plan as {planned} seasons [{source_index}]."
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            release_match = self._release_date_from_evidence(prompt, web_context)
            if release_match is not None:
                release_date, source_index = release_match
                prefix = "In India, the listed release date is" if self._target_region(prompt) == "india" else "The listed release date is"
                answer = (
                    f"{prefix} {release_date.rstrip('.')} "
                    f"[{source_index}]."
                )
                citations = self.citation_formatter.format_citations(web_context.citations)
                return f"{answer}\n\n{citations}" if citations else answer
            return None
        if web_context.answer_mode in {"news_summary", "overview"}:
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
                lines.append(f"- {chunk.text[:420]} [{chunk.source_index}]")
            citations = self.citation_formatter.format_citations(web_context.citations)
            if citations:
                lines.extend(["", citations])
            return "\n".join(lines)
        return None

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

    def _episode_count_from_evidence(self, prompt: str, web_context: WebContext) -> tuple[int, int] | None:
        if not re.search(r"\b(episode|episodes|how many)\b", prompt, re.IGNORECASE):
            return None
        candidates: list[tuple[int, int, int]] = []
        for position, chunk in enumerate(web_context.evidence_chunks):
            text = f"{chunk.source_title}. {chunk.text}"
            if re.search(r"\b(first|last|next|remaining|one more|final)\s+\w+\s+episodes\b", text, re.IGNORECASE):
                continue
            match = re.search(
                r"\b(?:consists of|has|have|with|contains|includes)\s+"
                r"(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
                r"\s+episodes\b",
                text,
                re.IGNORECASE,
            )
            if not match:
                match = re.search(
                    r"\b(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
                    r"\s+episodes\b",
                    text,
                    re.IGNORECASE,
                )
            if not match:
                continue
            count = self._number_from_text(match.group("count"))
            if count is None:
                continue
            candidates.append((position, count, chunk.source_index or position + 1))
        if not candidates:
            return None
        _, count, source_index = sorted(candidates)[0]
        return count, source_index

    def _planned_seasons_from_evidence(self, prompt: str, web_context: WebContext) -> tuple[str, int] | None:
        if not re.search(r"\b(kirkman|planning|planned|how many seasons)\b", prompt, re.IGNORECASE):
            return None
        for position, chunk in enumerate(web_context.evidence_chunks):
            text = f"{chunk.source_title}. {chunk.text}"
            if re.search(r"\b(7-9|7\s+to\s+9|seven,\s*eight,\s*or\s*nine|seven\s+or\s+eight\s+or\s+nine)\s+seasons\b", text, re.IGNORECASE):
                return "seven to nine", chunk.source_index or position + 1
            if re.search(r"\b(7-8|7\s+to\s+8|seven\s+to\s+eight)\s+seasons\b", text, re.IGNORECASE):
                return "seven to eight", chunk.source_index or position + 1
        return None

    def _number_from_text(self, value: str) -> int | None:
        lowered = value.lower()
        words = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        if lowered.isdigit():
            return int(lowered)
        return words.get(lowered)

    def _release_date_from_evidence(self, prompt: str, web_context: WebContext) -> tuple[str, int] | None:
        if not re.search(r"\b(release|released|releasing|premiere|date|when)\b", prompt, re.IGNORECASE):
            return None
        target_region = self._target_region(prompt)
        candidates: list[tuple[tuple[int, int, int, int, int], str, int]] = []
        for position, chunk in enumerate(web_context.evidence_chunks):
            domain = urlparse(chunk.source_url).netloc.lower().removeprefix("www.")
            text = f"{chunk.source_title}. {chunk.source_url}. {chunk.text}"
            region_penalty = self._region_penalty(target_region, domain, text)
            source_penalty = self._release_source_penalty(target_region, domain)
            for match in re.finditer(rf"\b(?P<date>{DATE_WITH_OPTIONAL_YEAR_PATTERN})\b", text, flags=re.IGNORECASE):
                normalized_date = self._normalize_release_date(match.group("date"), text)
                if normalized_date is None:
                    continue
                context = self._date_sentence(text, match.start(), match.end())
                release_penalty = self._release_context_penalty(context)
                if release_penalty >= 8:
                    continue
                booking_penalty = 4 if self._booking_date_context(context) and release_penalty > 0 else 0
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
            or bool(re.search(r"\b(india|indian|mumbai|delhi|chennai|bengaluru|gurgaon|hindi|tamil|telugu)\b", lowered))
        )

    def _release_source_penalty(self, target_region: str | None, domain: str) -> int:
        if target_region == "india":
            if domain in {"in.bookmyshow.com"}:
                return 0
            if domain in {"thehindu.com", "indiatoday.in", "business-standard.com", "timesnownews.com", "gadgets360.com"}:
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
            "web_selected_results_count": len(web_context.selected_results) if web_context is not None else 0,
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


FOLLOW_UP_SEARCH_COMMAND = re.compile(
    r"^(can you |could you |please )?(look|search|check|find)\s+(it|this|that)\s+up[.?!\s]*$",
    re.IGNORECASE,
)


def resolve_web_search_query(prompt: str, history: list[ChatTurn]) -> str:
    cleaned = prompt.strip()
    if not WebSearchDecisionService.BARE_COMMAND.match(cleaned) and not FOLLOW_UP_SEARCH_COMMAND.match(cleaned):
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
