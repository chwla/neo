from __future__ import annotations

import re

from app.models import Memory, Preference
from app.models.enums import GoalStatus, MemoryType
from app.repositories.memory_store import MemoryStore


class DirectMemoryAnswerService:
    """Answer simple memory questions without an LLM when evidence is explicit."""

    def answer(self, store: MemoryStore, query: str) -> str | None:
        lowered = query.lower()
        if self._asks_dedicated_gpu(lowered):
            return self._dedicated_gpu_answer(store)
        if self._asks_hardware(lowered):
            return self._hardware_answer(store)
        if self._asks_editor(lowered):
            return self._preference_answer(
                store,
                "editor",
                "You use {value}.",
            )
        if self._asks_competitive_programming_language(lowered):
            return self._preference_answer(
                store,
                "competitive_programming_language",
                "You use {value} for competitive programming.",
            )
        if self._asks_flutter_priority(lowered):
            return self._flutter_priority_answer(store)
        return None

    def _hardware_answer(self, store: MemoryStore) -> str | None:
        memory = self._current_hardware_memory(store)
        if memory is None:
            return None
        text = memory.memory_text.removeprefix("Current hardware:").strip()
        return f"Your current hardware is {text}."

    def _dedicated_gpu_answer(self, store: MemoryStore) -> str | None:
        memory = self._current_hardware_memory(store)
        if memory is None:
            return None
        lowered = memory.memory_text.lower()
        if "integrated graphics" in lowered or "integrated graphic" in lowered:
            return "No. Your stored current hardware says you have integrated graphics, not a dedicated GPU."
        if re.search(r"\b(rtx|gtx|nvidia|radeon|amd gpu|dedicated gpu)\b", lowered):
            return f"Your stored current hardware mentions a dedicated GPU: {memory.memory_text}."
        return "I do not have enough stored hardware detail to know whether you have a dedicated GPU."

    def _preference_answer(self, store: MemoryStore, category: str, template: str) -> str | None:
        preference = self._active_preference(store, category)
        if preference is None:
            return None
        return template.format(value=preference.value)

    def _current_hardware_memory(self, store: MemoryStore) -> Memory | None:
        memories = [
            memory
            for memory in store.active_memories_by_type(MemoryType.KNOWLEDGE)
            if memory.canonical_slot == "current_hardware"
            or memory.memory_text.lower().startswith("current hardware:")
        ]
        return sorted(memories, key=lambda memory: memory.updated_at, reverse=True)[0] if memories else None

    def _active_preference(self, store: MemoryStore, category: str) -> Preference | None:
        preferences = store.active_preferences_by_category(category)
        return sorted(preferences, key=lambda preference: preference.updated_at, reverse=True)[0] if preferences else None

    def _flutter_priority_answer(self, store: MemoryStore) -> str | None:
        goals = store.list_goals(GoalStatus.ACTIVE)
        goal_text = " ".join(
            part
            for goal in goals
            for part in [goal.goal, goal.description or ""]
        ).lower()
        if not goal_text:
            return None
        if re.search(r"\b(ai|ml|backend|faang|systems|infrastructure)\b", goal_text):
            return (
                "Not as a main priority right now. Based on your stored career goals, "
                "you should prioritize AI/ML, backend systems, and strong engineering depth; "
                "learn Flutter only if a specific Neo or startup feature needs a mobile app."
            )
        return None

    def _asks_hardware(self, lowered: str) -> bool:
        return bool(
            re.search(
                r"\b(what|which|tell me|current|my|do i|can my)\b",
                lowered,
            )
            and re.search(
                r"\b(laptop|hardware|computer|machine|pc|system|specs|ram|processor|cpu)\b",
                lowered,
            )
        )

    def _asks_dedicated_gpu(self, lowered: str) -> bool:
        return bool(
            re.search(r"\b(do i|have|has|dedicated|gpu|graphics)\b", lowered)
            and re.search(r"\b(dedicated gpu|gpu|graphics card|nvidia|amd|rtx|gtx)\b", lowered)
        )

    def _asks_editor(self, lowered: str) -> bool:
        return bool(re.search(r"\b(editor|ide|write code|code in|work in)\b", lowered))

    def _asks_competitive_programming_language(self, lowered: str) -> bool:
        return bool(
            re.search(r"\b(language|code in|use)\b", lowered)
            and re.search(r"\b(cp|competitive programming)\b", lowered)
        )

    def _asks_flutter_priority(self, lowered: str) -> bool:
        return bool(
            "flutter" in lowered
            and re.search(r"\b(should|learn|priority|prioritize|right now|worth)\b", lowered)
        )
