from __future__ import annotations

import json
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from app.models import Event, Goal, Memory, Preference, ProfileFact, Project
from app.models.enums import CandidateStatus, CandidateType, GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.services.conflicts import ConflictResolutionService


class MemoryReviewRequest(BaseModel):
    candidate_id: int
    decision: CandidateStatus = Field(pattern="^(accepted|rejected|merged)$")
    merged_into_memory_id: int | None = None


class MemoryReviewResult(BaseModel):
    candidate_id: int
    status: CandidateStatus
    accepted_memory_id: int | None = None


class MemoryReviewService:
    """Promote, reject, or merge pending memory candidates."""

    def __init__(self, conflicts: ConflictResolutionService | None = None) -> None:
        self.conflicts = conflicts or ConflictResolutionService()

    def review(self, store: MemoryStore, request: MemoryReviewRequest) -> MemoryReviewResult:
        candidate = store.get_candidate(request.candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate {request.candidate_id} does not exist.")
        if candidate.status != CandidateStatus.PENDING:
            raise ValueError(f"Candidate {candidate.id} has already been reviewed.")

        if request.decision == CandidateStatus.REJECTED:
            candidate.status = CandidateStatus.REJECTED
            candidate.reviewed_at = datetime.now(UTC)
            store.db.flush()
            return MemoryReviewResult(candidate_id=candidate.id, status=candidate.status)

        if request.decision == CandidateStatus.MERGED:
            return self._merge(store, candidate, request.merged_into_memory_id)

        memory = self._accept(store, candidate)
        candidate.status = CandidateStatus.ACCEPTED
        candidate.reviewed_at = datetime.now(UTC)
        candidate.accepted_memory_id = memory.id
        store.db.flush()
        return MemoryReviewResult(
            candidate_id=candidate.id,
            status=candidate.status,
            accepted_memory_id=memory.id,
        )

    def _merge(
        self,
        store: MemoryStore,
        candidate,
        merged_into_memory_id: int | None,
    ) -> MemoryReviewResult:
        if merged_into_memory_id is None:
            raise ValueError("merged_into_memory_id is required for merged decisions.")
        memory = store.get_memory(merged_into_memory_id)
        if memory is None:
            raise ValueError(f"Memory {merged_into_memory_id} does not exist.")
        memory.memory_text = f"{memory.memory_text}\n{candidate.candidate_text}"
        memory.importance = max(memory.importance, candidate.importance)
        candidate.status = CandidateStatus.MERGED
        candidate.reviewed_at = datetime.now(UTC)
        candidate.accepted_memory_id = memory.id
        store.db.flush()
        return MemoryReviewResult(
            candidate_id=candidate.id,
            status=candidate.status,
            accepted_memory_id=memory.id,
        )

    def _accept(self, store: MemoryStore, candidate) -> Memory:
        attrs = self._attributes(candidate.reasoning)
        if candidate.candidate_type == CandidateType.IDENTITY:
            key = str(attrs.get("key", "general"))
            value = str(attrs.get("value", candidate.candidate_text))
            memory_text = f"{key} = {value}"
            existing_memory = self._existing_memory(store, MemoryType.IDENTITY, memory_text)
            existing_profile = next(
                (
                    fact
                    for fact in store.active_profile_by_key(key)
                    if fact.value == value
                ),
                None,
            )
            if existing_memory is not None and existing_profile is not None:
                return existing_memory
            profile = store.add(
                ProfileFact(
                    key=key,
                    value=value,
                    confidence=candidate.confidence,
                )
            )
            memory_type = MemoryType.IDENTITY
            memory = existing_memory or store.add(self._memory(candidate, memory_type, memory_text))
            self.conflicts.supersede_profile_key(store, profile)
            self.conflicts.supersede_similar_memory(store, memory)
            return memory

        if candidate.candidate_type == CandidateType.PREFERENCE:
            category = str(attrs.get("category", "general"))
            value = str(attrs.get("value", candidate.candidate_text))
            memory_text = f"{category} = {value}"
            existing_memory = self._existing_memory(store, MemoryType.PREFERENCE, memory_text)
            existing_preference = next(
                (
                    preference
                    for preference in store.active_preferences_by_category(category)
                    if preference.value == value
                ),
                None,
            )
            if existing_memory is not None and existing_preference is not None:
                return existing_memory
            preference = store.add(
                Preference(
                    category=category,
                    value=value,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                )
            )
            memory = existing_memory or store.add(
                self._memory(candidate, MemoryType.PREFERENCE, memory_text)
            )
            self.conflicts.supersede_preference_category(store, preference)
            self.conflicts.supersede_similar_memory(store, memory)
            return memory

        if candidate.candidate_type == CandidateType.GOAL:
            goal = store.add(
                Goal(
                    goal=str(attrs.get("goal", candidate.candidate_text)),
                    description=candidate.candidate_text,
                    priority=int(attrs.get("priority", candidate.importance)),
                    status=GoalStatus.ACTIVE,
                )
            )
            return store.add(self._memory(candidate, MemoryType.GOAL_RELATED, goal.goal))

        if candidate.candidate_type == CandidateType.PROJECT:
            project = store.add(
                Project(
                    name=str(attrs.get("name", candidate.candidate_text)),
                    description=str(attrs.get("description", candidate.candidate_text)),
                    priority=candidate.importance,
                    status=ProjectStatus.ACTIVE,
                )
            )
            return store.add(self._memory(candidate, MemoryType.PROJECT_RELATED, project.name))

        if candidate.candidate_type == CandidateType.EVENT:
            event_date = self._parse_date(attrs.get("event_date"))
            event = store.add(
                Event(
                    event=str(attrs.get("event", candidate.candidate_text)),
                    description=candidate.candidate_text,
                    event_date=event_date,
                    importance=candidate.importance,
                )
            )
            return store.add(self._memory(candidate, MemoryType.LIFE_FACT, event.event))

        memory = store.add(self._memory(candidate, MemoryType.KNOWLEDGE, candidate.candidate_text))
        self.conflicts.supersede_similar_memory(store, memory)
        return memory

    def _existing_memory(
        self,
        store: MemoryStore,
        memory_type: MemoryType,
        memory_text: str,
    ) -> Memory | None:
        for memory in store.active_memories_by_type(memory_type):
            if memory.memory_text == memory_text:
                return memory
        return None

    def _memory(self, candidate, memory_type: MemoryType, text: str) -> Memory:
        return Memory(
            memory_text=text,
            memory_type=memory_type,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source=f"memory_candidate:{candidate.id}",
        )

    def _attributes(self, reasoning: str | None) -> dict:
        if not reasoning:
            return {}
        try:
            payload = json.loads(reasoning)
        except json.JSONDecodeError:
            return {}
        return payload.get("attributes", {})

    def _parse_date(self, value) -> date | None:
        if not value:
            return None
        return date.fromisoformat(str(value))
