from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel

from app.models import Memory
from app.models.enums import MemoryType
from app.repositories.memory_store import MemoryStore


class MemoryEvidence(BaseModel):
    memory_id: int
    memory_text: str
    memory_type: str
    status: str
    confidence: float
    source_sentence: str | None
    source_conversation_id: int | None
    canonical_slot: str | None
    created_at: datetime
    updated_at: datetime
    supersedes_id: int | None
    superseded_by_id: int | None
    update_reason: str | None


class MemoryExplanation(BaseModel):
    query: str
    answer: str
    known: bool
    source_kind: str
    evidence: list[MemoryEvidence]


class MemoryExplanationService:
    """Build source-aware explanations from stored memory metadata."""

    def should_handle(self, query: str) -> bool:
        lowered = query.lower()
        return bool(
            re.search(
                r"\b(why do you (?:think|believe)|when did i tell you|did i directly say|"
                r"why do you know|source for|where did you get)\b",
                lowered,
            )
        )

    def explain(self, store: MemoryStore, query: str) -> MemoryExplanation:
        memories = self._candidate_memories(store, query)
        if not memories:
            return MemoryExplanation(
                query=query,
                answer="I do not have a stored memory that supports that claim.",
                known=False,
                source_kind="unknown",
                evidence=[],
            )

        active = [memory for memory in memories if memory.is_active]
        superseded = [memory for memory in memories if memory.status == "superseded"]
        evidence = [self._evidence(memory) for memory in [*active, *superseded]]
        primary = active[0] if active else memories[0]
        source_sentence = primary.source_sentence or primary.memory_text
        status = "updated memory" if primary.supersedes_id else "directly stated memory"
        answer = f"I believe this from a {status}: \"{source_sentence}\"."
        if self._asks_when(query):
            answer += f" It was recorded at {primary.created_at.isoformat()}."
        if self._asks_direct_or_inferred(query):
            answer += " This is stored as directly stated memory, not an inference."
        if primary.supersedes_id:
            answer += f" It superseded memory #{primary.supersedes_id}."
        if primary.update_reason:
            answer += f" Update reason: {primary.update_reason}"

        return MemoryExplanation(
            query=query,
            answer=answer,
            known=True,
            source_kind=status,
            evidence=evidence,
        )

    def answer(self, store: MemoryStore, query: str) -> str:
        explanation = self.explain(store, query)
        if not explanation.known:
            return explanation.answer
        lines = [explanation.answer]
        for item in explanation.evidence[:3]:
            source = item.source_sentence or item.memory_text
            lines.append(
                f"- Memory #{item.memory_id} [{item.status}, {item.memory_type}, "
                f"confidence {item.confidence:.2f}]: {source}",
            )
        return "\n".join(lines)

    def _candidate_memories(self, store: MemoryStore, query: str) -> list[Memory]:
        slot = self._slot_from_query(query)
        memories = store.list_memories(active_only=False, limit=500)
        if slot is not None:
            slot_matches = [memory for memory in memories if memory.canonical_slot == slot]
            if slot_matches:
                return sorted(slot_matches, key=lambda memory: memory.updated_at, reverse=True)
            if slot == "current_hardware":
                return [
                    memory
                    for memory in memories
                    if memory.memory_text.lower().startswith("current hardware:")
                ]

        terms = store.query_terms(query)
        scored: list[tuple[int, Memory]] = []
        for memory in memories:
            haystack = " ".join(
                part
                for part in [
                    memory.memory_text,
                    memory.source_sentence or "",
                    memory.canonical_slot or "",
                ]
                if part
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, memory))
        return [
            memory
            for _, memory in sorted(
                scored,
                key=lambda item: (item[0], item[1].updated_at),
                reverse=True,
            )
        ][:5]

    def _slot_from_query(self, query: str) -> str | None:
        lowered = query.lower()
        if re.search(r"\b(vs\s*code|visual studio code|editor|ide)\b", lowered):
            return "preference:editor"
        if re.search(r"\b(hardware|laptop|computer|gpu|graphics|dell inspiron)\b", lowered):
            return "current_hardware"
        if "favorite" in lowered and "language" in lowered:
            return "preference:favorite_programming_language"
        if re.search(r"\b(cp|competitive programming)\b", lowered):
            return "preference:competitive_programming_language"
        return None

    def _asks_when(self, query: str) -> bool:
        return bool(re.search(r"\b(when|what date|what time)\b", query.lower()))

    def _asks_direct_or_inferred(self, query: str) -> bool:
        return bool(re.search(r"\b(directly say|inferred|inferring|inference)\b", query.lower()))

    def _evidence(self, memory: Memory) -> MemoryEvidence:
        return MemoryEvidence(
            memory_id=memory.id,
            memory_text=memory.memory_text,
            memory_type=memory.memory_type.value if isinstance(memory.memory_type, MemoryType) else str(memory.memory_type),
            status=memory.status,
            confidence=memory.confidence,
            source_sentence=memory.source_sentence,
            source_conversation_id=memory.source_conversation_id,
            canonical_slot=memory.canonical_slot,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            supersedes_id=memory.supersedes_id,
            superseded_by_id=memory.superseded_by_id,
            update_reason=memory.update_reason,
        )
