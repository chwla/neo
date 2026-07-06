from __future__ import annotations

import json
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from app.models import Event, Goal, Memory, Preference, ProfileFact, Project
from app.models.enums import CandidateStatus, CandidateType, GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.services.conflicts import ConflictResolutionService
from app.services.lifecycle import MemoryLifecycleService


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

    def __init__(
        self,
        conflicts: ConflictResolutionService | None = None,
        lifecycle: MemoryLifecycleService | None = None,
    ) -> None:
        self.conflicts = conflicts or ConflictResolutionService()
        self.lifecycle = lifecycle or MemoryLifecycleService()

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

        tombstone = self._resurrection_tombstone(store, candidate)
        if tombstone is not None:
            attrs = self._attributes(candidate.reasoning)
            if not attrs.get("allow_resurrection"):
                candidate.status = CandidateStatus.REJECTED
                candidate.reviewed_at = datetime.now(UTC)
                candidate.reasoning = self._merge_reasoning(
                    candidate.reasoning,
                    {
                        "lifecycle_rejection": "Rejected to prevent resurrection of inactive memory.",
                        "tombstone_memory_id": tombstone.id,
                        "tombstone_status": tombstone.status,
                    },
                )
                self.lifecycle.record_resurrection_blocked(
                    store,
                    tombstone,
                    candidate.candidate_text,
                    "Blocked likely resurrection of inactive memory.",
                )
                store.db.flush()
                return MemoryReviewResult(candidate_id=candidate.id, status=candidate.status)

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
        if not memory.is_active or memory.status != "active":
            raise ValueError(
                f"Memory {merged_into_memory_id} is not active and cannot be merged into."
            )
        memory.memory_text = f"{memory.memory_text}\n{candidate.candidate_text}"
        memory.importance = max(memory.importance, candidate.importance)
        memory.update_reason = "Merged accepted candidate into active memory."
        store._sync_memory_fts(memory)
        store._mark_embedding_stale(memory)
        store._sync_memory_embedding(memory)
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
                (fact for fact in store.active_profile_by_key(key) if fact.value == value),
                None,
            )
            if existing_profile is not None:
                existing_profile.confidence = max(existing_profile.confidence, candidate.confidence)
                memory = existing_memory or store.add(
                    self._memory(candidate, MemoryType.IDENTITY, memory_text)
                )
                self._refresh_memory(memory, candidate)
                return memory
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
            if existing_preference is not None:
                existing_preference.confidence = max(
                    existing_preference.confidence,
                    candidate.confidence,
                )
                existing_preference.importance = max(
                    existing_preference.importance,
                    candidate.importance,
                )
                memory = existing_memory or store.add(
                    self._memory(candidate, MemoryType.PREFERENCE, memory_text)
                )
                self._refresh_memory(memory, candidate)
                return memory
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
            goal_text = str(attrs.get("goal", candidate.candidate_text))
            for existing_goal in store.list_goals(GoalStatus.ACTIVE):
                if self._same_text(existing_goal.goal, goal_text):
                    existing_memory = self._existing_memory(
                        store,
                        MemoryType.GOAL_RELATED,
                        existing_goal.goal,
                    )
                    if existing_memory is not None:
                        self._refresh_memory(existing_memory, candidate)
                        return existing_memory
                    return store.add(
                        self._memory(candidate, MemoryType.GOAL_RELATED, existing_goal.goal)
                    )
            goal = store.add(
                Goal(
                    goal=goal_text,
                    description=candidate.candidate_text,
                    priority=int(attrs.get("priority", candidate.importance)),
                    status=GoalStatus.ACTIVE,
                )
            )
            return store.add(self._memory(candidate, MemoryType.GOAL_RELATED, goal.goal))

        if candidate.candidate_type == CandidateType.PROJECT:
            project_name = str(attrs.get("name", candidate.candidate_text))
            project_description = str(attrs.get("description", candidate.candidate_text))
            for existing_project in store.list_projects(ProjectStatus.ACTIVE):
                if self._same_text(existing_project.name, project_name):
                    if project_description and project_description != existing_project.description:
                        existing_project.description = project_description
                        existing_project.priority = max(
                            existing_project.priority, candidate.importance
                        )
                    existing_memory = self._existing_memory(
                        store,
                        MemoryType.PROJECT_RELATED,
                        existing_project.name,
                    )
                    if existing_memory is not None:
                        self._refresh_memory(existing_memory, candidate)
                        return existing_memory
                    return store.add(
                        self._memory(candidate, MemoryType.PROJECT_RELATED, existing_project.name)
                    )
            project = store.add(
                Project(
                    name=project_name,
                    description=project_description,
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

        existing_memory = self._existing_memory(
            store,
            MemoryType.KNOWLEDGE,
            candidate.candidate_text,
        )
        if existing_memory is not None:
            self._refresh_memory(existing_memory, candidate)
            return existing_memory
        existing_memory = self._existing_current_hardware(store, candidate.candidate_text)
        if existing_memory is not None:
            memory = store.add(
                self._memory(
                    candidate,
                    MemoryType.KNOWLEDGE,
                    candidate.candidate_text,
                    supersedes_id=existing_memory.id,
                    update_reason="User stated a replacement current hardware setup.",
                ),
            )
            self._supersede_memory(store, existing_memory, memory)
            return memory
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

    def _same_text(self, left: str | None, right: str | None) -> bool:
        if left is None or right is None:
            return False
        return " ".join(left.lower().split()) == " ".join(right.lower().split())

    def _existing_current_hardware(self, store: MemoryStore, candidate_text: str) -> Memory | None:
        if not candidate_text.lower().startswith("current hardware:"):
            return None
        for memory in store.active_memories_by_type(MemoryType.KNOWLEDGE):
            if memory.memory_text.lower().startswith("current hardware:"):
                return memory
        return None

    def _refresh_memory(self, memory: Memory, candidate) -> None:
        memory.confidence = max(memory.confidence, candidate.confidence)
        memory.importance = max(memory.importance, candidate.importance)
        attrs = self._attributes(candidate.reasoning)
        if attrs.get("source_sentence"):
            memory.source_sentence = str(attrs.get("source_sentence"))
        if attrs.get("source_conversation_id") is not None:
            memory.source_conversation_id = int(attrs["source_conversation_id"])
        if attrs.get("canonical_slot"):
            memory.canonical_slot = str(attrs.get("canonical_slot"))

    def _memory(
        self,
        candidate,
        memory_type: MemoryType,
        text: str,
        supersedes_id: int | None = None,
        update_reason: str | None = None,
    ) -> Memory:
        attrs = self._attributes(candidate.reasoning)
        return Memory(
            memory_text=text,
            memory_type=memory_type,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source=f"memory_candidate:{candidate.id}",
            source_sentence=str(attrs.get("source_sentence") or candidate.candidate_text),
            source_conversation_id=self._optional_int(attrs.get("source_conversation_id")),
            canonical_slot=str(
                attrs.get("canonical_slot") or self._canonical_slot(memory_type, text)
            ),
            status="active",
            supersedes_id=supersedes_id,
            update_reason=update_reason or str(attrs.get("update_reason") or ""),
        )

    def _supersede_memory(self, store: MemoryStore, old_memory: Memory, new_memory: Memory) -> None:
        self.lifecycle.supersede(
            store=store,
            old_memory=old_memory,
            new_memory=new_memory,
            reason="User stated a replacement current hardware setup.",
        )

    def _resurrection_tombstone(self, store: MemoryStore, candidate) -> Memory | None:
        memory_type, memory_text = self._candidate_memory_identity(candidate)
        attrs = self._attributes(candidate.reasoning)
        canonical_slot = attrs.get("canonical_slot")
        return store.inactive_memory_tombstone(
            memory_type,
            memory_text,
            canonical_slot=str(canonical_slot) if canonical_slot else None,
        )

    def _candidate_memory_identity(self, candidate) -> tuple[MemoryType, str]:
        attrs = self._attributes(candidate.reasoning)
        if candidate.candidate_type == CandidateType.IDENTITY:
            return (
                MemoryType.IDENTITY,
                f"{attrs.get('key', 'general')} = {attrs.get('value', candidate.candidate_text)}",
            )
        if candidate.candidate_type == CandidateType.PREFERENCE:
            return (
                MemoryType.PREFERENCE,
                f"{attrs.get('category', 'general')} = {attrs.get('value', candidate.candidate_text)}",
            )
        if candidate.candidate_type == CandidateType.GOAL:
            return MemoryType.GOAL_RELATED, str(attrs.get("goal", candidate.candidate_text))
        if candidate.candidate_type == CandidateType.PROJECT:
            return MemoryType.PROJECT_RELATED, str(attrs.get("name", candidate.candidate_text))
        if candidate.candidate_type == CandidateType.EVENT:
            return MemoryType.LIFE_FACT, str(attrs.get("event", candidate.candidate_text))
        return MemoryType.KNOWLEDGE, candidate.candidate_text

    def _merge_reasoning(self, reasoning: str | None, attributes: dict) -> str:
        payload: dict = {}
        if reasoning:
            try:
                payload = json.loads(reasoning)
            except json.JSONDecodeError:
                payload = {"note": reasoning}
        existing_attrs = payload.get("attributes")
        if not isinstance(existing_attrs, dict):
            existing_attrs = {}
        existing_attrs.update(attributes)
        payload["attributes"] = existing_attrs
        return json.dumps(payload, sort_keys=True)

    def _optional_int(self, value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _canonical_slot(self, memory_type: MemoryType, text: str) -> str:
        if text.lower().startswith("current hardware:"):
            return "current_hardware"
        return memory_type.value

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
