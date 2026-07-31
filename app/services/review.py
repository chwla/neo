from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from app.models import (
    Activity,
    Education,
    Event,
    Goal,
    Memory,
    Preference,
    ProfileFact,
    Project,
)
from app.models.enums import CandidateStatus, CandidateType, GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.services.conflicts import ConflictResolutionService
from app.services.identity_facts import is_durable_identity_fact, normalize_identity_value
from app.services.lifecycle import MemoryLifecycleService
from app.services.memory_fingerprints import memory_fingerprint
from app.services.memory_scope import (
    canonical_domain_label,
    domains_for_text,
    is_global_response_style,
    primary_domain_for_text,
)


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

        if candidate.candidate_type == CandidateType.IDENTITY:
            attrs = self._attributes(candidate.reasoning)
            key = str(attrs.get("key", "general"))
            value = str(attrs.get("value", candidate.candidate_text))
            if not is_durable_identity_fact(key, value):
                candidate.status = CandidateStatus.REJECTED
                candidate.reviewed_at = datetime.now(UTC)
                candidate.reasoning = self._merge_reasoning(
                    candidate.reasoning,
                    {"validation_rejection": "Rejected non-durable identity candidate."},
                )
                store.db.flush()
                return MemoryReviewResult(candidate_id=candidate.id, status=candidate.status)

        tombstone = self._resurrection_tombstone(store, candidate)
        if tombstone is not None:
            attrs = self._attributes(candidate.reasoning)
            source_message_id = self._optional_int(attrs.get("source_message_id"))
            is_source_replacement = (
                tombstone.status == "archived"
                and store.source_was_detached_for_replacement(
                    tombstone.id,
                    source_message_id,
                )
            )
            if is_source_replacement:
                self.lifecycle.reactivate_source_replacement(store, tombstone)
            elif not attrs.get("allow_resurrection"):
                candidate.status = CandidateStatus.REJECTED
                candidate.reviewed_at = datetime.now(UTC)
                candidate.reasoning = self._merge_reasoning(
                    candidate.reasoning,
                    {
                        "lifecycle_rejection": (
                            "Rejected to prevent resurrection of inactive memory."
                        ),
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
        attrs = self._attributes(candidate.reasoning)
        store.attach_memory_source(
            memory,
            source_sentence=str(attrs.get("source_sentence") or candidate.candidate_text),
            source_conversation_id=self._optional_int(attrs.get("source_conversation_id")),
            source_message_id=self._optional_int(attrs.get("source_message_id")),
        )
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
            value = normalize_identity_value(key, str(attrs.get("value", candidate.candidate_text)))
            memory_text = f"{key} = {value}"
            fingerprint = self._fingerprint(candidate, MemoryType.IDENTITY, memory_text)
            existing_memory = store.active_memory_by_fingerprint(
                MemoryType.IDENTITY,
                fingerprint,
            ) or self._existing_memory(store, MemoryType.IDENTITY, memory_text)
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

        if candidate.candidate_type == CandidateType.EDUCATION:
            institution = str(attrs.get("institution") or "").strip()
            degree = self._optional_text(attrs.get("degree"))
            field = self._optional_text(attrs.get("field_of_study"))
            graduation_date = self._parse_date(attrs.get("graduation_date"))
            description = str(attrs.get("description") or candidate.candidate_text)
            fingerprint = self._fingerprint(
                candidate,
                MemoryType.EDUCATION,
                candidate.candidate_text,
            )
            existing_education = self._matching_education(store, institution, fingerprint)
            existing_memory = store.active_memory_by_fingerprint(
                MemoryType.EDUCATION,
                fingerprint,
            ) or self._existing_memory(
                store,
                MemoryType.EDUCATION,
                candidate.candidate_text,
            )
            if existing_education is not None:
                existing_education.degree = degree or existing_education.degree
                existing_education.field_of_study = field or existing_education.field_of_study
                existing_education.graduation_date = (
                    graduation_date or existing_education.graduation_date
                )
                existing_education.description = self._education_description(existing_education)
                existing_memory = existing_memory or self._memory_for_education(
                    store,
                    existing_education,
                )
                memory = existing_memory or store.add(
                    self._memory(candidate, MemoryType.EDUCATION, candidate.candidate_text)
                )
                if existing_memory is not None:
                    memory.memory_text = existing_education.description
                    memory.canonical_slot = (
                        f"education:{self._education_key(existing_education.institution)}"
                    )
                self._refresh_memory(memory, candidate)
                if existing_memory is not None:
                    memory.canonical_slot = (
                        f"education:{self._education_key(existing_education.institution)}"
                    )
                    store._sync_memory_fts(memory)
                    store._mark_embedding_stale(memory)
                    store._sync_memory_embedding(memory)
                return memory
            if not institution:
                # There is no profile education record to resolve this partial statement
                # against. Keep it as a grounded knowledge memory instead of creating an
                # unusable education row with a fabricated institution.
                return store.add(self._memory(candidate, MemoryType.KNOWLEDGE, description))
            store.add(
                Education(
                    institution=institution,
                    degree=degree,
                    field_of_study=field,
                    graduation_date=graduation_date,
                    description=description,
                    fingerprint=fingerprint,
                ),
            )
            return existing_memory or store.add(
                self._memory(candidate, MemoryType.EDUCATION, candidate.candidate_text)
            )

        if candidate.candidate_type == CandidateType.PREFERENCE:
            category = str(attrs.get("category", "general"))
            value = str(attrs.get("value", candidate.candidate_text))
            memory_text = f"{category} = {value}"
            canonical_slot = str(attrs.get("canonical_slot") or f"preference:{category}")
            if self._is_explicit_replacement(attrs):
                return self._replace_preference(
                    store,
                    candidate,
                    category=category,
                    value=value,
                    memory_text=memory_text,
                    canonical_slot=canonical_slot,
                )
            fingerprint = self._fingerprint(candidate, MemoryType.PREFERENCE, memory_text)
            existing_memory = store.active_memory_by_fingerprint(
                MemoryType.PREFERENCE,
                fingerprint,
            ) or self._existing_memory(store, MemoryType.PREFERENCE, memory_text)
            existing_preference = next(
                (
                    preference
                    for preference in store.list_preferences()
                    if self._compatible_preference_domains(
                        preference,
                        category=category,
                        value=value,
                        canonical_slot=canonical_slot,
                    )
                    and (
                        preference.value == value
                        or (
                            preference.canonical_slot == canonical_slot
                            or self._same_preference_subject_slot(
                                preference.canonical_slot,
                                canonical_slot,
                            )
                            or self._overlapping_preference_refinement(
                                preference.value,
                                value,
                                existing_slot=preference.canonical_slot,
                                candidate_slot=canonical_slot,
                                additive=bool(attrs.get("additive")),
                            )
                        )
                    )
                ),
                None,
            )
            if existing_preference is not None:
                previous_value = existing_preference.value
                previous_slot = existing_preference.canonical_slot
                previous_fingerprint = existing_preference.fingerprint
                memory = (
                    existing_memory
                    or self._memory_for_preference_record(
                        store,
                        previous_fingerprint,
                        previous_slot,
                        previous_value,
                    )
                )
                existing_preference.category = category
                existing_preference.value = value
                existing_preference.canonical_slot = canonical_slot
                existing_preference.fingerprint = fingerprint
                existing_preference.confidence = max(
                    existing_preference.confidence,
                    candidate.confidence,
                )
                existing_preference.importance = max(
                    existing_preference.importance,
                    candidate.importance,
                )
                memory = (
                    memory
                    or store.add(self._memory(candidate, MemoryType.PREFERENCE, memory_text))
                )
                previous_memory_text = memory.memory_text
                memory.memory_text = memory_text
                memory.canonical_slot = canonical_slot
                self._refresh_memory(memory, candidate)
                if previous_memory_text != memory_text:
                    memory.update_reason = (
                        "Canonicalized an overlapping preference refinement."
                    )
                    store.record_lifecycle_audit(
                        memory,
                        "canonicalized",
                        previous_status="active",
                        new_status="active",
                        reason=memory.update_reason,
                        source_sentence=str(
                            attrs.get("source_sentence") or candidate.candidate_text
                        ),
                    )
                store._sync_memory_fts(memory)
                store._mark_embedding_stale(memory)
                store._sync_memory_embedding(memory)
                return memory
            preference = store.add(
                Preference(
                    category=category,
                    value=value,
                    canonical_slot=canonical_slot,
                    fingerprint=fingerprint,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                )
            )
            memory = existing_memory or store.add(
                self._memory(candidate, MemoryType.PREFERENCE, memory_text)
            )
            if not attrs.get("additive"):
                self.conflicts.supersede_preference_slot(store, preference)
                self.conflicts.supersede_similar_memory(store, memory)
            return memory

        if candidate.candidate_type == CandidateType.GOAL:
            goal_text = str(attrs.get("goal", candidate.candidate_text))
            if self._is_explicit_replacement(attrs):
                return self._replace_goal(store, candidate, goal_text)
            fingerprint = self._fingerprint(candidate, MemoryType.GOAL_RELATED, goal_text)
            for existing_goal in store.list_goals(GoalStatus.ACTIVE):
                exact_match = existing_goal.fingerprint == fingerprint or self._same_text(
                    existing_goal.goal, goal_text
                )
                compatible_refinement = self._compatible_text_domains(
                    existing_goal.goal,
                    goal_text,
                ) and self._overlapping_goal_refinement(
                    existing_goal.goal,
                    goal_text,
                )
                if exact_match or compatible_refinement:
                    previous_goal_text = existing_goal.goal
                    existing_memory = store.active_memory_by_fingerprint(
                        MemoryType.GOAL_RELATED,
                        existing_goal.fingerprint or "",
                    ) or self._existing_memory(
                        store,
                        MemoryType.GOAL_RELATED,
                        previous_goal_text,
                    )
                    existing_goal.goal = goal_text
                    existing_goal.description = candidate.candidate_text
                    existing_goal.priority = max(
                        existing_goal.priority,
                        int(attrs.get("priority", candidate.importance)),
                    )
                    existing_goal.fingerprint = fingerprint
                    existing_goal.horizon_months = (
                        self._optional_int(
                            attrs.get("horizon_months"),
                        )
                        or existing_goal.horizon_months
                    )
                    existing_goal.target_date = (
                        self._parse_date(attrs.get("target_date")) or existing_goal.target_date
                    )
                    if existing_memory is not None:
                        existing_memory.memory_text = goal_text
                        existing_memory.canonical_slot = str(
                            attrs.get("canonical_slot")
                            or f"goal:{self._canonical_token_key(goal_text)}"
                        )
                        self._refresh_memory(existing_memory, candidate)
                        if previous_goal_text != goal_text:
                            existing_memory.update_reason = (
                                "Canonicalized an overlapping goal refinement."
                            )
                            store.record_lifecycle_audit(
                                existing_memory,
                                "canonicalized",
                                previous_status="active",
                                new_status="active",
                                reason=existing_memory.update_reason,
                                source_sentence=str(
                                    attrs.get("source_sentence")
                                    or candidate.candidate_text
                                ),
                            )
                        store._sync_memory_fts(existing_memory)
                        store._mark_embedding_stale(existing_memory)
                        store._sync_memory_embedding(existing_memory)
                        return existing_memory
                    return store.add(
                        self._memory(candidate, MemoryType.GOAL_RELATED, goal_text)
                    )
            goal = store.add(
                Goal(
                    goal=goal_text,
                    description=candidate.candidate_text,
                    priority=int(attrs.get("priority", candidate.importance)),
                    target_date=self._parse_date(attrs.get("target_date")),
                    horizon_months=self._optional_int(attrs.get("horizon_months")),
                    fingerprint=fingerprint,
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

        if candidate.candidate_type == CandidateType.ACTIVITY:
            category = str(attrs.get("category") or "general")
            activity_text = str(attrs.get("activity") or candidate.candidate_text)
            started_at = self._parse_datetime(attrs.get("started_at")) or datetime.now(UTC)
            expires_at = self._parse_datetime(attrs.get("expires_at"))
            if expires_at is None:
                raise ValueError("Accepted current activities require an expiry.")
            fingerprint = self._fingerprint(
                candidate,
                MemoryType.ACTIVITY,
                activity_text,
            )
            existing_activity = next(
                (
                    activity
                    for activity in store.list_activities()
                    if activity.fingerprint == fingerprint
                ),
                None,
            )
            existing_memory = store.active_memory_by_fingerprint(
                MemoryType.ACTIVITY,
                fingerprint,
            ) or self._existing_memory(store, MemoryType.ACTIVITY, activity_text)
            if existing_activity is not None:
                existing_activity.expires_at = self._later_datetime(
                    existing_activity.expires_at,
                    expires_at,
                )
                memory = existing_memory or store.add(
                    self._memory(candidate, MemoryType.ACTIVITY, activity_text)
                )
                self._refresh_memory(memory, candidate)
                return memory
            for old_activity in store.list_activities():
                if old_activity.category != category:
                    continue
                old_activity.is_active = False
                old_activity.archived_at = started_at
                old_memory = store.active_memory_by_fingerprint(
                    MemoryType.ACTIVITY,
                    old_activity.fingerprint,
                )
                if old_memory is not None:
                    self.lifecycle.archive(
                        store,
                        old_memory,
                        "Archived after the user stated a newer current activity.",
                    )
            store.add(
                Activity(
                    category=category,
                    activity=activity_text,
                    description=candidate.candidate_text,
                    fingerprint=fingerprint,
                    started_at=started_at,
                    expires_at=expires_at,
                ),
            )
            return store.add(self._memory(candidate, MemoryType.ACTIVITY, activity_text))

        if candidate.candidate_type == CandidateType.EVENT:
            event_date = self._parse_date(attrs.get("event_date"))
            event_text = str(attrs.get("event", candidate.candidate_text))
            fingerprint = self._fingerprint(candidate, MemoryType.LIFE_FACT, event_text)
            existing_event = next(
                (
                    event
                    for event in store.list_events(limit=100000)
                    if event.fingerprint == fingerprint
                    or (
                        event.fingerprint is None
                        and self._same_text(event.event, event_text)
                        and event.event_date == event_date
                    )
                ),
                None,
            )
            existing_memory = store.active_memory_by_fingerprint(
                MemoryType.LIFE_FACT,
                fingerprint,
            ) or self._existing_memory(store, MemoryType.LIFE_FACT, event_text)
            if existing_event is not None:
                existing_event.fingerprint = fingerprint
                existing_event.importance = max(
                    existing_event.importance,
                    candidate.importance,
                )
                memory = existing_memory or store.add(
                    self._memory(candidate, MemoryType.LIFE_FACT, event_text)
                )
                self._refresh_memory(memory, candidate)
                return memory
            event = store.add(
                Event(
                    event=event_text,
                    description=str(attrs.get("description") or candidate.candidate_text),
                    event_date=event_date,
                    fingerprint=fingerprint,
                    importance=candidate.importance,
                )
            )
            return store.add(self._memory(candidate, MemoryType.LIFE_FACT, event.event))

        fingerprint = self._fingerprint(
            candidate,
            MemoryType.KNOWLEDGE,
            candidate.candidate_text,
        )
        existing_memory = store.active_memory_by_fingerprint(
            MemoryType.KNOWLEDGE,
            fingerprint,
        ) or self._existing_memory(
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

    def _matching_education(
        self,
        store: MemoryStore,
        institution: str,
        fingerprint: str,
    ) -> Education | None:
        education_records = store.list_education()
        for education in education_records:
            if education.fingerprint == fingerprint:
                return education
        normalized_institution = self._education_key(institution)
        if normalized_institution and not self._generic_education_reference(institution):
            return next(
                (
                    education
                    for education in education_records
                    if self._education_key(education.institution) == normalized_institution
                ),
                None,
            )
        # A statement such as "I studied CSE at college" supplies a field but no distinct
        # institution. It can safely enrich one existing education record, but must not guess
        # when multiple records are possible.
        return education_records[0] if len(education_records) == 1 else None

    @staticmethod
    def _education_key(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    @staticmethod
    def _generic_education_reference(value: str) -> bool:
        return MemoryReviewService._education_key(value) in {
            "college",
            "university",
            "school",
            "campus",
            "mycollege",
            "myuniversity",
        }

    @staticmethod
    def _education_description(education: Education) -> str:
        qualification = education.degree or "Education"
        if education.field_of_study:
            qualification = f"{qualification} in {education.field_of_study}"
        return f"{qualification} at {education.institution}"

    def _memory_for_education(self, store: MemoryStore, education: Education) -> Memory | None:
        memories = store.active_memories_by_type(MemoryType.EDUCATION)
        institution_key = self._education_key(education.institution)
        return next(
            (
                memory
                for memory in memories
                if institution_key and institution_key in self._education_key(memory.memory_text)
            ),
            memories[0] if len(memories) == 1 else None,
        )

    @staticmethod
    def _memory_for_preference(store: MemoryStore, canonical_slot: str) -> Memory | None:
        return next(
            (
                memory
                for memory in store.active_memories_by_type(MemoryType.PREFERENCE)
                if memory.canonical_slot == canonical_slot
                or MemoryReviewService._same_preference_subject_slot(
                    memory.canonical_slot,
                    canonical_slot,
                )
            ),
            None,
        )

    @staticmethod
    def _memory_for_preference_record(
        store: MemoryStore,
        fingerprint: str | None,
        canonical_slot: str | None,
        value: str,
    ) -> Memory | None:
        expected_text = value.casefold().strip()
        return next(
            (
                memory
                for memory in store.active_memories_by_type(MemoryType.PREFERENCE)
                if (fingerprint and memory.fingerprint == fingerprint)
                or (canonical_slot and memory.canonical_slot == canonical_slot)
                or memory.memory_text.partition("=")[2].casefold().strip() == expected_text
            ),
            None,
        )

    @classmethod
    def _overlapping_goal_refinement(cls, left: str, right: str) -> bool:
        return cls._canonical_overlap(left, right, minimum_common=3, minimum_ratio=0.65)

    @classmethod
    def _overlapping_preference_refinement(
        cls,
        left: str,
        right: str,
        *,
        existing_slot: str | None,
        candidate_slot: str | None,
        additive: bool,
    ) -> bool:
        if additive:
            return False
        protected_prefixes = (
            "preference:interest:",
            "preference:sentiment:",
            "preference:aversion:",
        )
        if any(
            str(slot or "").startswith(protected_prefixes)
            for slot in (existing_slot, candidate_slot)
        ):
            return False
        return cls._canonical_overlap(left, right, minimum_common=4, minimum_ratio=0.6)

    @staticmethod
    def _compatible_preference_domains(
        existing,
        *,
        category: str,
        value: str,
        canonical_slot: str,
    ) -> bool:
        """Prevent a semantically similar preference from updating another domain."""

        existing_text = " ".join(
            (
                str(existing.category or ""),
                str(existing.value or ""),
                str(existing.canonical_slot or ""),
            )
        )
        candidate_text = " ".join((category, value, canonical_slot))
        existing_global = is_global_response_style(existing_text)
        candidate_global = is_global_response_style(candidate_text)
        if existing_global or candidate_global:
            return existing_global and candidate_global

        existing_domain = MemoryReviewService._preference_domain_key(
            str(existing.category or ""),
            existing_text,
        )
        candidate_domain = MemoryReviewService._preference_domain_key(
            category,
            candidate_text,
        )
        if existing_domain and candidate_domain:
            return existing_domain == candidate_domain

        existing_domains = domains_for_text(
            existing_text
        )
        candidate_domains = domains_for_text(candidate_text)
        return MemoryReviewService._domain_sets_are_compatible(
            existing_domains,
            candidate_domains,
        )

    @staticmethod
    def _compatible_text_domains(left: str, right: str) -> bool:
        left_domain = primary_domain_for_text(left)
        right_domain = primary_domain_for_text(right)
        if left_domain and right_domain:
            return left_domain == right_domain
        return MemoryReviewService._domain_sets_are_compatible(
            domains_for_text(left),
            domains_for_text(right),
        )

    @staticmethod
    def _preference_domain_key(category: str, full_text: str) -> str | None:
        if category not in {"general", "response_style"}:
            return canonical_domain_label(category)
        return primary_domain_for_text(full_text)

    @staticmethod
    def _is_explicit_replacement(attrs: dict) -> bool:
        return bool(attrs.get("replacement_intent")) and bool(
            attrs.get("replacement_domain") or attrs.get("domain")
        )

    @staticmethod
    def _replacement_negated_hints(attrs: dict) -> tuple[str, ...]:
        return tuple(
            fragment.strip()
            for fragment in str(attrs.get("negated_fragments") or "").split("|")
            if fragment.strip()
        )

    @staticmethod
    def _matches_replacement_hint(value: str, hints: tuple[str, ...]) -> bool:
        value_tokens = re.findall(r"[a-z0-9]+", value.casefold())
        normalized_value = " ".join(value_tokens)
        if not normalized_value:
            return False
        for hint in hints:
            hint_tokens = re.findall(r"[a-z0-9]+", hint.casefold())
            normalized_hint = " ".join(hint_tokens)
            if normalized_hint and (
                normalized_hint in normalized_value
                or normalized_value in normalized_hint
            ):
                return True
            meaningful_hint_tokens = set(hint_tokens) - {"and", "not", "or"}
            if meaningful_hint_tokens and meaningful_hint_tokens <= set(value_tokens):
                return True
        return False

    @classmethod
    def _matches_replacement_domain(
        cls,
        existing_domain: str | None,
        target_domain: str,
        value: str,
        hints: tuple[str, ...],
    ) -> bool:
        """Match exact domains, or a hinted legacy head-only representation."""

        normalized_existing = canonical_domain_label(existing_domain or "")
        normalized_target = canonical_domain_label(target_domain) or target_domain
        if normalized_existing == normalized_target:
            return True
        if not normalized_existing:
            return cls._matches_replacement_hint(value, hints)
        legacy_head = normalized_target.rsplit("_", maxsplit=1)[-1]
        return (
            normalized_existing == legacy_head
            and cls._matches_replacement_hint(value, hints)
        )

    @staticmethod
    def _is_legacy_domain_head(
        existing_domain: str | None,
        target_domain: str,
    ) -> bool:
        normalized_existing = canonical_domain_label(existing_domain or "")
        normalized_target = canonical_domain_label(target_domain) or target_domain
        return (
            "_" in normalized_target
            and normalized_existing == normalized_target.rsplit("_", maxsplit=1)[-1]
        )

    def _replace_preference(
        self,
        store: MemoryStore,
        candidate,
        *,
        category: str,
        value: str,
        memory_text: str,
        canonical_slot: str,
    ) -> Memory:
        """Create a new canonical preference and retire its contradicted predecessor."""

        attrs = self._attributes(candidate.reasoning)
        raw_domain = str(attrs.get("replacement_domain") or attrs.get("domain") or "")
        domain = canonical_domain_label(raw_domain) or raw_domain
        if domain:
            category = domain
            canonical_slot = f"preference:{domain}"
            memory_text = f"{category} = {value}"
        negated_hints = self._replacement_negated_hints(attrs)
        old_preferences = [
            preference
            for preference in store.list_preferences()
            if preference.is_active
            and self._matches_replacement_domain(
                self._preference_domain_key(
                    str(preference.category or ""),
                    " ".join(
                        (
                            str(preference.category or ""),
                            str(preference.value or ""),
                            str(preference.canonical_slot or ""),
                        )
                    ),
                ),
                domain,
                preference.value,
                negated_hints,
            )
        ]
        fingerprint = self._fingerprint(
            candidate,
            MemoryType.PREFERENCE,
            memory_text,
        )
        preference = next(
            (
                existing
                for existing in old_preferences
                if self._same_text(existing.value, value)
            ),
            None,
        )
        if preference is None:
            preference = next(
                (
                    existing
                    for existing in store.list_preferences()
                    if existing.is_active
                    and self._same_text(existing.value, value)
                    and self._is_legacy_domain_head(
                        self._preference_domain_key(
                            str(existing.category or ""),
                            " ".join(
                                (
                                    str(existing.category or ""),
                                    str(existing.value or ""),
                                    str(existing.canonical_slot or ""),
                                )
                            ),
                        ),
                        domain,
                    )
                ),
                None,
            )
        existing_preference = preference is not None
        if preference is None:
            preference = store.add(
                Preference(
                    category=category,
                    value=value,
                    canonical_slot=canonical_slot,
                    fingerprint=fingerprint,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                )
            )
        else:
            preference.category = category
            preference.canonical_slot = canonical_slot
            preference.fingerprint = fingerprint
            preference.confidence = max(preference.confidence, candidate.confidence)
            preference.importance = max(preference.importance, candidate.importance)
        memory = next(
            (
                existing
                for existing in store.active_memories_by_type(MemoryType.PREFERENCE)
                if self._same_text(
                    existing.memory_text.partition("=")[2] or existing.memory_text,
                    value,
                )
                and (
                    self._memory_domain(existing) == domain
                    or self._is_legacy_domain_head(
                        self._memory_domain(existing),
                        domain,
                    )
                )
            ),
            None,
        )
        if memory is None and existing_preference:
            memory = self._existing_memory(
                store,
                MemoryType.PREFERENCE,
                memory_text,
            )
        if memory is None:
            memory = store.add(self._memory(candidate, MemoryType.PREFERENCE, memory_text))
        else:
            memory.memory_text = memory_text
            memory.canonical_slot = canonical_slot
            self._refresh_memory(memory, candidate)
            store._sync_memory_fts(memory)
            store._mark_embedding_stale(memory)
            store._sync_memory_embedding(memory)
        for old_preference in old_preferences:
            if old_preference.id != preference.id:
                old_preference.is_active = False
        for old_memory in list(store.active_memories_by_type(MemoryType.PREFERENCE)):
            if old_memory.id == memory.id:
                continue
            if not self._matches_replacement_domain(
                self._memory_domain(old_memory),
                domain,
                old_memory.memory_text,
                negated_hints,
            ):
                continue
            self.lifecycle.supersede(
                store,
                old_memory,
                memory,
                "Superseded by the user's explicit same-domain preference correction.",
            )
        return memory

    def _replace_goal(self, store: MemoryStore, candidate, goal_text: str) -> Memory:
        """Replace a same-domain goal even when its wording has no token overlap."""

        attrs = self._attributes(candidate.reasoning)
        raw_domain = str(attrs.get("replacement_domain") or attrs.get("domain") or "")
        domain = canonical_domain_label(raw_domain) or raw_domain
        negated_hints = self._replacement_negated_hints(attrs)
        old_memories = [
            memory
            for memory in store.active_memories_by_type(MemoryType.GOAL_RELATED)
            if self._matches_replacement_domain(
                self._memory_domain(memory),
                domain,
                memory.memory_text,
                negated_hints,
            )
        ]
        fingerprint = self._fingerprint(
            candidate,
            MemoryType.GOAL_RELATED,
            goal_text,
        )
        goal = next(
            (
                existing
                for existing in store.list_goals(GoalStatus.ACTIVE)
                if existing.fingerprint == fingerprint
                or self._same_text(existing.goal, goal_text)
            ),
            None,
        )
        memory = next(
            (
                existing
                for existing in old_memories
                if existing.fingerprint == fingerprint
                or self._same_text(existing.memory_text, goal_text)
            ),
            None,
        ) or self._existing_memory(
            store,
            MemoryType.GOAL_RELATED,
            goal_text,
        )
        for old_goal in store.list_goals(GoalStatus.ACTIVE):
            if goal is not None and old_goal.id == goal.id:
                continue
            if any(self._same_text(old_goal.goal, old.memory_text) for old in old_memories):
                old_goal.status = GoalStatus.ABANDONED
        if goal is None:
            goal = store.add(
                Goal(
                    goal=goal_text,
                    description=candidate.candidate_text,
                    priority=int(attrs.get("priority", candidate.importance)),
                    target_date=self._parse_date(attrs.get("target_date")),
                    horizon_months=self._optional_int(attrs.get("horizon_months")),
                    fingerprint=fingerprint,
                    status=GoalStatus.ACTIVE,
                )
            )
        else:
            goal.description = candidate.candidate_text
            goal.priority = max(
                goal.priority,
                int(attrs.get("priority", candidate.importance)),
            )
            goal.target_date = self._parse_date(attrs.get("target_date")) or goal.target_date
            goal.horizon_months = (
                self._optional_int(attrs.get("horizon_months")) or goal.horizon_months
            )
            goal.fingerprint = fingerprint
        if memory is None:
            memory = store.add(self._memory(candidate, MemoryType.GOAL_RELATED, goal.goal))
        else:
            self._refresh_memory(memory, candidate)
        for old_memory in old_memories:
            if old_memory.id == memory.id:
                continue
            self.lifecycle.supersede(
                store,
                old_memory,
                memory,
                "Superseded by the user's explicit same-domain goal correction.",
            )
        return memory

    @staticmethod
    def _memory_domain(memory: Memory) -> str | None:
        slot = str(memory.canonical_slot or "")
        parts = slot.split(":")
        if len(parts) >= 2 and parts[0] in {"goal", "preference"}:
            return canonical_domain_label(parts[1])
        return primary_domain_for_text(memory.memory_text)

    @staticmethod
    def _domain_sets_are_compatible(
        left: frozenset[str],
        right: frozenset[str],
    ) -> bool:
        if not left and not right:
            return True
        return bool(left & right)

    @classmethod
    def _canonical_overlap(
        cls,
        left: str,
        right: str,
        *,
        minimum_common: int,
        minimum_ratio: float,
    ) -> bool:
        left_tokens = cls._canonical_tokens(left)
        right_tokens = cls._canonical_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        common = left_tokens & right_tokens
        containment = len(common) / min(len(left_tokens), len(right_tokens))
        return len(common) >= minimum_common and containment >= minimum_ratio

    @staticmethod
    def _canonical_tokens(value: str) -> set[str]:
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "but",
            "for",
            "i",
            "in",
            "is",
            "my",
            "of",
            "on",
            "the",
            "to",
            "want",
            "while",
            "with",
        }
        aliases = {
            "cardio": "stamina",
            "exercises": "exercise",
            "fitness": "fitness",
            "improved": "improve",
            "improving": "improve",
            "plans": "plan",
            "reps": "rep",
            "sets": "set",
            "steps": "step",
            "workout": "fitness",
            "workouts": "fitness",
        }
        tokens: set[str] = set()
        for raw_token in re.findall(r"[a-z0-9+#]+", value.casefold()):
            token = aliases.get(raw_token, raw_token)
            if token.endswith("s") and len(token) > 4 and token not in {"fitness"}:
                token = token[:-1]
            if token not in stopwords:
                tokens.add(token)
        return tokens

    @staticmethod
    def _canonical_token_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")

    @staticmethod
    def _same_preference_subject_slot(left: str | None, right: str | None) -> bool:
        """Match current and legacy slots for one user preference subject."""
        return (
            left_subject := MemoryReviewService._preference_subject_slot(left)
        ) is not None and left_subject == MemoryReviewService._preference_subject_slot(right)

    @staticmethod
    def _preference_subject_slot(value: str | None) -> str | None:
        parts = str(value or "").split(":", maxsplit=2)
        if len(parts) != 3 or parts[0] != "preference":
            return None
        if parts[1] not in {"interest", "aversion", "sentiment"}:
            return None
        return parts[2] or None

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
        memory.fingerprint = self._fingerprint(candidate, memory.memory_type, memory.memory_text)
        if attrs.get("expires_at"):
            memory.expires_at = self._parse_datetime(attrs.get("expires_at"))

    def _memory(
        self,
        candidate,
        memory_type: MemoryType,
        text: str,
        supersedes_id: int | None = None,
        update_reason: str | None = None,
    ) -> Memory:
        attrs = self._attributes(candidate.reasoning)
        fingerprint = self._fingerprint(candidate, memory_type, text)
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
            fingerprint=fingerprint,
            expires_at=self._parse_datetime(attrs.get("expires_at")),
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
            replacement_source_message_id=self._optional_int(
                attrs.get("source_message_id"),
            ),
        )

    def _candidate_memory_identity(self, candidate) -> tuple[MemoryType, str]:
        attrs = self._attributes(candidate.reasoning)
        if candidate.candidate_type == CandidateType.IDENTITY:
            return (
                MemoryType.IDENTITY,
                f"{attrs.get('key', 'general')} = {attrs.get('value', candidate.candidate_text)}",
            )
        if candidate.candidate_type == CandidateType.EDUCATION:
            return MemoryType.EDUCATION, candidate.candidate_text
        if candidate.candidate_type == CandidateType.PREFERENCE:
            return (
                MemoryType.PREFERENCE,
                f"{attrs.get('category', 'general')} = "
                f"{attrs.get('value', candidate.candidate_text)}",
            )
        if candidate.candidate_type == CandidateType.GOAL:
            return MemoryType.GOAL_RELATED, str(attrs.get("goal", candidate.candidate_text))
        if candidate.candidate_type == CandidateType.PROJECT:
            return MemoryType.PROJECT_RELATED, str(attrs.get("name", candidate.candidate_text))
        if candidate.candidate_type == CandidateType.ACTIVITY:
            return MemoryType.ACTIVITY, str(attrs.get("activity", candidate.candidate_text))
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

    def _fingerprint(
        self,
        candidate,
        memory_type: MemoryType,
        text: str,
    ) -> str:
        attrs = self._attributes(candidate.reasoning)
        if candidate.candidate_type == CandidateType.IDENTITY:
            return memory_fingerprint(
                "identity",
                attrs.get("key", "general"),
                attrs.get("value", text),
            )
        if candidate.candidate_type == CandidateType.EDUCATION:
            return memory_fingerprint(
                "education",
                attrs.get("institution"),
                attrs.get("degree"),
                attrs.get("field_of_study"),
                attrs.get("graduation_date"),
            )
        if candidate.candidate_type == CandidateType.PREFERENCE:
            return memory_fingerprint(
                "preference",
                attrs.get("canonical_slot") or attrs.get("category", "general"),
                attrs.get("value", text),
            )
        if candidate.candidate_type == CandidateType.GOAL:
            return memory_fingerprint("goal", attrs.get("goal", text))
        if candidate.candidate_type == CandidateType.PROJECT:
            return memory_fingerprint("project", attrs.get("name", text))
        if candidate.candidate_type == CandidateType.ACTIVITY:
            return memory_fingerprint(
                "activity",
                attrs.get("category", "general"),
                attrs.get("activity", text),
            )
        if candidate.candidate_type == CandidateType.EVENT:
            return memory_fingerprint(
                "event",
                attrs.get("event", text),
                attrs.get("event_date"),
            )
        return memory_fingerprint(memory_type.value, text)

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

    def _parse_datetime(self, value) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _later_datetime(self, left: datetime, right: datetime) -> datetime:
        if left.tzinfo is None:
            left = left.replace(tzinfo=UTC)
        if right.tzinfo is None:
            right = right.replace(tzinfo=UTC)
        return max(left, right)

    def _optional_text(self, value) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
