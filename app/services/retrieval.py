from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any

from pydantic import BaseModel, Field

from app.models import GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.schemas.memory_objects import (
    EventRead,
    GoalRead,
    MemoryRead,
    PreferenceRead,
    ProfileFactRead,
    ProjectRead,
)
from app.services.archives import ArchiveSearchResult, QdrantArchiveService


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)
    include_archives: bool = True


class RetrievalResult(BaseModel):
    profile: list[ProfileFactRead]
    preferences: list[PreferenceRead]
    goals: list[GoalRead]
    projects: list[ProjectRead]
    relevant_memories: list[MemoryRead]
    events: list[EventRead]
    archive_results: list[ArchiveSearchResult]
    archive_error: str | None = None


class RetrievalService:
    """Retrieve structured memory in priority order plus optional archive hits."""

    def __init__(
        self,
        archive_service: QdrantArchiveService | None = None,
        enable_default_archives: bool = False,
    ) -> None:
        self.archive_service = archive_service
        self.enable_default_archives = enable_default_archives

    def retrieve(self, store: MemoryStore, request: RetrievalRequest) -> RetrievalResult:
        query = request.query
        personal_intent = self._personal_intent(query)
        advice_intent = self._advice_intent(query)
        memory_intent = personal_intent or advice_intent or self._memory_intent(query)

        memories = store.search_memories(query, limit=request.limit) if memory_intent else []
        memories = self._filter_memory_hits(
            store,
            query,
            memories,
            advice_intent=advice_intent,
        )
        for memory in memories:
            memory.last_accessed_at = datetime.now(UTC)

        archive_results: list[ArchiveSearchResult] = []
        archive_error = None
        if request.include_archives:
            try:
                archive_results = self._archives().search(request.query, limit=request.limit)
            except Exception as exc:  # Qdrant can be unavailable on local machines.
                archive_error = str(exc)

        return RetrievalResult(
            goals=[
                GoalRead.model_validate(goal)
                for goal in self._goals_for_query(store, query, request.limit, personal_intent, advice_intent)
            ],
            projects=[
                ProjectRead.model_validate(project)
                for project in self._projects_for_query(store, query, request.limit, personal_intent, advice_intent)
            ],
            profile=[
                ProfileFactRead.model_validate(fact)
                for fact in self._profile_for_query(store, query, request.limit, personal_intent)
            ],
            preferences=[
                PreferenceRead.model_validate(preference)
                for preference in self._preferences_for_query(
                    store,
                    query,
                    request.limit,
                    personal_intent,
                    advice_intent,
                )
            ],
            relevant_memories=[MemoryRead.model_validate(memory) for memory in memories],
            events=[
                EventRead.model_validate(event)
                for event in (store.search_events(query) if memory_intent else [])
            ],
            archive_results=archive_results,
            archive_error=archive_error,
        )

    def search_all(self, store: MemoryStore, query: str, limit: int = 10) -> dict[str, list[Any]]:
        return {
            "goals": [GoalRead.model_validate(item) for item in store.search_goals(query, limit)],
            "projects": [
                ProjectRead.model_validate(item) for item in store.search_projects(query, limit)
            ],
            "events": [
                EventRead.model_validate(item)
                for item in store.search_events(query, limit)
            ],
            "memories": [
                MemoryRead.model_validate(item) for item in store.search_memories(query, limit)
            ],
        }

    def _archives(self) -> QdrantArchiveService:
        if self.archive_service is None:
            if not self.enable_default_archives:
                raise RuntimeError("Qdrant archive service is not configured.")
            self.archive_service = QdrantArchiveService()
        return self.archive_service

    def _profile_for_query(
        self,
        store: MemoryStore,
        query: str,
        limit: int,
        personal_intent: bool,
    ):
        if not personal_intent:
            return []
        lowered = query.lower()
        if self._about_me_intent(lowered):
            return store.list_profile()[:limit]
        key_matches: list = []
        key_patterns = {
            "name": r"\b(name|called)\b",
            "age": r"\b(age|old)\b",
            "location": r"\b(location|where|from|live)\b",
            "education": r"\b(education|study|degree|college|university)\b",
            "birthday": r"\b(birthday|birth date|date of birth)\b",
        }
        for key, pattern in key_patterns.items():
            if re.search(pattern, lowered):
                key_matches.extend(store.active_profile_by_key(key))
        if key_matches:
            return key_matches[:limit]
        if re.search(r"\b(name|age|old|location|where|from|education|study|degree|birthday)\b", lowered):
            return store.search_profile(query, limit)
        return []

    def _preferences_for_query(
        self,
        store: MemoryStore,
        query: str,
        limit: int,
        personal_intent: bool,
        advice_intent: bool,
    ):
        if not (personal_intent or advice_intent):
            return []
        lowered = query.lower()
        if self._about_me_intent(lowered):
            return store.list_preferences()[:limit]
        preferences = store.search_preferences(query, limit)
        category_hint = self._preference_category_hint(lowered)
        if category_hint is not None:
            preferences = [
                preference
                for preference in preferences
                if category_hint in preference.category
            ]
            if not preferences:
                preferences = [
                    preference
                    for preference in store.list_preferences()
                    if category_hint in preference.category
                ][:limit]
        elif advice_intent:
            terms = store.query_terms(query)
            preferences = [
                preference
                for preference in preferences
                if self._memory_match_count(
                    f"{preference.category} {preference.value}",
                    terms,
                )
                >= 2
            ]
        if preferences:
            return preferences
        if advice_intent and self._preference_advice_fallback(lowered):
            return store.list_preferences()[: min(limit, 5)]
        return []

    def _goals_for_query(
        self,
        store: MemoryStore,
        query: str,
        limit: int,
        personal_intent: bool,
        advice_intent: bool,
    ):
        lowered = query.lower()
        if self._preference_category_hint(lowered) is not None or self._hardware_query(lowered):
            return []
        if self._about_me_intent(lowered) or advice_intent or re.search(
            r"\b(goal|career|roadmap|plan|should|long[- ]term|fit)\b",
            lowered,
        ):
            return store.list_goals(GoalStatus.ACTIVE)[:limit]
        if personal_intent:
            return store.search_goals(query, limit)
        return []

    def _projects_for_query(
        self,
        store: MemoryStore,
        query: str,
        limit: int,
        personal_intent: bool,
        advice_intent: bool,
    ):
        lowered = query.lower()
        if self._about_me_intent(lowered) or re.search(
            r"\b(project|projects|building|working on|build|startup|fit|assistant|improve|focus on)\b",
            lowered,
        ):
            return store.list_projects(ProjectStatus.ACTIVE)[:limit]
        if personal_intent or advice_intent:
            return store.search_projects(query, limit)
        return []

    def _personal_intent(self, query: str) -> bool:
        lowered = query.lower()
        return bool(
            self._about_me_intent(lowered)
            or re.search(r"\b(my|me|mine|i|i'm|am i|do i|should i)\b", lowered)
        )

    def _advice_intent(self, query: str) -> bool:
        return bool(
            re.search(
                r"\b(suggest|recommend|advice|should|roadmap|plan|choose|worth|fit|"
                r"help me|prioritize|priority|direction|target|learn)\b",
                query.lower(),
            )
        )

    def _memory_intent(self, query: str) -> bool:
        return bool(re.search(r"\b(remember|memory|know about)\b", query.lower()))

    def _about_me_intent(self, lowered_query: str) -> bool:
        return bool(re.search(r"\b(about me|know about me|my profile|who am i)\b", lowered_query))

    def _preference_category_hint(self, lowered_query: str) -> str | None:
        if re.search(r"\b(cp|competitive programming)\b", lowered_query):
            return "competitive_programming"
        if "web development" in lowered_query or "web dev" in lowered_query:
            return "web_development"
        if "backend" in lowered_query:
            return "backend"
        if (
            "editor" in lowered_query
            or "ide" in lowered_query
            or "write code" in lowered_query
            or "where do i code" in lowered_query
            or "code in" in lowered_query
        ):
            return "editor"
        if "favorite" in lowered_query and "language" in lowered_query:
            return "favorite_programming_language"
        return None

    def _preference_advice_fallback(self, lowered_query: str) -> bool:
        return bool(
            re.search(r"\b(suggest|recommend|choose)\b", lowered_query)
            and re.search(
                r"\b(framework|language|editor|ide|stack|tool|library|database)\b",
                lowered_query,
            )
        )

    def _filter_memory_hits(self, store: MemoryStore, query: str, memories: list, advice_intent: bool):
        if not memories:
            return memories
        lowered = query.lower()
        if self._current_hardware_query(lowered):
            return [
                memory
                for memory in memories
                if memory.canonical_slot == "current_hardware"
                or memory.memory_text.lower().startswith("current hardware:")
            ]
        if self._hardware_query(lowered):
            return [
                memory
                for memory in memories
                if memory.canonical_slot == "current_hardware"
                or "hardware" in memory.memory_text.lower()
                or "machine" in memory.memory_text.lower()
                or "integrated graphics" in memory.memory_text.lower()
            ]
        category_hint = self._preference_category_hint(lowered)
        if category_hint is not None:
            return [
                memory
                for memory in memories
                if self._memory_matches_preference_category(memory, category_hint)
            ]
        if self._project_memory_query(lowered):
            return [
                memory
                for memory in memories
                if memory.memory_type == MemoryType.PROJECT_RELATED
                or "project" in memory.memory_text.lower()
                or "assistant" in memory.memory_text.lower()
            ]
        if self._career_memory_query(lowered):
            return [
                memory
                for memory in memories
                if memory.memory_type == MemoryType.GOAL_RELATED
                or "goal" in memory.memory_text.lower()
                or "career" in memory.memory_text.lower()
            ]
        minimum_matches = 2 if advice_intent else 1
        terms = store.query_terms(query)
        return [
            memory
            for memory in memories
            if self._memory_match_count(memory.memory_text, terms) >= minimum_matches
            or (advice_intent and memory.memory_type in {MemoryType.GOAL_RELATED, MemoryType.PROJECT_RELATED})
        ]

    def _memory_matches_preference_category(self, memory, category_hint: str) -> bool:
        lowered = memory.memory_text.lower()
        if memory.canonical_slot == f"preference:{category_hint}":
            return True
        if category_hint in lowered:
            return True
        if category_hint == "editor":
            return "visual studio code" in lowered or "vs code" in lowered
        if category_hint == "competitive_programming_language":
            return "competitive programming" in lowered or "c++" in lowered
        return False

    def _project_memory_query(self, lowered_query: str) -> bool:
        return bool(re.search(r"\b(project|building|assistant|improve|focus on)\b", lowered_query))

    def _career_memory_query(self, lowered_query: str) -> bool:
        return bool(
            re.search(
                r"\b(career|roadmap|skills|flutter|frontend|target|prioritize)\b",
                lowered_query,
            )
        )

    def _memory_match_count(self, memory_text: str, terms: list[str]) -> int:
        lowered = memory_text.lower()
        return sum(1 for term in terms if term in lowered)

    def _current_hardware_query(self, lowered_query: str) -> bool:
        return bool(
            self._hardware_query(lowered_query)
            and re.search(
                r"\b(use|using|have|has|currently|current|laptop|computer|pc|specs|"
                r"gpu|graphics|system|machine|llm|llms|run)\b",
                lowered_query,
            )
        )

    def _hardware_query(self, lowered_query: str) -> bool:
        return bool(
            re.search(
                r"\b(hardware|laptop|computer|pc|specs|machine|system|gpu|graphics|"
                r"processor|cpu|ram|ssd|llm|llms)\b",
                lowered_query,
            )
        )
