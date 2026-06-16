from __future__ import annotations

from app.models import Memory, Preference, ProfileFact
from app.models.enums import MemoryType
from app.repositories.memory_store import MemoryStore


class ConflictResolutionService:
    """Resolve contradictions without deleting historical records."""

    def supersede_profile_key(self, store: MemoryStore, new_fact: ProfileFact) -> None:
        for old_fact in store.active_profile_by_key(new_fact.key):
            if old_fact.id != new_fact.id and old_fact.value != new_fact.value:
                old_fact.is_active = False

    def supersede_preference_category(self, store: MemoryStore, new_preference: Preference) -> None:
        for old_preference in store.active_preferences_by_category(new_preference.category):
            is_different_record = old_preference.id != new_preference.id
            has_different_value = old_preference.value != new_preference.value
            if is_different_record and has_different_value:
                old_preference.is_active = False

    def supersede_similar_memory(self, store: MemoryStore, new_memory: Memory) -> None:
        for old_memory in store.active_memories_by_type(new_memory.memory_type):
            if old_memory.id == new_memory.id:
                continue
            if self._conflicts(old_memory, new_memory):
                old_memory.is_active = False
                old_memory.superseded_by_id = new_memory.id

    def _conflicts(self, old_memory: Memory, new_memory: Memory) -> bool:
        if old_memory.memory_type in {MemoryType.IDENTITY, MemoryType.PREFERENCE}:
            return self._prefix(old_memory.memory_text) == self._prefix(new_memory.memory_text)
        return old_memory.memory_text.strip().lower() == new_memory.memory_text.strip().lower()

    def _prefix(self, text: str) -> str:
        return text.split("=", maxsplit=1)[0].strip().lower()
