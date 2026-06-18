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
        self.settings = get_settings()

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
    ) -> list[OllamaMessage]:
        system_prompt = (
            "You are Neo, a local personal AI assistant. Use the provided memory context "
            "when it is relevant. Do not claim memories that are not present. If memory "
            "context conflicts, prefer active goals, active projects, current profile facts, "
            "and current preferences. For personal questions about the user's name, age, "
            "location, preferences, goals, or projects, answer only from memory context or "
            "conversation history. If the fact is not present, say you do not know yet. "
            "Answer directly and concisely.\n\n"
            f"Memory context:\n{self._compact_context(context)}"
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
        direct_reply = self._direct_reply(prompt)
        if direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            return direct_reply
        context = self.build_context(prompt)
        messages = self.build_messages(prompt, history, context)
        self.db.rollback()

        result = self.ollama.chat_with_metadata(
            messages,
            temperature=0.2,
            num_predict=self._num_predict(prompt, context),
        )
        reply = result.content
        self.store.add_chat_message(
            chat_id,
            "assistant",
            reply,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            duration_ms=result.duration_ms,
            thinking=result.thinking,
        )
        self.db.commit()
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
        direct_reply = self._direct_reply(prompt)
        if direct_reply is not None:
            assistant = self.store.add_chat_message(chat_id, "assistant", direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
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
            }
            return
        context = self.build_context(prompt)
        messages = self.build_messages(prompt, history, context)
        self.db.rollback()

        raw_reply = ""
        final_metadata: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "duration_ms": None,
        }
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

        reply = self.ollama.clean_response(raw_reply)
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
        yield {
            "type": "done",
            "message_id": assistant.id,
            "reply": reply,
            "thinking": thinking,
            "prompt_tokens": final_metadata.get("prompt_tokens"),
            "completion_tokens": final_metadata.get("completion_tokens"),
            "total_tokens": final_metadata.get("total_tokens"),
            "duration_ms": final_metadata.get("duration_ms"),
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
