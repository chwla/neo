from __future__ import annotations

from sqlalchemy.orm import Session

from app.repositories.memory_store import MemoryStore
from app.services.context import ContextAssemblyService, ContextPackage
from app.services.extraction import ExtractionRequest, MemoryExtractionService
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

    def build_context(self, prompt: str) -> ContextPackage:
        return self.context_assembler.assemble(
            self.store,
            RetrievalRequest(query=prompt, include_archives=True),
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
            "and current preferences.\n\n"
            f"Memory context:\n{context.model_dump_json(indent=2)}"
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
        context = self.build_context(prompt)
        messages = self.build_messages(prompt, history, context)
        self.store.add_chat_message(chat_id, "user", prompt)
        self.store.rename_chat_from_prompt(chat_id, prompt)
        reply = self.ollama.chat(messages)
        self.store.add_chat_message(chat_id, "assistant", reply)
        self.extract_after_turn(prompt, reply)
        self.db.commit()
        return reply

    def extract_after_turn(self, user_prompt: str, assistant_reply: str) -> list[int]:
        extraction = self.extractor.extract(
            ExtractionRequest(
                text=f"User: {user_prompt}\nAssistant: {assistant_reply}",
                persist=True,
            )
        )
        candidates = self.extractor.persist_candidates(self.store, extraction)
        self.db.commit()
        return [candidate.id for candidate in candidates]
